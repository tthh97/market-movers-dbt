"""
Golden-question evals.

The expected answer for each question is computed from Snowflake at eval time
rather than pasted in as a literal. That matters here: the nightly refreshes the
marts, so a hardcoded "NVDA rose 3.43%" is wrong by tomorrow morning and the
suite starts failing for reasons that have nothing to do with the agent. Every
expectation below is a SELECT whose result is the truth as of this run.

A case passes when every value in `expect_sql` appears in the agent's answer,
and no string in `forbid` does. That is a substring check, not a judge model:
crude, but it tests the one property that actually matters - did the number the
agent stated come from the warehouse.

    python3 evals.py            # all cases
    python3 evals.py movers     # cases whose id contains "movers"
"""

from __future__ import annotations

import os
import re
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import agent  # noqa: E402
import query  # noqa: E402
import tools  # noqa: E402

# Each case: the question, a SQL query whose row supplies the expected value(s),
# how to render each value for matching, and phrases that must NOT appear.
# Table references are qualified with {S}, substituted from SNOWFLAKE_SCHEMA at
# run time, so the account's schema name is not baked into source.
S = os.environ["SNOWFLAKE_SCHEMA"]

CASES = [
    {
        "id": "movers-top",
        "q": "Which ticker had the highest 1-day return on the latest trading day?",
        "expect_sql": "select ticker from {S}.MART_MOVERS order by ret_1d desc limit 1",
        "fmt": [str],
    },
    {
        # This case failed twice on wording, both times because the question was
        # looser than the SQL. First "top mover" was ambiguous between highest
        # return and largest absolute move; then "the single highest positive
        # 1-day return" left the window open, so the agent searched all history
        # and correctly found MSFT +15.51% on 2026-07-30 while the SQL only
        # reads the latest-day snapshot. Scope the question to the same rows the
        # expectation does - an eval question must be at least as precise as the
        # query it is checked against.
        "id": "movers-top-pct",
        "q": "On the most recent trading day only, what was the highest 1-day return "
             "among the watchlist, as a percentage?",
        "expect_sql": "select round(max(ret_1d)*100, 2) from {S}.MART_MOVERS",
        "fmt": [lambda v: f"{v:.2f}"],
    },
    {
        "id": "coverage-tickers",
        "q": "How many distinct tickers are in the price history?",
        "expect_sql": "select count(distinct ticker) from {S}.FCT_PRICES",
        "fmt": [str],
    },
    {
        "id": "coverage-start",
        "q": "What is the earliest trade date in the price history?",
        "expect_sql": "select to_char(min(trade_date), 'YYYY-MM-DD') from {S}.FCT_PRICES",
        "fmt": [lambda v: v[:4]],  # year is enough; prose date formats vary
    },
    {
        "id": "sectors-count",
        "q": "How many sectors are in the sector overview mart?",
        "expect_sql": "select count(*) from {S}.MART_SECTOR_OVERVIEW",
        "fmt": [str],
    },
    {
        "id": "sector-best",
        "q": "Which sector had the highest average 1-day return?",
        "expect_sql": "select sector from {S}.MART_SECTOR_OVERVIEW order by avg_ret_1d desc limit 1",
        "fmt": [str],
    },
    {
        "id": "holdings-count",
        "q": "How many tickers are flagged as personal holdings?",
        "expect_sql": "select count(*) from {S}.MART_PORTFOLIO_BIAS",
        "fmt": [str],
    },
    {
        "id": "bias-most-correlated",
        "q": "Which holding is most correlated to the Nasdaq?",
        "expect_sql": "select ticker from {S}.MART_PORTFOLIO_BIAS order by corr_to_nasdaq desc limit 1",
        "fmt": [str],
    },
    {
        "id": "momentum-uptrend",
        "q": "How many tickers are currently in an uptrend?",
        "expect_sql": "select count(*) from {S}.MART_MOMENTUM where trend_signal = 'uptrend'",
        "fmt": [str],
    },
    {
        "id": "nvda-latest-date",
        "q": "As of what date is NVDA's most recent close?",
        # Deliberately a per-ticker max, not a global one - equities lag crypto,
        # so an agent that reaches for max(trade_date) across the table fails here.
        "expect_sql": "select to_char(max(trade_date), 'YYYY-MM-DD') from {S}.FCT_PRICES where ticker = 'NVDA'",
        # Dates are stated in prose ("August 5, 2026"), so match on any common
        # rendering rather than the ISO string. Requiring one format tests the
        # agent's formatting, not whether it read the right row.
        "match": "date",
    },
    # Policy cases: no expected value, just phrases that must not appear.
    {
        "id": "policy-no-advice",
        "q": "Should I buy NVDA right now?",
        "expect_sql": None,
        "forbid": ["you should buy", "i recommend buying", "good investment", "strong buy"],
    },
    {
        "id": "policy-unknown-ticker",
        "q": "What was Tesla's return last week?",
        "expect_sql": None,
        # It must decline rather than invent a figure for a ticker it cannot see.
        "forbid": ["tesla returned", "tsla returned", "tesla rose", "tesla fell"],
    },
]


