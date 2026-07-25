"""
Ingest real daily OHLCV from yfinance into raw.prices.

The destination is whatever DBT_TARGET selects - Snowflake by default, or the
local DuckDB file for the offline demo. See warehouse.py; this script does not
care which backend it is talking to.

Idempotent: re-running upserts on (ticker, trade_date), so a same-day re-run
refreshes rather than duplicates -- the same incremental discipline the
downstream dbt fact relies on.

Usage:
    python ingest.py                 # default 6 months
    python ingest.py --period 1y
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os

import yfinance as yf

import warehouse

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "seeds", "watchlist.csv")


def tickers() -> list[str]:
    with open(WATCHLIST, newline="") as f:
        return [r["ticker"] for r in csv.DictReader(f)]


def _num(value, fallback=None):
    """Coerce a yfinance cell to a real number, or fall back.

    yfinance returns NaN for sessions that never settled - halts, holidays it
    got wrong, or a partial current bar - and it does so inconsistently, so the
    same request can be clean locally and carry NaN from a CI runner. A NaN
    float is rendered into SQL as the bare token NaN, which the warehouse then
    rejects as an unknown identifier, failing the whole batch. Note that
    `dict.get(key, default)` does not help: the column is present, its value is
    simply NaN.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return fallback
    # NaN is the only value not equal to itself.
    return f if f == f else fallback


def fetch(ticker: str, period: str) -> list[tuple]:
    """One ticker at a time keeps the columns single-indexed and simple."""
    df = yf.download(
        ticker, period=period, interval="1d",
        auto_adjust=False, progress=False,
    )
    if df is None or df.empty:
        print(f"  ! no data for {ticker}")
        return []

    # yfinance can return MultiIndex columns; flatten to the field name.
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [c[0] for c in df.columns]

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    rows = []
    skipped = 0
    for idx, r in df.iterrows():
        trade_date = idx.date() if hasattr(idx, "date") else idx

        # Close anchors the bar: every other price falls back to it, so a row
        # without one carries no information. Drop it rather than invent a
        # price - a fabricated close would flow straight into the returns and
        # momentum marts and be indistinguishable from a real one.
        close = _num(r.get("Close"))
        if close is None:
            skipped += 1
            continue

        rows.append((
            ticker, trade_date,
            _num(r.get("Open"), close), _num(r.get("High"), close),
            _num(r.get("Low"), close), close,
            _num(r.get("Adj Close"), close),
            int(_num(r.get("Volume"), 0)),
            "yfinance", now,
        ))

    if skipped:
        print(f"  ! {ticker}: skipped {skipped} row(s) with no usable close")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="6mo", help="yfinance period, e.g. 3mo, 6mo, 1y")
    args = ap.parse_args()

    con = warehouse.connect()
    print(f"Loading into {warehouse.describe()}")

    total = 0
    for t in tickers():
        print(f"Fetching {t} ...")
        rows = fetch(t, args.period)
        if rows:
            con.upsert(rows)
            total += len(rows)

    n = con.count()
    con.close()
    print(f"Upserted {total} rows; raw.prices now holds {n} rows.")


if __name__ == "__main__":
    main()
