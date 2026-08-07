"""
Tools the agent can call: get_schema and run_sql.

The guardrails live inside run_sql rather than in a wrapper around it. That is
deliberate: a wrapper is something a future caller can forget to use, whereas a
check inside the only function that can reach the warehouse cannot be bypassed
by calling the tool a different way.

Three layers, weakest to strongest:

  1. Statement shape  - SELECT/WITH only, single statement. Cheap and local.
  2. Query budget     - a per-run cap, so a confused agent cannot loop on the
                        warehouse and run up credits.
  3. Snowflake grants - the agent connects as a role with USAGE + SELECT and
                        nothing else. This is the layer that actually matters;
                        the first two exist so obvious mistakes fail without
                        waking the warehouse at all.

Layer 3 is verified, not assumed: as the configured service user and role,
CREATE TABLE, DELETE, reading the raw landing schema, and USE ROLE ACCOUNTADMIN
all fail with access-control errors. Layers 1 and 2 keep cheap mistakes cheap.
"""

from __future__ import annotations

import os

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

MAX_ROWS = 50          # rows returned to the model per query
MAX_QUERIES = 12       # per process run; see _budget below

_con = None            # reused across calls - reconnecting costs ~1s each time
_queries_run = 0


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. The agent needs SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, "
            "SNOWFLAKE_PRIVATE_KEY_PATH, SNOWFLAKE_ROLE, SNOWFLAKE_WAREHOUSE, "
            "SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA. See .env.example."
        )
    return value


def _private_key_der(path: str):
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _connect():
    """One connection per process, opened lazily.

    Reused rather than reopened per query. This does not keep the warehouse
    awake - Snowflake suspends on idle regardless of open sessions - it just
    avoids paying the connect handshake on every tool call.
    """
    global _con
    if _con is None:
        _con = snowflake.connector.connect(
            account=_require("SNOWFLAKE_ACCOUNT"),
            user=_require("SNOWFLAKE_USER"),
            private_key=_private_key_der(_require("SNOWFLAKE_PRIVATE_KEY_PATH")),
            role=_require("SNOWFLAKE_ROLE"),
            warehouse=_require("SNOWFLAKE_WAREHOUSE"),
            database=_require("SNOWFLAKE_DATABASE"),
            schema=_require("SNOWFLAKE_SCHEMA"),
            autocommit=True,
        )
    return _con


def close() -> None:
    global _con
    if _con is not None:
        _con.close()
        _con = None


def reset_budget() -> None:
    """Called at the start of a run so the cap is per question, not per process."""
    global _queries_run
    _queries_run = 0


def _is_read_only(q: str) -> str | None:
    """Return an error string if the statement is not a single read, else None."""
    stripped = q.strip().rstrip(";").strip()
    if not stripped:
        return "ERROR: empty query."
    # A trailing semicolon is fine and stripped above. An *interior* one means
    # more than one statement was submitted - reject rather than hope the
    # driver refuses it, since "select 1; drop table t" passes a naive
    # startswith() check.
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


def run_sql(query: str) -> str:
    """Run one read-only SELECT against the analytics schema and return rows as text.

    Errors are returned as a string rather than raised, so the model reads the
    warehouse's own error message and corrects its SQL on the next turn instead
    of the run dying.
    """
    global _queries_run

    bad = _is_read_only(query)
    if bad:
        return bad

    if _queries_run >= MAX_QUERIES:
        return (
            f"ERROR: query budget of {MAX_QUERIES} reached for this question. "
            "Answer with what you already have, and say which part is unanswered."
        )

    q = query.strip().rstrip(";").strip()
    try:
        cur = _connect().cursor()
        _queries_run += 1
        cur.execute(q)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchmany(MAX_ROWS)
        cur.close()
    except Exception as e:  # returned to the model, not raised
        return f"SQL ERROR: {e}"

    if not rows:
        return "(0 rows)"

    out = [" | ".join(cols)]
    out += [" | ".join("NULL" if v is None else str(v) for v in r) for r in rows]
    if len(rows) == MAX_ROWS:
        out.append(
            f"(truncated at {MAX_ROWS} rows - re-run with an aggregate, a tighter "
            "filter, or an explicit LIMIT rather than assuming this is everything)"
        )
    return "\n".join(out)


def get_schema() -> str:
    """List the tables and columns the agent is allowed to read.

    Driven off information_schema rather than a hardcoded list, so a new mart
    shows up here the moment the role is granted SELECT on it - and a mart the
    role cannot read never appears, which keeps the tool description honest.
    """
    # No defaults: hardcoding the real database and schema would publish the
    # account's layout, and a silent fallback to the wrong one is worse than a
    # loud failure.
    schema = _require("SNOWFLAKE_SCHEMA")
    db = _require("SNOWFLAKE_DATABASE")
    sql = f"""
        select t.table_name, t.table_type, t.row_count,
               listagg(c.column_name || ' ' || c.data_type, ', ')
                 within group (order by c.ordinal_position) as cols
        from {db}.information_schema.tables t
        join {db}.information_schema.columns c
          on c.table_schema = t.table_schema and c.table_name = t.table_name
        where t.table_schema = '{schema}'
        group by t.table_name, t.table_type, t.row_count
        order by t.table_name
    """
    try:
        cur = _connect().cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        return f"SCHEMA ERROR: {e}"

    if not rows:
        return f"No readable tables in {db}.{schema}."

    out = [f"{db}.{schema} - readable objects:"]
    for name, kind, n, cols in rows:
        count = f", {n} rows" if n is not None else ""
        out.append(f"\n{name} ({kind.lower()}{count})\n  {cols}")
    return "\n".join(out)


TOOL_SCHEMAS = [
    {
        "name": "get_schema",
        "description": (
            "List every table and view you can read, with column names and types. "
            "Call this first, before writing any SQL, so your query references real "
            "columns rather than guessed ones."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_sql",
        "description": (
            "Run one read-only SELECT against the analytics schema and get the rows "
            "back as text. This is the only way to obtain a number - every figure you "
            "state must come from a result this tool returned in this conversation.\n\n"
            "Call it when: the user asks anything about prices, returns, volatility, "
            "drawdown, sectors, or the watchlist; you need the data's date range; or "
            "you want to check a value before asserting it.\n\n"
            "Constraints: one statement per call, SELECT or WITH only, "
            f"{MAX_ROWS} rows maximum returned, {MAX_QUERIES} calls per question. "
            "Prefer an aggregate over pulling rows and reasoning across them. "
            "Errors come back as text starting with 'SQL ERROR:' - read the message "
            "and fix the query rather than repeating it unchanged."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A single SELECT or WITH statement. No trailing semicolon needed.",
                }
            },
            "required": ["query"],
        },
    },
]


def dispatch(name: str, args: dict) -> str:
    if name == "get_schema":
        return get_schema()
    if name == "run_sql":
        return run_sql(args.get("query", ""))
    return f"ERROR: unknown tool {name!r}."
