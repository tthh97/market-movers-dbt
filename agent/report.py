"""
The weekly report agent: writer -> deterministic matcher -> verifier -> gate.

Same newsroom as agent.py, different job. agent.py is the on-call researcher;
this is the weekly column, with a journalist and a fact-checker inside one
program. Two Claude calls with two different job descriptions, run in
sequence - "subagent" sounds grand, but this is all it is.

  1. WRITER   - drafts the weekly narrative. It has the same read-only
                run_sql tool as the researcher, and every query it runs plus
                the actual rows that came back are kept in an evidence
                notebook, like a journalist's file of sources.
  2. MATCHER  - ordinary code (claims.py). Every number and date in the
                draft must trace to the notebook. Code checks what code can
                check; no model grades its own arithmetic.
  3. VERIFIER - a second Claude call that never saw the draft being written.
                It gets the draft plus the notebook and one job: claim by
                claim, is each direction word and superlative supported by
                the rows? Fresh eyes, no attachment - the same reason a
                second analyst reviews dashboard numbers before they go out.
  4. GATE     - ordinary code (claims.py). Any unsupported claim blocks
                publication. One rewrite loop, then it flags for a human.
                The rule is mechanical, the way a failing dbt test blocks a
                build. Fabrication is structurally unable to reach the
                reader.

Targets:

  REPORT_TARGET=snowflake (default) - reuses agent/tools.py: same read-only
      role, same statement-shape guardrails, same query budget.
  REPORT_TARGET=duckdb - local demo with zero credentials against the repo's
      market.duckdb, opened with read_only=True so the read-only property is
      enforced by the engine, not promised by the prompt.

Usage:
    python3 report.py                                  # Snowflake
    REPORT_TARGET=duckdb python3 report.py             # offline demo
    python3 report.py --inject-fault fabricated_number # prove the gate works

Fault injection mirrors scripts/inject_fault.py: the fault goes into the
draft after writing and before checking, so a green gate on an injected
fault would be visible, loud evidence the harness is broken.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from dotenv import load_dotenv

import claims
import policy

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 4096
WRITER_MAX_TURNS = 10

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(os.path.dirname(HERE), "reports")

# ---------------------------------------------------------------------------
# Warehouse backends. Snowflake reuses tools.py wholesale; DuckDB reimplements
# only the connection, borrowing the same statement-shape check so both paths
# refuse the same queries.
# ---------------------------------------------------------------------------


class _SnowflakeBackend:
    def __init__(self):
        import tools  # heavy import (snowflake connector), so done lazily
        self._tools = tools
        tools.reset_budget()

    def run_sql(self, query: str) -> str:
        return self._tools.run_sql(query)

    def get_schema(self) -> str:
        return self._tools.get_schema()

    def close(self) -> None:
        self._tools.close()


class _DuckDBBackend:
    """Read-only DuckDB over the repo's market.duckdb, via the shared QueryRunner.

    read_only=True (inside QueryRunner) is the layer that matters here - the
    engine itself refuses writes, the offline counterpart to the Snowflake
    grants. The statement-shape guard is the same one the Snowflake path uses,
    because both now run through QueryRunner.
    """

    def __init__(self):
        import query
        self._runner = query.QueryRunner("duckdb")
        self._render = query.render_for_model

    def run_sql(self, q: str) -> str:
        return self._render(self._runner.run(q))

    def get_schema(self) -> str:
        return self.run_sql(
            "select table_name, string_agg(column_name || ' ' || data_type, ', '"
            " order by ordinal_position) as cols"
            " from information_schema.columns"
            " where table_schema = 'main' group by table_name order by table_name")

    def close(self) -> None:
        self._runner.close()


def _backend():
    target = os.environ.get("REPORT_TARGET", "snowflake").lower()
    if target == "duckdb":
        return _DuckDBBackend()
    return _SnowflakeBackend()


# ---------------------------------------------------------------------------
# 1. The writer.
# ---------------------------------------------------------------------------

WRITER_PROMPT = policy.SYSTEM_PROMPT + """

## This task: the weekly column

Write this week's market narrative for the watchlist: 3-5 short paragraphs a
reader skims in two minutes. Cover the week's top and bottom movers, sector
performance, and anything notable in momentum or drawdown. Query the marts
for everything; a figure you did not query is a figure you may not use. When
you are done querying, produce ONLY the report text - no preamble, no
sign-off. State the date range the report covers, taken from the data.

Attach the time window to each figure whenever a paragraph mentions more than
one - "fell 6.2% on the week", not a bare "fell 6.2%" three clauses after the
last mention of the week. Every claim is fact-checked one at a time, and an
unqualified number sitting beside a mix of windows is ambiguous to the checker
even when it is correct.

Before you call anything the best, worst, biggest, strongest or weakest, order
by that exact column in SQL and read the top row. Do not carry a ranking across
columns: the leader on a one-day return is routinely not the leader on a
five-day one, and a superlative inferred from a query sorted by the wrong
column is the single most common way this report gets a fact wrong.

