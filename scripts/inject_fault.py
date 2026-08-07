#!/usr/bin/env python3
"""Inject one deliberate fault into a checkout, to exercise the assisted-triage layer.

This replaces the three `demo/*` branches the repo used to carry. Those branches
had to be rebased after every structural change, drifted stale, and left an open
PR that put an intentional break one click from main - which is not hypothetical:
PR #7 was merged by accident on 2026-07-26 and had to be reverted in 258fd50.

A fault is now a workflow input instead, so the demo runs from main and never
needs rebasing.

    gh workflow run daily-refresh -f inject_fault=accepted_values

Two rules this file follows, both load-bearing:

  1. It refuses to run outside CI unless forced, because it edits tracked files
     in place and a stray local run would dirty a real working tree.
  2. If the text it expects is missing, it EXITS NON-ZERO rather than doing
     nothing. A silent no-op would produce a green build and look like the
     triage layer had failed to notice a fault that was never actually injected.

Usage:
    python3 scripts/inject_fault.py <accepted_values|dup_ticker|renamed_column>
    python3 scripts/inject_fault.py <fault> --force     # local, edits your tree
"""

from __future__ import annotations

import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Paths are globbed rather than hardcoded so this keeps working across layout
# changes - the model folders were renumbered once already.
FAULTS = {
    "accepted_values": {
        "glob": "models/**/_staging.yml",
        "old": '["tech", "industrials", "financials", "crypto", "benchmark"]',
        "new": '["tech", "industrials", "financials", "benchmark"]',
        "breaks": "accepted_values test on stg_watchlist.sector (3 crypto rows fall outside the list)",
    },
    "dup_ticker": {
        "glob": "seeds/watchlist.csv",
        "append": "NVDA,NVIDIA Corp,tech,equity,true,false\n",
        "breaks": "unique test on stg_watchlist.ticker (NVDA appears twice)",
    },
    "renamed_column": {
        "glob": "models/**/int_daily_returns.sql",
        # The copy inside the calc CTE, indented 8. The outer select still says
        # close_price, so renaming this one breaks the model at build time
        # rather than merely failing a test.
        "old": "        close_price,\n",
        "new": "        closing_price,\n",
        "breaks": "int_daily_returns fails to compile (outer select references a column the CTE no longer produces)",
    },
}


def _resolve(pattern: str) -> str:
    matches = glob.glob(os.path.join(ROOT, pattern), recursive=True)
    if not matches:
        sys.exit(f"inject_fault: nothing matched {pattern!r} under {ROOT}")
    if len(matches) > 1:
        listing = "\n  ".join(sorted(matches))
        sys.exit(f"inject_fault: {pattern!r} is ambiguous, matched:\n  {listing}")
    return matches[0]


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--force"]
    forced = "--force" in sys.argv

    if len(args) != 1 or args[0] not in FAULTS:
        sys.exit(f"usage: inject_fault.py <{'|'.join(FAULTS)}> [--force]")

    if not os.environ.get("CI") and not forced:
        sys.exit(
            "inject_fault: refusing to run outside CI - this edits tracked files "
            "in place. Pass --force if you really mean to break your working tree."
        )

    name = args[0]
    fault = FAULTS[name]
    path = _resolve(fault["glob"])
    rel = os.path.relpath(path, ROOT)

    with open(path) as f:
        content = f.read()

    if "append" in fault:
        if not content.endswith("\n"):
            content += "\n"
        updated = content + fault["append"]
    else:
        if fault["old"] not in content:
            sys.exit(
                f"inject_fault: anchor text not found in {rel}.\n"
                f"  looking for: {fault['old'].strip()!r}\n"
                "The file changed since this fault was written. Fix the anchor "
                "rather than letting the run go green with no fault injected."
            )
        updated = content.replace(fault["old"], fault["new"], 1)

    with open(path, "w") as f:
        f.write(updated)

    print(f"inject_fault: {name}")
    print(f"  edited : {rel}")
    print(f"  breaks : {fault['breaks']}")
    print("  This is a deliberate break. The build SHOULD go red from here.")


if __name__ == "__main__":
    main()