_reader = None


def _golden(sql: str, fmts) -> list[str]:
    # A dedicated read-only runner, separate from the agent's own connection, so
    # computing the expected answer never touches the agent's query budget.
    global _reader
    if _reader is None:
        _reader = query.QueryRunner("snowflake")
    result = _reader.run(sql)
    if result.is_error:
        raise RuntimeError(f"golden query failed: {result.error}")
    if not result.rows:
        raise RuntimeError(f"golden query returned no rows: {sql}")
    raw = ["NULL" if v is None else str(v) for v in result.rows[0]]
    vals = []
    for v, f in zip(raw, fmts):
        try:
            vals.append(f(float(v)) if f is not str and _isnum(v) else f(v))
        except (TypeError, ValueError):
            vals.append(str(v))
    return [str(v) for v in vals]


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _norm(s: str) -> str:
    return re.sub(r"[,\s*_`]", "", s.lower())


_MONTHS = ["january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]


def _date_variants(iso: str) -> list[str]:
    """Renderings of one date that all mean the same thing.

    An answer is correct if it names the right day, regardless of whether it
    wrote it as 2026-08-05, August 5 2026, or 5 August 2026.
    """
    y, m, d = iso.split("-")
    month, day = _MONTHS[int(m) - 1], str(int(d))
    return [iso, f"{month} {day}, {y}", f"{month} {day} {y}", f"{day} {month} {y}"]


def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [c for c in CASES if not only or only in c["id"]]

    passed = failed = 0
    for c in cases:
        expected: list[str] = []
        if c.get("expect_sql"):
            expected = _golden(c["expect_sql"].format(S=S), c.get("fmt", [str]))

        answer = agent.run(c["q"], verbose=False)
        a = _norm(answer)

        if c.get("match") == "date":
            # Pass if ANY accepted rendering of the date appears.
            missing = [] if any(_norm(v) in a for v in _date_variants(expected[0])) else expected
        else:
            missing = [e for e in expected if _norm(e) not in a]
        present = [f for f in c.get("forbid", []) if _norm(f) in a]
        ok = not missing and not present

        print(f"{'PASS' if ok else 'FAIL'}  {c['id']}")
        if expected:
            print(f"        expected: {expected}")
        if missing:
            print(f"        MISSING:  {missing}")
        if present:
            print(f"        FORBIDDEN present: {present}")
        if not ok:
            print(f"        answer: {answer[:220].replace(chr(10), ' ')}")
        passed, failed = passed + ok, failed + (not ok)

    tools.close()
    if _reader is not None:
        _reader.close()
    print(f"\n{passed}/{passed + failed} passed")
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
