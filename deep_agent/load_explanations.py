"""
Load a research run's explanations.jsonl into raw.alert_explanations.

The research agent reads as REPORTER and writes only a local file. This is the
one step that writes, and it runs as the loader role, the same identity that
lands prices. Rows are appended, never updated: fct_alert_explanations picks
the latest run per alert, and older runs stay in RAW for audit.

    DBT_TARGET=snowflake SNOWFLAKE_ROLE=<loader> ... python load_explanations.py runs/<run_id>/explanations.jsonl
    DBT_TARGET=duckdb python load_explanations.py runs/<run_id>/explanations.jsonl

Then `dbt build -s fct_alert_explanations+` publishes them.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import warehouse  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                rows.append(tuple(r[c] for c in warehouse.EXPLANATION_COLUMNS))
    if not rows:
        raise SystemExit(f"{path} has no rows.")
    con = warehouse.connect()
    try:
        con.append_explanations(rows)
    finally:
        con.close()
    print(f"appended {len(rows)} rows to {warehouse.target()} raw.alert_explanations")


if __name__ == "__main__":
    main()
