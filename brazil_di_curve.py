# Brazil's DI1 futures curve from B3's public historical data.
#
# Downloads the DI1 futures history once (through PYield) and slices the same table two ways:
#   1. one market date -> every listed contract -> the DI1 curve for that date
#   2. one contract (DI1F31) -> every date it traded -> how its rate, price, DV01 and
#      open interest moved, set against the Selic target from the central bank (BCB).
#
# Run with:  python brazil_di_curve.py          (add --no-show to only save the charts)

import argparse
import sys
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyield as yd
import requests
from adjustText import adjust_text

CURVE_DATE = pd.Timestamp("2026-09-22")
CONTRACT = "DI1F31"  # the contract followed through time: DI1 maturing January 2031

# A broad history window. If CONTRACT is already listed at the very start of it,
# move HISTORY_START earlier. After changing the window, run once with --refresh.
HISTORY_START = "01-01-2018"
HISTORY_END = "22-09-2026"

DATA_DIR = Path(__file__).with_name("data")
OUTPUT_DIR = Path(__file__).with_name("output")
DI1_CACHE = DATA_DIR / "di1_history.parquet"
SELIC_CACHE = DATA_DIR / "selic_history.parquet"

BCB_SELIC_SERIES = 432  # BCB SGS series for the Selic target
BCB_API = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{BCB_SELIC_SERIES}/dados"

LABELLED_MONTHS = {"F", "J", "N", "V"}  # January and quarterly expiries: F=Jan, J=Apr, N=Jul, V=Oct
CROWDED_YEARS = 5.5  # labels up to this maturity are crowded, so adjustText spreads them out

LATE_2024_START = pd.Timestamp("2024-10-01")
LATE_2024_END = pd.Timestamp("2024-12-31")


class MarketDataError(RuntimeError):
    pass


# ---------------------------------------------------------------- data
# Fetch the Selic target (BCB series 432) once across a date range
def fetch_selic_history(start_date: date, end_date: date) -> pd.DataFrame:
    params = {
        "formato": "json",
        "dataInicial": start_date.strftime("%d/%m/%Y"),
        "dataFinal": end_date.strftime("%d/%m/%Y"),
    }

    response = requests.get(BCB_API, params=params, timeout=30)
    response.raise_for_status()

    try:
        records = response.json()
    except ValueError as err:
        raise MarketDataError("BCB Selic endpoint did not return JSON.") from err

    observations = []

    for record in records:
        try:
            observation_date = datetime.strptime(record["data"], "%d/%m/%Y")
            value = float(str(record["valor"]).replace(",", "."))  # BCB writes decimals with a comma
            observations.append((observation_date, value))
        except (KeyError, ValueError, TypeError):
            continue

    if not observations:
        raise MarketDataError(f"No Selic target observations found between {start_date} and {end_date}.")

    selic = pd.DataFrame(observations, columns=["data_referencia", "selic_target_pct"])
    selic = selic.sort_values("data_referencia")
    selic = selic.drop_duplicates("data_referencia", keep="last")

    return selic.reset_index(drop=True)


# Load the DI1 history from the cache, or download it from B3 through PYield and cache it
def load_di1_history(refresh: bool) -> pd.DataFrame:
    if DI1_CACHE.exists() and not refresh:
        print(f"\nLoading cached DI1 history from: {DI1_CACHE}")
        history = pd.read_parquet(DI1_CACHE)

    else:
        print(f"\nDownloading DI1 history from {HISTORY_START} to {HISTORY_END}. This first run can take a while.")

        available_dates = yd.futuro.datas_disponiveis("DI1")

        if hasattr(available_dates, "to_list"):
            available_dates = available_dates.to_list()

        window_start = pd.to_datetime(HISTORY_START, dayfirst=True)
        window_end = pd.to_datetime(HISTORY_END, dayfirst=True)

        history_dates = []

        for timestamp in pd.to_datetime(available_dates):
            if window_start <= pd.Timestamp(timestamp) <= window_end:
                history_dates.append(timestamp.date())

        if not history_dates:
            raise MarketDataError("No DI1 dates are available inside the requested history window.")

        history = yd.futuro.historico(history_dates, "DI1").to_pandas()
        history["data_referencia"] = pd.to_datetime(history["data_referencia"])

        DATA_DIR.mkdir(exist_ok=True)
        history.to_parquet(DI1_CACHE, index=False)
        print(f"Saved history to: {DI1_CACHE}")

    # The date column comes back as dates from PYield and as timestamps from Parquet
    history["data_referencia"] = pd.to_datetime(history["data_referencia"])

    print(
        "\nHistory loaded:",
        f"{history['data_referencia'].min().date()} -> {history['data_referencia'].max().date()}",
        f"| rows: {len(history):,}",
    )

    return history


