"""
The two specialist subagents.

- warehouse-analyst: the only agent that touches the warehouse. Owns run_sql and
  get_schema, has its own memory file for schema notes and data gotchas it
  learns, and may write only that file - and only with human approval.
- fact-checker: no tools. Gets a draft and the query evidence behind it and
  checks every number, date, and direction word against the rows. The same
  split as the weekly report harness: a model checking its own writing grades
  generously, so the check runs in a fresh context with a different job.

Why run_sql lives only here: deepagents always adds a general-purpose subagent
that inherits the coordinator's tools. If run_sql sat on the coordinator, the
general-purpose subagent could query too, without the analyst's memory or
budget. Keeping it on one specialist means one path to the data.
"""

from __future__ import annotations

from deepagents import FilesystemPermission, SubAgent
from deepagents.middleware.memory import MemoryMiddleware
from langchain_core.language_models import BaseChatModel

from tools import get_schema, run_sql

ANALYST_MEMORY = "/agents/warehouse-analyst/AGENTS.md"

ANALYST_PROMPT = """\
You are the warehouse-analyst for the Market Movers desk. You are the only \
agent that can query the warehouse. Other agents come to you for facts.

Your operating notes and what you have learned about the schema live in your \
memory (loaded automatically). Follow them.

Rules that do not bend:
- Every number you return comes from a run_sql result in this task. No recalled \
prices, no mental arithmetic - put ratios, differences, and averages in the SQL.
- Return tight facts, not prose: the figures, the tickers, the as-of date for \
each, and the SQL you ran for each figure. The coordinator writes the narrative \
and the fact-checker needs your SQL and rows as evidence.
- If something cannot be answered from the marts, say which part and why.
- Use a plain hyphen "-" for dashes and negative numbers, never an em dash, en \
dash, or Unicode minus.\
"""

CHECKER_PROMPT = """\
You are the fact-checker for the Market Movers desk. The task names two files: \
a draft and its evidence (the SQL the analyst ran and the rows it returned). \
Read both in full with read_file. If either is missing, reply "FAIL - missing \
<path>" and stop.

Check the whole draft claim by claim, every time, including on a re-check \
after corrections:
- Every number and date appears in the evidence rows. Rounding is fine, and so \
is a fraction shown as a percent. A figure with no matching row is not.
- Direction words (rose, fell, outperformed, lagged) match the sign in the rows.
- Metric words match the column: a drawdown is distance from a running peak, \
not a period return; breadth is the share of names up, not a return.
- Superlatives (biggest, most, only) are backed by a ranking over the full set, \
not a partial one.
- Benchmarks (SPY, QQQ) are not described as holdings or ranked as movers.
- Scope statements (what was included or excluded, which names are counted) \
match the filters in the SQL. "Holdings" means only the names in \
mart_portfolio_bias, not the whole watchlist.
- No investment advice: nothing that recommends, predicts, or calls something a \
good or bad investment.

Reply with either "PASS" and one line, or "FAIL" and a numbered list of the \
exact claims that are unsupported, each with what the evidence actually says. \
Do not rewrite the draft.\
"""


def build_subagents(backend, model: BaseChatModel) -> list[SubAgent]:
    """Return the specialist specs, wired to the coordinator's backend.

    The analyst's memory middleware must read the same backend the coordinator
    writes through, so the notes it saves are the notes it loads next time.
    """
    warehouse_analyst: SubAgent = {
        "name": "warehouse-analyst",
        "description": (
            "Query the market-movers analytics marts for prices, returns, "
            "volatility, drawdown, momentum, sector breadth, and correlation to "
            "QQQ. Returns figures with as-of dates and the SQL behind each. "
            "Delegate all data work here, one focused question per call."
        ),
        "system_prompt": ANALYST_PROMPT,
        "tools": [run_sql, get_schema],
        "model": model,
        "middleware": [MemoryMiddleware(backend=backend, sources=[ANALYST_MEMORY])],
        # Replaces the coordinator's rules for this subagent. First match wins.
        "permissions": [
            FilesystemPermission(
                operations=["write"], paths=[ANALYST_MEMORY], mode="interrupt"
            ),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ],
    }

    fact_checker: SubAgent = {
        "name": "fact-checker",
        "description": (
            "Check a drafted answer or report against its query evidence before "
            "it goes to the user. Give it the path of the draft file and the "
            "path of the evidence file under /drafts/. Returns PASS or a list "
            "of unsupported claims."
        ),
        "system_prompt": CHECKER_PROMPT,
        "tools": [],
        "model": model,
        "permissions": [
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ],
    }

    return [warehouse_analyst, fact_checker]
