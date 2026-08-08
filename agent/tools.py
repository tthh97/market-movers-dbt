"""
Tools the agent can call: get_schema and run_sql.

The read-only guard and the warehouse connection now live in agent/query.py
(QueryRunner), shared with the report and dashboard paths. This module keeps
what is specific to the agent's tool interface: the per-question query budget,
the Snowflake schema listing, and the tool schemas Claude sees.

The guardrails still cannot be bypassed. run_sql goes through QueryRunner, whose
read-only check runs inside run() itself rather than in a wrapper a caller could
forget. And the agent connects as a role with USAGE + SELECT and nothing else -
the layer that actually matters. As that service user and role, CREATE TABLE,
DELETE, reading the raw landing schema, and USE ROLE ACCOUNTADMIN all fail with
access-control errors; the budget and the statement-shape check exist so obvious
mistakes fail without waking the warehouse at all.
"""

from __future__ import annotations

import query

MAX_ROWS = query.DEFAULT_ROW_LIMIT   # rows returned to the model per query
MAX_QUERIES = 12                     # per question; see the budget below

_runner = None
_queries_run = 0


def _get_runner() -> "query.QueryRunner":
    """One read-only Snowflake runner per process, opened lazily and reused."""
    global _runner
    if _runner is None:
        _runner = query.QueryRunner("snowflake")
    return _runner


def close() -> None:
    global _runner
    if _runner is not None:
        _runner.close()
        _runner = None


def reset_budget() -> None:
    """Called at the start of a run so the cap is per question, not per process."""
    global _queries_run
    _queries_run = 0


def run_sql(query_text: str) -> str:
    """Run one read-only SELECT against the analytics schema and return rows as text.

    Errors are returned as a string rather than raised, so the model reads the
    warehouse's own error message and corrects its SQL on the next turn instead
    of the run dying.
    """
    global _queries_run

    # Guarded here before the budget is charged, so a malformed query does not
    # consume the allowance; run() guards again, which is what makes the check
    # impossible to bypass.
    bad = query.read_only_error(query_text)
    if bad:
        return bad

    if _queries_run >= MAX_QUERIES:
        return (
            f"ERROR: query budget of {MAX_QUERIES} reached for this question. "
            "Answer with what you already have, and say which part is unanswered."
        )

    _queries_run += 1
    return query.render_for_model(_get_runner().run(query_text, limit=MAX_ROWS))


def get_schema() -> str:
    """List the tables and columns the agent is allowed to read.

    Driven off information_schema rather than a hardcoded list, so a new mart
    shows up here the moment the role is granted SELECT on it - and a mart the
    role cannot read never appears, which keeps the tool description honest.
    """
    # No defaults: hardcoding the real database and schema would publish the
    # account's layout, and a silent fallback to the wrong one is worse than a
    # loud failure.
    schema = query.require("SNOWFLAKE_SCHEMA")
    db = query.require("SNOWFLAKE_DATABASE")
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
    result = _get_runner().run(sql, limit=None)
    if result.is_error:
        # Same message the warehouse gave, labelled for the schema tool.
        return result.error.replace("SQL ERROR:", "SCHEMA ERROR:", 1)
    if not result.rows:
        return f"No readable tables in {db}.{schema}."

    out = [f"{db}.{schema} - readable objects:"]
    for name, kind, n, cols in result.rows:
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
