"""
Session 3 of the assisted-triage layer: PROPOSE (surface the diagnosis to a human).

Reads diagnosis.json (from scripts/diagnose_failure.py) and failure_context.json
(from scripts/capture_failure.py) and renders one human-approval report. It PROPOSES
a fix a human reviews and approves - it NEVER edits models, writes to the warehouse,
or opens a pull request.

Delivery:
  - In CI (GITHUB_ACTIONS set) it opens a GitHub issue via `gh issue create` so a
    human is actually notified instead of having to dig through Actions artifacts.
  - Locally (or if `gh` is unavailable) it writes proposed_fix_issue.md and prints it.

Design guarantees (mirrors capture/diagnose):
  - Pure standard library - runs on the CI runner with no extra installs.
  - Exit code is always 0: proposing is a reporting step, not a gate. It skips
    cleanly when there is no diagnosis (e.g. the run failed before dbt, or the key
    was unset so no diagnosis was produced).

Usage:
    python3 scripts/propose_fix.py
    python3 scripts/propose_fix.py --diagnosis diagnosis.json --context failure_context.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CONFIDENCE_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🔴"}


def _skip(msg: str) -> None:
    print(f"propose_fix: {msg} - skipping (no proposal surfaced).")
    sys.exit(0)


def _load(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _failing_names(context: dict | None) -> list[str]:
    if not context:
        return []
    return [f["name"] for f in context.get("failures", []) if f.get("name")]


def _ci_run_url() -> str | None:
    """A clickable link back to the CI run, when we're inside GitHub Actions."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def build_title(diagnosis: dict, context: dict | None) -> str:
    model = diagnosis.get("failing_model") or (_failing_names(context) or ["unknown"])[0]
    conf = diagnosis.get("confidence", "?")
    return f"[assisted-triage] {model} failed - proposed fix ({conf} confidence)"


def build_body(diagnosis: dict, context: dict | None) -> str:
    conf = diagnosis.get("confidence", "unknown")
    emoji = CONFIDENCE_EMOJI.get(conf, "⚪")
    failing = _failing_names(context)

    flags = []
    if diagnosis.get("touches_operating_rules"):
        flags.append(
            "⚠️ **Touches operating rules** - the proposed fix may conflict with a "
            "pipeline operating rule. Review carefully before applying."
        )
    if diagnosis.get("is_upstream_data_issue"):
        flags.append(
            "📉 **Likely an upstream/data issue** - this may be stale or empty source "
            "data rather than a code bug. A retry or a data check may be the right call, "
            "not a SQL change."
        )
    flags_md = "\n".join(f"- {f}" for f in flags) if flags else "- None raised."

    lines = [
        "## Assisted-triage: a fix is proposed for your approval",
        "",
        "The nightly `dbt build` failed. `capture_failure.py` turned dbt's artifacts "
        "into a clean failure report, and `diagnose_failure.py` made **one** Claude API "
        "call for a structured diagnosis. This issue surfaces that diagnosis so a human "
        "can decide. **Nothing has been changed** - no models edited, no PR opened, no "
        "writes to the warehouse.",
        "",
        f"**Failing node(s):** {', '.join(f'`{n}`' for n in failing) if failing else '_see failure context_'}",
        f"**Diagnosis confidence:** {emoji} {conf}",
        "",
        "### Likely cause",
        diagnosis.get("likely_cause", "_not provided_"),
        "",
        "### Proposed fix (for a human to apply)",
        diagnosis.get("proposed_fix", "_not provided_"),
        "",
        "### Safety flags",
        flags_md,
        "",
        "### Human approval checklist",
        "- [ ] Read the proposed fix and confirm it addresses the real cause",
        "- [ ] Confirm it does **not** violate an operating rule (single-writer DuckDB, "
        "`--profiles-dir .`, never write to `main`, etc.)",
        "- [ ] Apply the change on a branch and re-run `dbt build --profiles-dir .`",
        "- [ ] Open a PR once the build is green",
        "",
    ]

    run_url = _ci_run_url()
    if run_url:
        lines += [f"**CI run:** {run_url}", ""]

    meta = []
    if context:
        for k in ("captured_at", "dbt_version", "invocation_id"):
            if context.get(k):
                meta.append(f"- `{k}`: {context[k]}")
    if meta:
        lines += ["<details><summary>Run metadata</summary>", "", *meta, "", "</details>", ""]

    lines += [
        "---",
        "_This is a **proposal** produced by assisted triage. A human reviews and "
        "approves before any change. The pipeline never self-edits._",
    ]
    return "\n".join(lines)


def open_github_issue(title: str, body: str) -> bool:
    """Open a GitHub issue via `gh`. Returns True on success. Best-effort."""
    gh = shutil.which("gh")
    if not gh:
        print("propose_fix: `gh` not found on PATH; cannot open an issue.")
        return False
    try:
        result = subprocess.run(
            [gh, "issue", "create", "--title", title, "--body", body],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"propose_fix: `gh issue create` failed to run ({type(e).__name__}).")
        return False
    if result.returncode != 0:
        print(f"propose_fix: `gh issue create` returned {result.returncode}: {result.stderr.strip()}")
        return False
    print(f"propose_fix: opened GitHub issue -> {result.stdout.strip()}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Surface a dbt-failure diagnosis for human approval.")
    ap.add_argument("--diagnosis", default=os.path.join(ROOT, "diagnosis.json"))
    ap.add_argument("--context", default=os.path.join(ROOT, "failure_context.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "proposed_fix_issue.md"))
    args = ap.parse_args()

    diagnosis = _load(args.diagnosis)
    if diagnosis is None:
        _skip(f"{os.path.basename(args.diagnosis)} not found (nothing was diagnosed)")

    context = _load(args.context)  # optional - enriches the report if present

    title = build_title(diagnosis, context)
    body = build_body(diagnosis, context)

    # Always write the markdown so it can be uploaded as a CI artifact / read locally.
    with open(args.out, "w") as f:
        f.write(f"# {title}\n\n{body}\n")
    print(f"propose_fix: wrote {args.out}")

    # In CI, also open an issue so a human is notified. Locally, printing is enough.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if not open_github_issue(title, body):
            print("propose_fix: falling back to the uploaded markdown artifact.")
    else:
        print("propose_fix: not in GitHub Actions - proposal written locally, not opening an issue.\n")
        print(f"--- {os.path.basename(args.out)} ---\n{title}\n\n{body}")


if __name__ == "__main__":
    main()
