"""
Generate synthetic daily OHLCV into raw.prices.

This exists so the whole dbt pipeline runs with ZERO network access and ZERO
credentials -- a reviewer can clone the repo and run:

    DBT_TARGET=duckdb python scripts/seed_sample.py
    DBT_TARGET=duckdb dbt build --profiles-dir .

It writes through the same warehouse layer as ingest.py, so it also works
against Snowflake if DBT_TARGET says so; DuckDB is simply the point of it.
For real data, use ingest.py instead (same target table, idempotent upsert).

Tickers and flags are read from seeds/watchlist.csv so the sample always
matches the modelled universe.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WATCHLIST = os.path.join(ROOT, "seeds", "watchlist.csv")

sys.path.insert(0, ROOT)
import warehouse  # noqa: E402 - single source of truth for the raw.prices schema

TRADING_DAYS = 180          # ~8 months of weekdays
SEED = 42

# Per-asset-class starting price, drift (daily) and volatility (daily) so the
# marts have realistic, differentiated signal.
PROFILES = {
    "crypto": dict(start=60000, drift=-0.0020, vol=0.045),
    "equity": dict(start=200,   drift=0.0006,  vol=0.018),
    "etf":    dict(start=500,   drift=0.0005,  vol=0.010),
}
# Override a couple of starts so prices look plausible per ticker. Only the
# tickers currently in seeds/watchlist.csv - unlisted equities fall through to
# PROFILES["equity"]["start"].
START_OVERRIDE = {
    "BTC-USD": 60000, "ETH-USD": 2500, "SOL-USD": 150,
    "AAPL": 230, "MSFT": 450, "NVDA": 130, "GOOGL": 180,
    "META": 600, "SPY": 740, "QQQ": 600,
}


def load_watchlist() -> list[dict]:
    with open(WATCHLIST, newline="") as f:
        return list(csv.DictReader(f))


def business_days(n: int) -> list[dt.date]:
    days, d = [], dt.date.today()
    while len(days) < n:
        if d.weekday() < 5:           # Mon-Fri
            days.append(d)
        d -= dt.timedelta(days=1)
    return sorted(days)


def gen_series(ticker: str, asset_class: str, dates: list[dt.date]) -> list[tuple]:
    rng = random.Random(f"{SEED}-{ticker}")
    prof = PROFILES.get(asset_class, PROFILES["equity"])
    price = float(START_OVERRIDE.get(ticker, prof["start"]))
    rows = []
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for d in dates:
        ret = rng.gauss(prof["drift"], prof["vol"])
        prev = price
        price = max(0.01, prev * (1 + ret))
        open_p = prev
        close_p = price
        high_p = max(open_p, close_p) * (1 + abs(rng.gauss(0, prof["vol"] / 2)))
        low_p = min(open_p, close_p) * (1 - abs(rng.gauss(0, prof["vol"] / 2)))
        volume = int(abs(rng.gauss(5_000_000, 1_500_000)))
        rows.append((
            ticker, d, round(open_p, 4), round(high_p, 4), round(low_p, 4),
            round(close_p, 4), round(close_p, 4), volume, "synthetic", now,
        ))
    return rows


def main() -> None:
    dates = business_days(TRADING_DAYS)
    watchlist = load_watchlist()

    all_rows: list[tuple] = []
    for row in watchlist:
        all_rows.extend(gen_series(row["ticker"], row["asset_class"], dates))

    con = warehouse.connect()
    print(f"Seeding into {warehouse.describe()}")
    con.upsert(all_rows)
    n = con.count()
    con.close()
    print(f"Seeded {len(all_rows)} synthetic rows; raw.prices now holds {n} rows.")


if __name__ == "__main__":
    main()
