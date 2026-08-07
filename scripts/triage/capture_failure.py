"""
Capture a clean, machine-readable snapshot of a failed dbt build.

Session 1 of the assisted-triage layer: NO AI here. This step only parses
dbt's own artifacts (target/run_results.json + target/manifest.json) and writes
one curated failure_context.json. A later step sends that file to the model for
a diagnosis; a human approves any fix. Keep this file pure-stdlib so it runs on
the CI runner with no extra installs.

Usage (locally or in CI, after a dbt build):
    python3 scripts/triage/capture_failure.py
    python3 scripts/triage/capture_failure.py --out failure_context.json

Exit code is always 0: capture is a reporting step, not a gate. It writes a
context file describing whatever it found (including "dbt never ran").
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os

from _triage_common import ROOT, load_json as _load

TARGET = os.path.join(ROOT, "target")
RUN_RESULTS = os.path.join(TARGET, "run_results.json")
MANIFEST = os.path.join(TARGET, "manifest.json")

# Statuses that mean "this node did not pass". 'warn' is intentionally excluded
# (soft) but counted separately so a diagnosis can see it.
FAIL_STATUSES = {"fail", "error", "runtime error"}

# Cap embedded source so the context stays lean (context discipline: send the
# model curated facts, not the whole repo).
MAX_SQL_CHARS = 6000
MAX_DEF_CHARS = 4000


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _truncate(text: str | None, limit: int) -> str | None:
    if not text:
        return text
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n-- [truncated {len(text) - limit} chars] --"


def _read_source(rel_path: str) -> str | None:
    """Read a model/test source file relative to the project root, capped."""
    if not rel_path:
        return None
    full = os.path.join(ROOT, rel_path)
    if not os.path.isfile(full):
        return None
    try:
        with open(full) as f:
            return _truncate(f.read(), MAX_DEF_CHARS)
    except OSError:
        return None


def _tested_node(test_node: dict) -> str | None:
    """The model/source a generic test is attached to, if any."""
    attached = test_node.get("attached_node")
    if attached:
        return attached
    deps = (test_node.get("depends_on") or {}).get("nodes") or []
    return deps[0] if deps else None


def build_context() -> dict:
    run = _load(RUN_RESULTS)
    manifest = _load(MANIFEST)

    # dbt never produced results -> failure happened before/around dbt (e.g. the
    # ingest step, a bad profile). Say so plainly rather than guess.
    if run is None:
        return {
            "captured_at": _now_iso(),
            "status": "no_run_results",
            "note": (
                "No target/run_results.json found. The failure likely occurred "
                "before dbt ran its nodes (e.g. ingestion, dependency install, "
                "or profile resolution). This is probably an upstream/data or "
                "environment issue, not a model bug."
            ),
            "failures": [],
        }

    meta = run.get("metadata", {})
    nodes = (manifest or {}).get("nodes", {})
    results = run.get("results", [])

    failures = []
    n_warn = 0
    for r in results:
        status = (r.get("status") or "").lower()
        if status == "warn":
            n_warn += 1
            continue
        if status not in FAIL_STATUSES:
            continue

        uid = r.get("unique_id")
        node = nodes.get(uid, {})
        resource_type = node.get("resource_type", "unknown")

        entry: dict = {
            "unique_id": uid,
            "name": node.get("name") or uid,
            "resource_type": resource_type,
            "status": status,
            "num_failing_rows": r.get("failures"),
            "message": r.get("message"),
            "relation_name": r.get("relation_name"),
            "original_file_path": node.get("original_file_path"),
            "compiled_path": node.get("compiled_path"),
            "compiled_sql": _truncate(r.get("compiled_code"), MAX_SQL_CHARS),
        }

        if resource_type == "test":
            tm = node.get("test_metadata") or {}
            entry["test_metadata"] = {
                "name": tm.get("name"),
                "kwargs": tm.get("kwargs"),
            }
            entry["column_name"] = node.get("column_name")
            entry["tested_node"] = _tested_node(node)
            # Singular tests carry their logic in a .sql file worth showing;
            # generic tests are fully described by test_metadata + compiled_sql.
            if tm.get("name") is None:
                entry["definition"] = _read_source(node.get("original_file_path"))
        else:
            # Model/seed/snapshot error: the raw source is the useful artifact.
            entry["definition"] = _read_source(node.get("original_file_path"))

        failures.append(entry)

    return {
        "captured_at": _now_iso(),
        "status": "failures_found" if failures else "clean",
        "dbt_version": meta.get("dbt_version"),
        "invocation_id": meta.get("invocation_id"),
        "run_generated_at": meta.get("generated_at"),
        "command": (run.get("args") or {}).get("which"),
        "n_failures": len(failures),
        "n_warnings": n_warn,
        "failures": failures,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture dbt failure context (no AI).")
    ap.add_argument(
        "--out",
        default=os.path.join(ROOT, "failure_context.json"),
        help="Path to write the failure context JSON.",
    )
    args = ap.parse_args()

    context = build_context()
    with open(args.out, "w") as f:
        json.dump(context, f, indent=2)
        f.write("\n")

    status = context["status"]
    n = context.get("n_failures", 0)
    print(f"capture_failure: status={status} failures={n} -> {args.out}")
    for fdef in context.get("failures", []):
        print(f"  - {fdef['status']:5} {fdef['resource_type']:5} {fdef['name']}")


if __name__ == "__main__":
    main()
