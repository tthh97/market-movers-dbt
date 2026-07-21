"""Shared helpers for the assisted-triage layer (capture -> diagnose -> propose).

Kept tiny and pure-stdlib on purpose - the triage scripts run on the bare CI
runner with no extra installs.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_json(path: str) -> dict | None:
    """Read a JSON file, or None if it's missing or unparseable."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def skip(msg: str) -> None:
    """Print msg and exit 0 - a skip is a no-op, never a CI failure."""
    print(msg)
    sys.exit(0)
