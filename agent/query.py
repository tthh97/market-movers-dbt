"""
QueryRunner: run one read-only SQL statement against the warehouse, get typed rows.

The shape "connect, check the SQL is read-only, run it, hand back rows" used to
be copy-pasted across agent/tools.py, agent/report.py and scripts/build_viz.py,
and reverse-parsed out of text in agent/evals.py. It lives here once now. See
docs/design-query-runner.md.

The whole interface is small:

    runner = QueryRunner("snowflake")      # or "duckdb"
    result = runner.run("select ...")      # -> QueryResult(columns, rows, ...)

The seam is `engine`: two real adapters - Snowflake in production (the read-only
REPORTER identity taken from the environment) and DuckDB offline (the repo's
market.duckdb opened read_only=True, so the engine itself refuses writes). The
read-only guard lives inside run(), so a caller cannot reach the warehouse a way
that skips it.

render_for_model() turns a QueryResult into the pipe-delimited text the agent
shows the model; callers that want the values (evals, the dashboard) read
QueryResult.rows directly instead of parsing that text back apart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Rows the agent shows the model per query. Callers that need the whole result
# (the dashboard aggregates over it) pass limit=None to run().
DEFAULT_ROW_LIMIT = 50


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


def read_only_error(sql: str) -> str | None:
    """Return an error string if the statement is not a single read, else None.

    A trailing semicolon is fine and stripped; an interior one means more than
    one statement was submitted, so it is rejected rather than hoping the driver
    refuses it - "select 1; drop table t" passes a naive startswith() check.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return "ERROR: empty query."
    if ";" in stripped:
        return (
            "ERROR: only one statement per call. Remove the ';' and send a single "
            "SELECT, or make separate run_sql calls."
        )
    if not stripped.lower().startswith(("select", "with")):
        return (
            "ERROR: only SELECT queries are allowed. This tool is read-only - it "
            "cannot INSERT, UPDATE, DELETE, CREATE, or DROP."
        )
    return None


def require(name: str) -> str:
    """A required SNOWFLAKE_* variable, or a loud failure. No identifier is ever
    defaulted - a silent fallback to the wrong account or role is worse than a
    stop."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. Snowflake access needs SNOWFLAKE_ACCOUNT, "
            "SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY_PATH, SNOWFLAKE_ROLE, "
            "SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA. "
            "See agent/.env.example, or use the duckdb engine offline."
        )
    return value


def _connect_snowflake_reader():
    import sys

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import warehouse  # load_private_key lives with the writer path, at the root
    import snowflake.connector

    return snowflake.connector.connect(
        account=require("SNOWFLAKE_ACCOUNT"),
        user=require("SNOWFLAKE_USER"),
        private_key=warehouse.load_private_key(),
        role=require("SNOWFLAKE_ROLE"),
        warehouse=require("SNOWFLAKE_WAREHOUSE"),
        database=require("SNOWFLAKE_DATABASE"),
        schema=require("SNOWFLAKE_SCHEMA"),
        autocommit=True,
    )


def _connect_duckdb_read_only():
    import duckdb

    path = os.environ.get("MARKET_DUCKDB_PATH", os.path.join(ROOT, "market.duckdb"))
    # read_only=True is the engine enforcing what the guard promises: it refuses
    # writes itself, the offline counterpart to the REPORTER role on Snowflake.
    return duckdb.connect(path, read_only=True)


class QueryRunner:
    """Run read-only SQL against one engine and get rows back."""

    def __init__(self, engine: str):
        self.engine = engine.strip().lower()
        if self.engine not in ("snowflake", "duckdb"):
            raise SystemExit(
                f"Unknown engine {engine!r}. Expected 'snowflake' or 'duckdb'."
            )
        self._con = None

    def _connect(self):
        # One connection per runner, opened lazily and reused - reconnecting
        # costs about a second on Snowflake, and reuse does not keep the
        # warehouse awake.
        if self._con is None:
            self._con = (
                _connect_snowflake_reader()
                if self.engine == "snowflake"
                else _connect_duckdb_read_only()
            )
        return self._con

    def run(self, sql: str, limit: int | None = DEFAULT_ROW_LIMIT) -> QueryResult:
        """Run one read-only statement and return its rows.

        Errors come back on the result (is_error / error) rather than raised, so
        the caller - or the model reading render_for_model() - can correct the
        SQL and try again. `limit` caps the rows fetched and sets `truncated`;
        pass limit=None to fetch every row.
        """
        bad = read_only_error(sql)
        if bad:
            return QueryResult(error=bad)
        q = sql.strip().rstrip(";").strip()
        try:
            if self.engine == "snowflake":
                cur = self._connect().cursor()
                try:
                    cur.execute(q)
                    cols = [c[0] for c in cur.description]
                    rows = cur.fetchall() if limit is None else cur.fetchmany(limit)
                finally:
                    cur.close()
            else:  # duckdb
                cur = self._connect().execute(q)
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall() if limit is None else cur.fetchmany(limit)
        except Exception as e:  # returned to the caller, not raised
            return QueryResult(error=f"SQL ERROR: {e}")
        return QueryResult(
            columns=cols,
            rows=[tuple(r) for r in rows],
            truncated=limit is not None and len(rows) == limit,
        )

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


def render_for_model(result: QueryResult) -> str:
    """The pipe-delimited text the agent shows the model.

    Errors pass through unchanged so the model reads the warehouse's own
    message. This is the one place the text format lives; every other caller
    reads result.rows.
    """
    if result.is_error:
        return result.error
    if not result.rows:
        return "(0 rows)"
    out = [" | ".join(result.columns)]
    out += [
        " | ".join("NULL" if v is None else str(v) for v in r) for r in result.rows
    ]
    if result.truncated:
        out.append(
            f"(truncated at {len(result.rows)} rows - re-run with an aggregate, a "
            "tighter filter, or an explicit LIMIT rather than assuming this is "
            "everything)"
        )
    return "\n".join(out)
