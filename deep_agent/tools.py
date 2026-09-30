"""
Tools for the deep agent: get_schema and run_sql (warehouse-analyst only), and
render_bar_chart (the coordinator).

The warehouse access goes through agent/query.py's QueryRunner, the same seam the
chat agent, the weekly report, and the dashboard use. Its read-only guard runs
inside run() itself, so nothing here can reach the warehouse a way that skips it.
On Snowflake the connection is the read-only REPORTER identity from agent/.env;
offline it is market.duckdb opened read_only=True.

What is specific to this module:

- The query budget is per delegation, not per process. A deep agent runs many
  threads in one server, so a module-level counter would leak between them.
  Instead run_sql counts its own earlier calls in the calling agent's messages.
  Each `task` delegation starts the subagent with a fresh message list, so the
  count restarts for every question the coordinator hands over.
- render_bar_chart takes data, never code, so there is no execution surface to
  secure. It writes only into workspace/outputs/.
"""

from __future__ import annotations

import os
import re
import sys

from langchain.tools import ToolRuntime, tool

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(os.path.dirname(HERE), "agent")
OUTPUTS_DIR = os.path.join(HERE, "workspace", "outputs")

if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)
import query  # noqa: E402  (agent/query.py, the shared read-only seam)

MAX_ROWS = query.DEFAULT_ROW_LIMIT   # rows returned to the model per query
MAX_QUERIES = 12                     # per delegation, same cap as the chat agent

ENGINE = os.environ.get("DEEP_AGENT_ENGINE", "snowflake").strip().lower()

_runner: query.QueryRunner | None = None


def _get_runner() -> query.QueryRunner:
    """One read-only runner per process, opened lazily and reused."""
    global _runner
    if _runner is None:
        _runner = query.QueryRunner(ENGINE)
    return _runner


def _prior_calls(runtime: ToolRuntime, name: str) -> int:
    """How many times the calling agent called `name` in this delegation.

    The current call's AIMessage is already in state, so it is counted too.
    """
    n = 0
    for msg in runtime.state.get("messages", []):
        for call in getattr(msg, "tool_calls", None) or []:
            if call.get("name") == name:
                n += 1
    return n


@tool
def run_sql(query_text: str, runtime: ToolRuntime) -> str:
    """Run one read-only SELECT against the analytics marts and return rows as text.

    This is the only way to obtain a number. Every figure you report must come
    from a result this tool returned. One statement per call, SELECT or WITH
    only, 50 rows returned at most, 12 calls per task. Prefer an aggregate over
    pulling rows and reasoning across them. Errors come back as text starting
    with 'SQL ERROR:' - read the message and fix the query rather than repeating
    it unchanged.
    """
    # Guarded before the budget is charged, so a malformed query does not use
    # up the allowance. QueryRunner.run() guards again, which is what makes the
    # check impossible to bypass.
    bad = query.read_only_error(query_text)
    if bad:
        return bad
    if _prior_calls(runtime, "run_sql") > MAX_QUERIES:
        return (
            f"ERROR: query budget of {MAX_QUERIES} reached for this task. Answer "
            "with what you already have, and say which part is unanswered."
        )
    return query.render_for_model(_get_runner().run(query_text, limit=MAX_ROWS))


def _schema_sql() -> str:
    if ENGINE == "duckdb":
        # The offline build keeps every model in the default schema. Only the
        # marts and the fact table are listed, so the offline agent sees the
        # same shape of schema REPORTER sees on Snowflake.
        return """
            select c.table_name,
                   string_agg(c.column_name || ' ' || c.data_type, ', '
                              order by c.ordinal_position) as cols
            from information_schema.columns c
            where c.table_schema = 'main'
              and (c.table_name like 'mart_%' or c.table_name like 'fct_%')
            group by c.table_name
            order by c.table_name
        """
    # No defaults: a silent fallback to the wrong database or schema is worse
    # than a loud failure.
    db = query.require("SNOWFLAKE_DATABASE")
    schema = query.require("SNOWFLAKE_SCHEMA")
    return f"""
        select c.table_name,
               listagg(c.column_name || ' ' || c.data_type, ', ')
                 within group (order by c.ordinal_position) as cols
        from {db}.information_schema.columns c
        where c.table_schema = '{schema}'
        group by c.table_name
        order by c.table_name
    """


@tool
def get_schema() -> str:
    """List every table and view you can read, with column names and types.

    Driven off information_schema, so it only ever shows what the read-only
    role can actually select. Call it before writing SQL against a table whose
    columns are not already in your memory.
    """
    result = _get_runner().run(_schema_sql(), limit=None)
    if result.is_error:
        return result.error.replace("SQL ERROR:", "SCHEMA ERROR:", 1)
    if not result.rows:
        return "No readable tables."
    return "\n\n".join(f"{name}\n  {cols}" for name, cols in result.rows)


_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}\.png$")


@tool
def render_bar_chart(
    title: str, labels: list[str], values: list[float], filename: str, value_label: str = ""
) -> str:
    """Render a horizontal bar chart to /outputs/<filename> and return its path.

    Pass the labels and values exactly as run_sql returned them - this tool
    draws, it does not compute. `filename` must be lowercase letters, digits,
    '-' or '_', ending in .png. Negative values are drawn left of zero.
    """
    if not _SAFE_NAME.match(filename):
        return "ERROR: filename must match [a-z0-9_-]+.png, e.g. sector-returns-2026-09-14.png"
    if len(labels) != len(values) or not labels:
        return "ERROR: labels and values must be non-empty and the same length."

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Matplotlib draws negative ticks with a Unicode minus by default; the
    # house style everywhere in this repo is a plain hyphen.
    plt.rcParams["axes.unicode_minus"] = False

    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    order = sorted(range(len(values)), key=lambda i: values[i])
    ys = [labels[i] for i in order]
    xs = [values[i] for i in order]
    colors = ["#c0392b" if v < 0 else "#2e7d5b" for v in xs]

    fig, ax = plt.subplots(figsize=(7.5, 0.42 * len(xs) + 1.4), dpi=150)
    ax.barh(ys, xs, color=colors)
    ax.axvline(0, color="#555", linewidth=0.8)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    if value_label:
        ax.set_xlabel(value_label)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUTS_DIR, filename))
    plt.close(fig)
    return f"/outputs/{filename}"
