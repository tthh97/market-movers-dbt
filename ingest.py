"""
Ingest real daily OHLCV from yfinance into raw.prices in market.duckdb.

Idempotent: re-running upserts on (ticker, trade_date) via INSERT OR REPLACE,
so a same-day re-run refreshes rather than duplicates -- the same incremental
discipline the downstream dbt fact relies on.

Usage:
    python ingest.py                 # default 6 months
    python ingest.py --period 1y
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os

import duckdb
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "market.duckdb")
WATCHLIST = os.path.join(HERE, "seeds", "watchlist.csv")

DDL = """
create schema if not exists raw;
create table if not exists raw.prices (
    ticker      varchar,
    trade_date  date,
    open        double,
    high        double,
    low         double,
    close       double,
    adj_close   double,
    volume      bigint,
    source      varchar,
    ingested_at timestamp,
    primary key (ticker, trade_date)
);
"""

UPSERT = """
insert or replace into raw.prices
(ticker, trade_date, open, high, low, close, adj_close, volume, source, ingested_at)
values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def tickers() -> list[str]:
    with open(WATCHLIST, newline="") as f:
        return [r["ticker"] for r in csv.DictReader(f)]


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
    for idx, r in df.iterrows():
        trade_date = idx.date() if hasattr(idx, "date") else idx
        close = float(r.get("Close"))
        adj = r.get("Adj Close", close)
        rows.append((
            ticker, trade_date,
            float(r.get("Open", close)), float(r.get("High", close)),
            float(r.get("Low", close)), close,
            float(adj if adj == adj else close),  # NaN-guard
            int(r.get("Volume", 0) or 0),
            "yfinance", now,
        ))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="6mo", help="yfinance period, e.g. 3mo, 6mo, 1y")
    args = ap.parse_args()

    con = duckdb.connect(DB_PATH)
    con.execute(DDL)

    total = 0
    for t in tickers():
        print(f"Fetching {t} ...")
        rows = fetch(t, args.period)
        if rows:
            con.executemany(UPSERT, rows)
            total += len(rows)

    n = con.execute("select count(*) from raw.prices;").fetchone()[0]
    con.close()
    print(f"Upserted {total} rows; raw.prices now holds {n} rows.")


if __name__ == "__main__":
    main()