Re-read the finished draft once before you hand it over. If two sentences
disagree with each other - a sector called the strongest in one paragraph while
another sector's figure two sentences later is plainly higher - fix it. An
internal contradiction is a fact error you can find without any help, and the
fact-checker will find it if you do not.
"""


def write_draft(backend, feedback: str | None = None,
                notebook: list[dict] | None = None) -> tuple[str, list[dict]]:
    """One writer run. Returns (draft, notebook). On the rewrite pass the
    previous notebook is carried forward - evidence already gathered is still
    evidence - and the failures are the first user message."""
    import anthropic
    import tools as tool_schemas

    client = anthropic.Anthropic()
    notebook = list(notebook or [])

    user = ("Write this week's market report."
            if feedback is None else
            "Your previous draft failed fact-checking. Failures:\n\n" + feedback +
            "\n\nRewrite the report. Fix every failed claim using query results - "
            "rerun or add queries if needed. Remove any claim you cannot support. "
            "Output only the corrected report text: no preamble, and no note "
            "about what you changed.")
    messages = [{"role": "user", "content": user}]

    draft = ""
    for _ in range(WRITER_MAX_TURNS):
        resp = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=WRITER_PROMPT, tools=tool_schemas.TOOL_SCHEMAS,
            messages=messages)
        if resp.stop_reason != "tool_use":
            draft = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            # House punctuation, applied before the matcher runs. Safe in this
            # order: claims.py compares absolute values, so turning a Unicode
            # minus into a hyphen only helps it find the number.
            draft = claims.normalize_punctuation(draft)
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "run_sql":
                q = block.input.get("query", "")
                out = backend.run_sql(q)
                notebook.append({"query": q, "result": out})
                print(f"  [writer run_sql] {q[:100]}", file=sys.stderr)
            else:
                out = backend.get_schema()
                print("  [writer get_schema]", file=sys.stderr)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": out,
                            "is_error": out.startswith(("ERROR:", "SQL ERROR:",
                                                        "SCHEMA ERROR:"))})
        messages.append({"role": "user", "content": results})
    return draft, notebook


# ---------------------------------------------------------------------------
# 3. The verifier. No tools, no memory of the draft being written: only the
# draft, the notebook, and one job.
# ---------------------------------------------------------------------------

VERIFIER_PROMPT = """\
You are a fact-checker. You will receive a draft market report and an evidence
notebook: the exact SQL queries that were run and the exact rows they
returned. The notebook is the ONLY source of truth. Your own market knowledge
is out of bounds; if the notebook does not show it, it is not supported.

Go through the draft claim by claim. A claim is any checkable assertion:
every direction word (rose, fell, led, lagged, gained, dropped), every
superlative or ranking (biggest, worst, top, most), every attribution of a
number to an entity, every date-range statement.

Read each claim in the context of the sentence and paragraph it sits in. A
figure inherits the time window its sentence established: in "over the five
days, MSFT dropped 5.9% and AAPL shed 5.4%", both figures are five-day returns
and must be checked against the five-day column, not the one-day one. A claim
judged as an isolated fragment, against whichever column happens to be nearest,
produces false contradictions - and a false contradiction is as damaging as a
missed one, because it blocks a report that was correct.

For each claim, give a verdict:
  - "supported":    the notebook rows show it
  - "contradicted": the notebook rows show otherwise
  - "not_found":    the notebook contains no evidence either way

If a claim is genuinely ambiguous about which window or column it means, and
the surrounding text does not settle it, that is "not_found" - not
"contradicted". Both block publication, so this costs you nothing; it just
keeps the reason accurate for whoever reads the failure report.

Ground every verdict in a specific row. In "evidence", name the query number,
the column and the exact value you compared against - "q4, avg_ret_5d, crypto
0.0197 > tech 0.0113". If you cannot point at a specific value, you have not
checked the claim, and the verdict is "not_found" rather than "contradicted".

Reply with ONLY a JSON object, no other text:
{"claims": [{"text": "<claim as written>", "kind": "direction|superlative|number|other",
             "verdict": "supported|contradicted|not_found",
             "evidence": "<query number, column and value that decides it>"}]}
