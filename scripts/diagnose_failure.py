"""
Session 2 of the assisted-triage layer: DIAGNOSE (the only AI step).

Reads the curated failure_context.json produced by scripts/capture_failure.py and
asks the Claude API for a single, structured diagnosis with safety flags. It
PROPOSES; a human approves. It never edits models, never writes to the warehouse,
never opens a PR. Output is printed and written to diagnosis.json (git-ignored,
uploaded as a CI artifact) - no source files are touched.

Design guarantees (see docs/pipeline-explainer.md and the build guideline):
  - Exactly ONE model call per failed run. No retry loop (cost_runaway guard).
  - Structured output (output_config.format) so the diagnosis is machine-checkable.
  - Skips cleanly (exit 0) when there is no key, no failure_context, or nothing
    failed - so the CI job never breaks just because triage had nothing to do.

Usage:
    python3 scripts/diagnose_failure.py
    python3 scripts/diagnose_failure.py --in failure_context.json --out diagnosis.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The operating rules the pipeline must obey. They go into the prompt so the model
# can flag a fix that would violate one (touches_operating_rules).
OPERATING_RULES = """\
- profiles.yml is at the project root: every dbt command needs `--profiles-dir .`.
- Use `dbt build` (models + tests in one pass), never `dbt run` alone.
- DuckDB is single-writer: any read connection must use read_only=True.
- Always use the project .venv; never base conda.
- Use python3, not python.
- Never write to `main`, never auto-merge, never modify market.duckdb directly.
- dbt 1.11+: accepted_values / relationships args nest under `arguments:`.\
"""

# Structured-output contract for the diagnosis (the required response shape).
DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "failing_model": {"type": "string"},
        "likely_cause": {"type": "string"},
        "proposed_fix": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "touches_operating_rules": {"type": "boolean"},
        "is_upstream_data_issue": {"type": "boolean"},
    },
    "required": [
        "failing_model",
        "likely_cause",
        "proposed_fix",
        "confidence",
        "touches_operating_rules",
        "is_upstream_data_issue",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""\
You are a senior analytics engineer performing assisted triage on a failed nightly
dbt build for a DuckDB-backed pipeline (market-movers-dbt: yfinance -> DuckDB ->
dbt, layered staging -> incremental fact -> intermediate -> marts).

You are given a curated failure_context.json - the failing node(s), the rule each
broke, the error, the number of offending rows, and the exact compiled SQL dbt ran.
Diagnose the single most likely cause and propose a fix for a HUMAN to approve. You
do not and cannot apply changes.

Be precise and defensible:
- If the failure is an upstream/data problem (e.g. the source returned empty or
  stale data) rather than a code bug, set is_upstream_data_issue=true and recommend
  a retry or a data check - do not invent a SQL change.
- If your proposed fix would violate any operating rule below, set
  touches_operating_rules=true.
- Set confidence honestly; a human reads it before acting.

Operating rules (must not be violated by any proposed fix):
{OPERATING_RULES}\
"""


def _skip(msg: str) -> None:
    print(f"diagnose_failure: {msg} - skipping (no model call).")
    sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnose a dbt failure (one AI call).")
    ap.add_argument("--in", dest="infile", default=os.path.join(ROOT, "failure_context.json"))
    ap.add_argument("--out", dest="outfile", default=os.path.join(ROOT, "diagnosis.json"))
    args = ap.parse_args()

    # Load .env if present (local dev); CI passes the key as an env var.
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    if not os.environ.get("ANTHROPIC_API_KEY"):
        _skip("ANTHROPIC_API_KEY not set (add it to .env locally or a CI secret)")

    if not os.path.isfile(args.infile):
        _skip(f"{os.path.basename(args.infile)} not found (run capture_failure.py first)")

    with open(args.infile) as f:
        context = json.load(f)

    if context.get("status") != "failures_found" or not context.get("failures"):
        _skip(f"nothing to diagnose (capture status={context.get('status')!r})")

    # Import here so the earlier skip paths don't require the SDK to be installed.
    import anthropic

    model = os.environ.get("DIAGNOSIS_MODEL", "claude-opus-4-8")
    client = anthropic.Anthropic()

    user_content = (
        "Here is the curated failure context from the failed nightly dbt build. "
        "Return one structured diagnosis.\n\n"
        f"```json\n{json.dumps(context, indent=2)}\n```"
    )

    try:
        # Exactly one call. Adaptive thinking for a defensible diagnosis; the
        # response is constrained to DIAGNOSIS_SCHEMA so it is machine-checkable.
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA},
            },
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        _skip("ANTHROPIC_API_KEY was rejected (check the key in .env or the CI secret)")
    except anthropic.APIError as e:  # network/5xx/etc. - triage is best-effort
        _skip(f"Claude API error ({type(e).__name__}); leaving triage to the human")

    if response.stop_reason == "refusal":
        _skip("model declined to answer")

    # output_config.format guarantees a text block with valid JSON (after any
    # thinking blocks). Pull the first text block and parse it.
    raw = next((b.text for b in response.content if b.type == "text"), None)
    if raw is None:
        _skip("no text block in response")
    diagnosis = json.loads(raw)

    with open(args.outfile, "w") as f:
        json.dump(diagnosis, f, indent=2)
        f.write("\n")

    print(f"diagnose_failure: wrote {args.outfile}  (model={response.model})\n")
    print(json.dumps(diagnosis, indent=2))
    flags = []
    if diagnosis.get("touches_operating_rules"):
        flags.append("touches_operating_rules")
    if diagnosis.get("is_upstream_data_issue"):
        flags.append("is_upstream_data_issue")
    print(
        f"\n-> confidence={diagnosis.get('confidence')}"
        + (f"  flags: {', '.join(flags)}" if flags else "  flags: none")
        + "\n-> This is a PROPOSAL. A human reviews and approves before any change."
    )


if __name__ == "__main__":
    main()