# Load the Selic target history from the cache or BCB, limited to [start_date, end_date].
# Returns an empty table if BCB cannot be reached, so the charts still draw.
def load_selic_history(start_date: date, end_date: date, refresh: bool) -> pd.DataFrame:
    try:
        if SELIC_CACHE.exists() and not refresh:
            print(f"\nLoading cached Selic history from: {SELIC_CACHE}")
            selic = pd.read_parquet(SELIC_CACHE)
            selic["data_referencia"] = pd.to_datetime(selic["data_referencia"])
        else:
            print(f"\nDownloading BCB Selic target history (Series {BCB_SELIC_SERIES}).")
            selic = fetch_selic_history(start_date, end_date)

            DATA_DIR.mkdir(exist_ok=True)
            selic.to_parquet(SELIC_CACHE, index=False)
            print(f"Saved Selic history to: {SELIC_CACHE}")

    except (requests.RequestException, MarketDataError) as err:
        print(f"\nWARNING: could not retrieve Selic target history: {err}")
        return pd.DataFrame(columns=["data_referencia", "selic_target_pct"])

    in_window = selic["data_referencia"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))

    return selic[in_window].copy()


# ---------------------------------------------------------------- slice 1: the curve on one date
# Every contract on one market date, sorted by maturity
def curve_on(history: pd.DataFrame, curve_date: pd.Timestamp) -> pd.DataFrame:
    curve = history.loc[history["data_referencia"] == curve_date].copy()

    if curve.empty:
        raise MarketDataError(
            f"No DI1 rows found for curve date {curve_date.date()}. Check that the cached history covers this date."
        )

    curve["years_to_expiry"] = curve["dias_uteis"] / 252  # Brazilian 252-business-day years

    return curve


def print_curve_summary(curve: pd.DataFrame) -> None:
    print("\nCurve-date sample:")
    sample_columns = [
        "codigo_negociacao", "data_vencimento", "dias_uteis", "taxa_ajuste",
        "preco_ajuste", "dv01", "contratos_abertos",
    ]
    print(curve[sample_columns].head(10).to_string(index=False))

    # Reproduce B3's settlement price (PU) for the first contract.
    # taxa_ajuste is already a decimal in the PYield data.
    first = curve.iloc[0]
    pu_b3 = first["preco_ajuste"]
    pu_mine = 100_000 / (1 + first["taxa_ajuste"]) ** (first["dias_uteis"] / 252)

    print(f"\n{first['codigo_negociacao']}: B3 PU = {pu_b3:,.2f} | my PU = {pu_mine:,.2f}")

    curve = curve.sort_values("years_to_expiry")

    print("\nShortest curve points:")
    shortest_columns = ["codigo_negociacao", "data_vencimento", "dias_uteis", "years_to_expiry", "taxa_ajuste"]
    print(curve[shortest_columns].head(10).to_string(index=False))

    rankings = [
        ("open interest", "contratos_abertos"),
        ("contracts traded", "volume_negociado"),
        ("number of trades", "numero_negocios"),
    ]

    for title, column in rankings:
        print(f"\nTop 10 by {title}:")
        ranking_columns = ["codigo_negociacao", "years_to_expiry", "taxa_ajuste", column]
        print(curve.nlargest(10, column)[ranking_columns].to_string(index=False))


# Marker area grows with the square root of open interest: the ranking is kept while huge
# and small contracts stay comparable. The minimum size keeps low-OI points visible.
def marker_sizes(curve: pd.DataFrame) -> pd.Series:
    open_interest = curve["contratos_abertos"].fillna(0).astype(float)
    max_open_interest = open_interest.max()

    if max_open_interest > 0:
        return 25 + 175 * np.sqrt(open_interest / max_open_interest)
    else:
        return pd.Series(40.0, index=curve.index)  # every value zero or missing on this date