"""


def verify(draft: str, notebook: list[dict]) -> list[dict] | None:
    """Returns the verifier's claim list, or None if it could not run or its
    output could not be parsed. None fails the gate - fail closed."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("verifier: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return None
    import anthropic

    evidence = "\n\n".join(
        f"--- query {i + 1} ---\n{e['query']}\n--- result ---\n{e['result']}"
        for i, e in enumerate(notebook))
    try:
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=VERIFIER_PROMPT,
            messages=[{"role": "user", "content":
                       f"DRAFT:\n{draft}\n\nEVIDENCE NOTEBOOK:\n{evidence}"}])
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group())["claims"] if m else None
    except Exception as e:
        print(f"verifier failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Fault injection - the gate's own demo, mirroring scripts/inject_fault.py.
# ---------------------------------------------------------------------------

def inject_fault(draft: str, fault: str) -> str:
    """Corrupt the draft between writing and checking. Exits non-zero if the
    corruption could not be applied - a silent no-op would look like the gate
    missed a fault that was never injected."""
    if fault == "fabricated_number":
        new = draft + ("\n\nSeparately, the watchlist's aggregate dividend yield "
                       "reached 47.3% this week.")
        return new
    if fault == "flipped_direction":
        # Wide on purpose. The writer picks its own verbs, and a narrow list
        # meant the demo could not find a word to flip and aborted - which
        # looked like a broken harness rather than what it was, an unlucky
        # draft. The exit-3 guard below stays: a fault that silently fails to
        # inject would show a green gate and read as a miss.
        for a, b in (("rose", "fell"), ("fell", "rose"),
                     ("gained", "lost"), ("lost", "gained"),
                     ("climbed", "slid"), ("slid", "climbed"),
                     ("advanced", "declined"), ("declined", "advanced"),
                     ("dropped", "jumped"), ("jumped", "dropped"),
                     ("led", "lagged"), ("lagged", "led"),
                     ("strongest", "weakest"), ("weakest", "strongest"),
                     ("higher", "lower"), ("lower", "higher"),
                     ("above", "below"), ("below", "above"),
                     ("dragged", "lifted"), ("lifted", "dragged"),
                     ("best", "worst"), ("worst", "best"),
                     ("up", "down"), ("down", "up")):
            if re.search(rf"\b{a}\b", draft):
                return re.sub(rf"\b{a}\b", b, draft, count=1)
        print("inject_fault: no direction word found to flip", file=sys.stderr)
        raise SystemExit(3)
    print(f"inject_fault: unknown fault {fault!r}", file=sys.stderr)
    raise SystemExit(3)


# ---------------------------------------------------------------------------
# The run: write -> check -> (rewrite once) -> publish or flag.
# ---------------------------------------------------------------------------

def run(fault: str | None = None) -> int:
    backend = _backend()
    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")

    try:
        draft, notebook = write_draft(backend)
        if fault:
            draft = inject_fault(draft, fault)
            print(f"fault injected: {fault}", file=sys.stderr)

        for attempt in (1, 2):
            matcher_failures = claims.check_draft(draft, notebook)
            verifier_claims = verify(draft, notebook)
            decision = claims.gate_decision(matcher_failures, verifier_claims)

            if decision["publish"]:
                path = os.path.join(REPORTS_DIR, f"{stamp}-weekly-report.md")
                appendix = "\n".join(
                    f"{i + 1}. `{e['query'].strip()}`"
                    for i, e in enumerate(notebook))
                # The target is recorded in the file, not just in the log,
                # because the dashboard reads these reports and must never put
                # a Snowflake narrative on top of synthetic charts. A consumer
                # that has to guess the provenance will eventually guess wrong.
                target = os.environ.get("REPORT_TARGET", "snowflake").lower()
                with open(path, "w") as f:
                    f.write(f"# Weekly market report - {stamp}\n\n"
                            f"<!-- generated-from: {target} -->\n\n{draft}\n\n"
                            f"---\n\n*Every figure above was checked against "
                            f"the queries below; publication is blocked "
                            f"mechanically if any check fails. Source: {target}.*\n\n"
                            f"## Evidence: queries run\n\n{appendix}\n")
                print(f"PUBLISHED {path} "
                      f"(attempt {attempt}, {len(notebook)} queries, "
                      f"{len(verifier_claims)} claims verified)")
                return 0

            print(f"gate BLOCKED publication (attempt {attempt}):", file=sys.stderr)
            for r in decision["reasons"]:
                print(f"  - {r}", file=sys.stderr)

            if attempt == 1 and not fault:
                feedback = "\n".join(f"- {r}" for r in decision["reasons"])
                draft, notebook = write_draft(backend, feedback, notebook)
            else:
                # An injected fault must never be "fixed" into a publish -
                # the demo is that it gets caught, not repaired.
                break

        failure = {"date": stamp, "draft": draft,
                   "reasons": decision["reasons"],
                   "notebook_queries": [e["query"] for e in notebook]}
        path = os.path.join(REPORTS_DIR, f"{stamp}-gate-failure.json")
        with open(path, "w") as f:
            json.dump(failure, f, indent=2)
        print(f"NOT PUBLISHED - failure report at {path}", file=sys.stderr)
        return 1
    finally:
        backend.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inject-fault",
                   choices=["fabricated_number", "flipped_direction"],
                   help="corrupt the draft before checking, to demo the gate")
    args = p.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set - the writer needs it. "
              "See agent/.env.example.", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(args.inject_fault))


if __name__ == "__main__":
    main()
