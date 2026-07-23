"""
Backend-agnostic writes into raw.prices.

Both loaders - ingest.py (real yfinance data) and scripts/seed_sample.py
(synthetic offline data) - land rows in the same table, so the connect / DDL /
upsert logic lives here once rather than being duplicated per script.

Which warehouse is used is decided by DBT_TARGET, the *same* switch dbt reads
from profiles.yml, so the loader and the models can never disagree about where
the data went:

    DBT_TARGET unset or "snowflake"  -> Snowflake (the real warehouse)
    DBT_TARGET=duckdb                -> local market.duckdb (offline demo)

Both backends expose the same idempotent upsert on (ticker, trade_date), which
is what lets a same-day re-run refresh rather than duplicate, and what the
downstream incremental fct_prices relies on.
"""

from __future__ import annotations

import os
from typing import Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
DUCKDB_PATH = os.path.join(HERE, "market.duckdb")

# The one authoritative column order for a raw.prices row. Every loader builds
# tuples in exactly this order.
COLUMNS = (
    "ticker",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "source",
    "ingested_at",
)

Row = tuple


def target() -> str:
    """The active backend: 'snowflake' (default) or 'duckdb'."""
    return os.environ.get("DBT_TARGET", "snowflake").strip().lower()


# --------------------------------------------------------------------------
# Snowflake
# --------------------------------------------------------------------------

# NB: no CREATE SCHEMA here. RAW is provisioned once by the account bootstrap,
# and the LOADER role is deliberately granted CREATE TABLE on RAW but *not*
# CREATE SCHEMA on the database - the loader writes tables, an admin owns the
# schema layout. That RBAC split is the point, so the loader must not assume a
# privilege it isn't meant to have. On DuckDB (offline demo) the schema is
# created below, where it costs nothing and no roles exist.

# Snowflake does not enforce PRIMARY KEY, so the constraint here is documentation
# of the grain only - idempotency is enforced by the MERGE below, not by the key.
_SF_DDL_TABLE = """
create table if not exists {db}.raw.prices (
    ticker      varchar,
    trade_date  date,
    open        double,
    high        double,
    low         double,
    close       double,
    adj_close   double,
    volume      bigint,
    source      varchar,
    ingested_at timestamp_ntz,
    primary key (ticker, trade_date)
)
"""

_SF_STAGE = "create or replace temporary table {db}.raw.prices_stage like {db}.raw.prices"

_SF_INSERT_STAGE = (
    "insert into {db}.raw.prices_stage "
    "(ticker, trade_date, open, high, low, close, adj_close, volume, source, ingested_at) "
    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

# QUALIFY de-duplicates the staged batch before the MERGE. Without it, a feed
# that returns the same ticker-day twice makes Snowflake abort the whole
# statement with "Duplicate row detected during DML action".
_SF_MERGE = """
merge into {db}.raw.prices as t
using (
    select ticker, trade_date, open, high, low, close,
           adj_close, volume, source, ingested_at
    from {db}.raw.prices_stage
    qualify row_number() over (
        partition by ticker, trade_date order by ingested_at desc
    ) = 1
) as s
on t.ticker = s.ticker and t.trade_date = s.trade_date
when matched then update set
    t.open        = s.open,
    t.high        = s.high,
    t.low         = s.low,
    t.close       = s.close,
    t.adj_close   = s.adj_close,
    t.volume      = s.volume,
    t.source      = s.source,
    t.ingested_at = s.ingested_at
when not matched then insert
    (ticker, trade_date, open, high, low, close, adj_close, volume, source, ingested_at)
values
    (s.ticker, s.trade_date, s.open, s.high, s.low, s.close,
     s.adj_close, s.volume, s.source, s.ingested_at)
"""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. Snowflake needs SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, "
            "a credential (SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PASSWORD), and "
            "optionally SNOWFLAKE_ROLE / _WAREHOUSE / _DATABASE. "
            "For the offline demo instead, set DBT_TARGET=duckdb."
        )
    return value


def _private_key_der(path: str, passphrase: str | None):
    """Load a PEM key into the DER bytes the Snowflake connector expects."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(
            f.read(),
            password=passphrase.encode() if passphrase else None,
            backend=default_backend(),
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class _Snowflake:
    def __init__(self) -> None:
        import snowflake.connector

        self.database = os.environ.get("SNOWFLAKE_DATABASE", "MARKET_MOVERS")

        params = dict(
            account=_require("SNOWFLAKE_ACCOUNT"),
            user=_require("SNOWFLAKE_USER"),
            role=os.environ.get("SNOWFLAKE_ROLE", "TRANSFORMER"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
            database=self.database,
            autocommit=True,
        )

        # Prefer key-pair auth; Snowflake blocks password-only sign-in for
        # service users, so CI always takes this branch.
        key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "").strip()
        if key_path:
            params["private_key"] = _private_key_der(
                key_path, os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
            )
        else:
            params["password"] = _require("SNOWFLAKE_PASSWORD")

        self._con = snowflake.connector.connect(**params)

    def ensure_raw(self) -> None:
        # Table only - RAW schema is owned by the bootstrap, not the loader.
        cur = self._con.cursor()
        cur.execute(_SF_DDL_TABLE.format(db=self.database))
        cur.close()

    def upsert(self, rows: Sequence[Row]) -> None:
        if not rows:
            return
        cur = self._con.cursor()
        cur.execute(_SF_STAGE.format(db=self.database))
        cur.executemany(_SF_INSERT_STAGE.format(db=self.database), list(rows))
        cur.execute(_SF_MERGE.format(db=self.database))
        cur.close()

    def count(self) -> int:
        cur = self._con.cursor()
        cur.execute(f"select count(*) from {self.database}.raw.prices")
        n = cur.fetchone()[0]
        cur.close()
        return int(n)

    def close(self) -> None:
        self._con.close()


# --------------------------------------------------------------------------
# DuckDB (offline demo only)
# --------------------------------------------------------------------------

_DUCK_DDL = """
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

_DUCK_UPSERT = """
insert or replace into raw.prices
(ticker, trade_date, open, high, low, close, adj_close, volume, source, ingested_at)
values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


class _DuckDB:
    def __init__(self) -> None:
        import duckdb

        self._con = duckdb.connect(DUCKDB_PATH)

    def ensure_raw(self) -> None:
        self._con.execute(_DUCK_DDL)

    def upsert(self, rows: Sequence[Row]) -> None:
        if rows:
            self._con.executemany(_DUCK_UPSERT, list(rows))

    def count(self) -> int:
        return int(self._con.execute("select count(*) from raw.prices").fetchone()[0])

    def close(self) -> None:
        self._con.close()


# --------------------------------------------------------------------------

def connect():
    """Open the warehouse named by DBT_TARGET, with raw.prices guaranteed to exist."""
    backend = target()
    if backend == "duckdb":
        wh = _DuckDB()
    elif backend == "snowflake":
        wh = _Snowflake()
    else:
        raise SystemExit(
            f"Unknown DBT_TARGET '{backend}'. Expected 'snowflake' or 'duckdb'."
        )
    wh.ensure_raw()
    return wh


def describe() -> str:
    """Human-readable name of the active destination, for log lines."""
    if target() == "duckdb":
        return f"duckdb:{os.path.basename(DUCKDB_PATH)}"
    return f"snowflake:{os.environ.get('SNOWFLAKE_DATABASE', 'MARKET_MOVERS')}.raw.prices"