def plot_curve(curve: pd.DataFrame, curve_date: pd.Timestamp) -> plt.Figure:
    curve = curve.sort_values("years_to_expiry")
    rates_pct = curve["taxa_ajuste"] * 100
    sizes = marker_sizes(curve)

    traded = curve["volume_negociado"].fillna(0) > 0  # NaN for contracts that did not trade that session
    labelled = curve["codigo_negociacao"].str[3].isin(LABELLED_MONTHS)  # 4th character is the month code
    unlabelled = ~labelled

    fig, ax = plt.subplots(figsize=(14, 8))

    # Other contracts that traded
    selected = traded & unlabelled
    ax.scatter(
        curve.loc[selected, "years_to_expiry"], rates_pct[selected], s=sizes.loc[selected],
        label="Other traded contracts", alpha=0.65,
    )

    # Other contracts with no trades
    selected = ~traded & unlabelled
    ax.scatter(
        curve.loc[selected, "years_to_expiry"], rates_pct[selected], s=sizes.loc[selected],
        facecolors="none", edgecolors="black", linewidths=1.2, label="No trades; official settlement",
    )

    # January / quarterly contracts that traded
    selected = traded & labelled
    ax.scatter(
        curve.loc[selected, "years_to_expiry"], rates_pct[selected], s=sizes.loc[selected],
        color="tab:orange", edgecolors="black", linewidths=0.7, label="January / quarterly contracts", zorder=3,
    )

    # January / quarterly contracts with no trades
    selected = ~traded & labelled
    ax.scatter(
        curve.loc[selected, "years_to_expiry"], rates_pct[selected], s=sizes.loc[selected],
        facecolors="none", edgecolors="tab:orange", linewidths=1.8, zorder=3,
    )

    # Labels: adjustText spreads out the crowded front of the curve; the sparse long end gets fixed offsets
    label_rows = curve[labelled]
    texts = []

    for row in label_rows[label_rows["years_to_expiry"] <= CROWDED_YEARS].itertuples():
        texts.append(ax.text(row.years_to_expiry, row.taxa_ajuste * 100, row.codigo_negociacao,
                             fontsize=8, color="black"))

    if texts:
        crowded_points = curve[curve["years_to_expiry"] <= CROWDED_YEARS]
        adjust_text(
            texts,
            x=crowded_points["years_to_expiry"].to_numpy(),
            y=(crowded_points["taxa_ajuste"] * 100).to_numpy(),
            ax=ax,
            arrowprops=dict(arrowstyle="-", lw=0.5, color="gray"),
        )

    for row in label_rows[label_rows["years_to_expiry"] > CROWDED_YEARS].itertuples():
        ax.annotate(row.codigo_negociacao, (row.years_to_expiry, row.taxa_ajuste * 100),
                    xytext=(5, 5), textcoords="offset points", fontsize=8, color="black")

    ax.set_title(f"Brazil DI1 Futures Curve — {curve_date:%d %B %Y}")
    ax.set_xlabel("Years to expiry (DU / 252)")
    ax.set_ylabel("Settlement rate (%)")
    ax.set_xlim(left=0)
    ax.grid(alpha=0.25)
    ax.legend()

    fig.text(
        0.01, 0.01,
        "Marker area uses a square-root-scaled version of open interest. "
        "Hollow markers are listed contracts with no trades that session.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.035, 1, 1))

    return fig


# ---------------------------------------------------------------- slice 2: one contract through time
# Every observation of one contract, oldest first
def contract_history(history: pd.DataFrame, ticker: str) -> pd.DataFrame:
    contract = history.loc[history["codigo_negociacao"] == ticker].copy()

    if contract.empty:
        raise MarketDataError(f"No {ticker} observations were found in the downloaded history.")

    contract = contract.sort_values("data_referencia")
    contract["years_to_expiry"] = contract["dias_uteis"] / 252

    return contract


def print_contract_summary(contract: pd.DataFrame, selic: pd.DataFrame) -> None:
    if not selic.empty:
        latest = selic.iloc[-1]
        print(
            f"\nLatest Selic target in {CONTRACT} window:",
            f"{latest['selic_target_pct']:.2f}% on {latest['data_referencia'].date()}",
        )

    print(f"\n{CONTRACT} available history:")
    print("First observation:", contract["data_referencia"].min().date())
    print("Last observation: ", contract["data_referencia"].max().date())
    print("Number of observations:", len(contract))

    print(f"\n{CONTRACT} — first and last observations:")
    columns = [
        "data_referencia", "data_vencimento", "dias_uteis", "years_to_expiry",
        "taxa_ajuste", "preco_ajuste", "dv01", "contratos_abertos",
    ]
    print(contract[columns].iloc[[0, -1]].to_string(index=False))

    # A contract listed right at the start of the window may have earlier history we didn't fetch
    requested_start = pd.to_datetime(HISTORY_START, dayfirst=True)

    if contract["data_referencia"].min() <= requested_start + pd.Timedelta(days=31):
        print(
            f"\nWARNING: {CONTRACT} appears near the start of the requested history. "
            "Move HISTORY_START earlier if you want to test for an earlier listing."
        )


# Observations in late 2024, when the contract's DV01 moved sharply
def late_2024(contract: pd.DataFrame) -> pd.DataFrame:
    in_window = contract["data_referencia"].between(LATE_2024_START, LATE_2024_END)

    return contract[in_window].copy()


def print_late_2024_dv01(late: pd.DataFrame) -> None:
    if late.empty:
        return

    peak = late.loc[late["dv01"].idxmax()]

    print(f"\n{CONTRACT} — late-2024 DV01 check:")
    print(late[["data_referencia", "years_to_expiry", "taxa_ajuste", "dv01"]].iloc[[0, -1]].to_string(index=False))
    print("Peak Q4-2024 DV01:", f"{peak['dv01']:.4f} R$/bp", "on", peak["data_referencia"].date())


# Four panels sharing one date axis: rate (with the Selic target), price, DV01 and open interest
def plot_contract_history(contract: pd.DataFrame, selic: pd.DataFrame, late: pd.DataFrame) -> plt.Figure:
    fig, axes = plt.subplots(4, 1, figsize=(14, 11), sharex=True)
    dates = contract["data_referencia"]

    axes[0].plot(dates, contract["taxa_ajuste"] * 100, label=f"{CONTRACT} settlement rate")

    if not selic.empty:
        axes[0].step(selic["data_referencia"], selic["selic_target_pct"], where="post",
                     label=f"Selic target (BCB Series {BCB_SELIC_SERIES})")

    axes[0].set_ylabel("Rate (%)")
    axes[0].legend()

    axes[1].plot(dates, contract["preco_ajuste"])
    axes[1].set_ylabel("PU")

    axes[2].plot(dates, contract["dv01"])
    axes[2].set_ylabel("DV01\n(R$/bp)")

    axes[3].plot(dates, contract["contratos_abertos"])
    axes[3].set_ylabel("Open interest")
    axes[3].set_xlabel("Reference date")

    for ax in axes:
        ax.grid(alpha=0.25)

    # Mark late 2024 on the DV01 panel without asserting why DV01 moved
    if not late.empty:
        peak = late.loc[late["dv01"].idxmax()]
        axes[2].axvspan(LATE_2024_START, LATE_2024_END, alpha=0.08)
        axes[2].annotate("Late 2024", xy=(peak["data_referencia"], peak["dv01"]),
                         xytext=(8, 10), textcoords="offset points", fontsize=8)

    locator = mdates.AutoDateLocator()  # keeps the shared date axis readable over several years
    axes[3].xaxis.set_major_locator(locator)
    axes[3].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    fig.suptitle(f"{CONTRACT} Through Time — Settlement Rate, PU, DV01 and Open Interest", fontsize=14)
    fig.text(
        0.01, 0.01,
        "One fixed-expiry contract followed through time. "
        f"Sources: B3 historical futures data via PyYield; BCB SGS Series {BCB_SELIC_SERIES} for the Selic target.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))

    return fig


def save_figure(fig: plt.Figure, name: str) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / name
    fig.savefig(path, dpi=150)
    print(f"Saved chart to: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Brazil's DI1 futures curve from B3's public data.")
    parser.add_argument("--refresh", action="store_true", help="download the DI1 and Selic histories again")
    parser.add_argument("--no-show", action="store_true", help="save the charts without opening them")
    args = parser.parse_args()

    print("Python:", sys.version.split()[0], "| PYield:", version("pyield"))

    history = load_di1_history(args.refresh)

    # Slice 1: one market date -> many contracts -> one curve
    curve = curve_on(history, CURVE_DATE)
    print_curve_summary(curve)

    curve_figure = plot_curve(curve, CURVE_DATE)
    save_figure(curve_figure, f"di1_curve_{CURVE_DATE.date()}.png")

    if not args.no_show:
        plt.show()

    # Slice 2: one contract -> many dates -> the contract through time
    contract = contract_history(history, CONTRACT)
    selic = load_selic_history(
        contract["data_referencia"].min().date(), contract["data_referencia"].max().date(), args.refresh
    )
    print_contract_summary(contract, selic)

    late = late_2024(contract)
    print_late_2024_dv01(late)

    history_figure = plot_contract_history(contract, selic, late)
    save_figure(history_figure, f"{CONTRACT.lower()}_history.png")

    if not args.no_show:
        plt.show()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
