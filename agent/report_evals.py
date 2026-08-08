"""
Evals for the report gate. The harness's own harness.

The claim this project makes is that fabrication is structurally unable to
reach the reader. That claim is only believable if the gate is shown catching
faults - so, like scripts/inject_fault.py for the triage layer, every failure
class the gate exists to stop has a case here that proves it stops it.

Two tiers:

  OFFLINE (always runs, no API key, no warehouse, pure stdlib) - exercises
  the deterministic matcher and the gate rule against fixture notebooks and
  hand-written drafts. This tier runs in CI as the cheap stage that gates the
  expensive one, the same shape as build-duckdb gating build-snowflake.

  LIVE (needs ANTHROPIC_API_KEY; skips cleanly without, like
  scripts/triage/diagnose_failure.py) - exercises the verifier call on the
  two failure classes only a model can judge: a flipped direction word and an
  unsupported superlative.

    python3 report_evals.py            # offline tier, plus live if key set
    python3 report_evals.py --offline  # offline only, even with a key
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

import claims

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# One fixture notebook, shaped exactly like the writer's: the query text and
# the pipe-delimited rows run_sql returns. Values chosen so rounding and
# percent/fraction scale are both exercised.
NOTEBOOK = [
    {"query": "select ticker, ret_1d from mart_movers order by ret_1d desc",
     "result": "TICKER | RET_1D\nNVDA | 0.03217\nCAT | 0.0121\nJPM | -0.0812"},
    {"query": "select sector, avg_ret_1d from mart_sector_overview order by avg_ret_1d desc",
     "result": "SECTOR | AVG_RET_1D\ntech | 0.0184\nfinancials | -0.0032"},
    {"query": "select count(distinct ticker) as n, max(trade_date) as latest from fct_prices",
     "result": "N | LATEST\n18 | 2026-08-07"},
]

# Offline cases: (id, draft, expect_publish, note). The stub verifier result
# below stands in for a verifier that found everything supported, so these
# cases isolate the matcher and the gate rule.
ALL_SUPPORTED: list[dict] = []

OFFLINE_CASES = [
    ("clean-pass",
     "Over the week to August 7, 2026, NVDA led the 18-ticker watchlist, up "
     "3.2%, while JPM fell 8.1%. Tech averaged 1.8%; financials slipped 0.3%.",
     True,
     "rounded percents vs fraction evidence, count, prose date - all traceable"),

    ("fabricated-number",
     "NVDA rose 3.2% and the watchlist's average dividend yield hit 4.7%.",
     False,
     "4.7% appears in no query result - the core fabrication case"),

    ("fabricated-date",
     "As of August 9, 2026, NVDA was up 3.2%.",
     False,
     "date never appeared in any result"),

    ("flipped-direction-passes-matcher",
     "JPM rose 8.1% on the week.",
     True,
     "matcher matches |value| by design; direction is the verifier's job - "
     "this case pins the division of labour so it can't erode silently"),

    ("terminology-exempt",
     "NVDA's 20-day moving average held above its 5-day, and the top 3 names "
     "were all tech. NVDA gained 3.2%.",
     True,
     "window words and top-N are method terms, not claims"),

    ("scale-and-rounding",
     "Tech's average move was 1.84%, roughly 2% on the day.",
     True,
     "0.0184 supports both 1.84% (x100 exact) and 2% (x100, rounded to 0dp)"),
]

# Punctuation cases. The project bans em dashes and the model ignored the
# prompt that said so, so the rule is enforced in code and pinned here. The
# entity case is the one that actually happened: &mdash; renders as an em dash,
# so writing the entity broke the rule while looking like it hadn't.
PUNCTUATION_CASES = [
    ("punct-em-dash",
     "NVDA led the watchlist — up 3.2% — while JPM fell.",
     "spaced em dash becomes a spaced hyphen"),
    ("punct-em-dash-unspaced",
     "NVDA led—JPM lagged.",
     "unspaced em dash is parenthetical, so it gains spaces rather than "
     "gluing the words together"),
    ("punct-en-dash-range",
     "Z-scores landed in the 1.3–1.8 band.",
     "en dash in a range becomes a tight hyphen, not a spaced one"),
    ("punct-unicode-minus",
     "JPM fell −8.1% on the day.",
     "U+2212 minus sign becomes a plain hyphen"),
    ("punct-html-entity",
     "Market movers &mdash; price action &ndash; latest run.",
     "entities expand to dashes on the page, so they count as dashes here"),
]

# Gate-rule cases, driven by stub verifier outputs rather than drafts.
GATE_CASES = [
    ("gate-verifier-contradicted",
     [{"text": "JPM rose 8.1%", "kind": "direction", "verdict": "contradicted",
       "evidence": "ret_1d is -0.0812"}],
     False, "one contradicted claim blocks"),
    ("gate-verifier-not-found",
     [{"text": "volume was unusually heavy", "kind": "other",
       "verdict": "not_found", "evidence": "no volume query in notebook"}],
     False, "a claim with no evidence blocks - not just wrong ones"),
    ("gate-fails-closed",
     None,
     False, "verifier never ran (no key / parse failure) - block, never publish"),
    ("gate-all-supported",
     ALL_SUPPORTED,
     True, "verifier ran, nothing unsupported - publish"),
]

# Live cases: only a model can judge these. The draft is corrupted relative
# to the same fixture notebook; the verifier must return at least one
# non-supported verdict, which the gate then turns into a block.
LIVE_CASES = [
    ("live-flipped-direction",
     "Over the week NVDA fell 3.2% while JPM gained 8.1%. Tech averaged 1.8%.",
     "both direction words contradict the signs in the notebook"),
    ("live-unsupported-superlative",
     "CAT was the week's biggest gainer, up 1.2%.",
     "1.2% traces to CAT's row, but NVDA's 3.2% makes 'biggest' false"),
]


def main() -> None:
    offline_only = "--offline" in sys.argv
    passed = failed = 0

    def result(ok: bool, case_id: str, detail: str = "") -> None:
        nonlocal passed, failed
        print(f"{'PASS' if ok else 'FAIL'}  {case_id}")
        if not ok and detail:
            print(f"        {detail}")
        passed, failed = passed + ok, failed + (not ok)

    for case_id, draft, expect_publish, note in OFFLINE_CASES:
        matcher_failures = claims.check_draft(draft, NOTEBOOK)
        decision = claims.gate_decision(matcher_failures, ALL_SUPPORTED)
        result(decision["publish"] == expect_publish, case_id,
               f"expected publish={expect_publish}; reasons={decision['reasons']} ({note})")

    for case_id, raw, note in PUNCTUATION_CASES:
        cleaned = claims.normalize_punctuation(raw)
        left = claims.find_forbidden_punctuation(cleaned)
        result(not left, case_id, f"still present: {left} in {cleaned!r} ({note})")

    # Normalising runs before the matcher, so it must not cost the matcher a
    # number it would otherwise have traced. A Unicode minus on a figure that
    # IS in the notebook has to survive as a publishable claim.
    minus_draft = claims.normalize_punctuation("JPM fell −8.1% while NVDA rose 3.2%.")
    minus_decision = claims.gate_decision(
        claims.check_draft(minus_draft, NOTEBOOK), ALL_SUPPORTED)
    result(minus_decision["publish"], "punct-preserves-matcher",
           f"normalising broke number tracing: {minus_decision['reasons']}")

    for case_id, verifier_claims, expect_publish, note in GATE_CASES:
        decision = claims.gate_decision([], verifier_claims)
        result(decision["publish"] == expect_publish, case_id,
               f"expected publish={expect_publish}; reasons={decision['reasons']} ({note})")

    if not offline_only and os.environ.get("ANTHROPIC_API_KEY"):
        import report
        for case_id, draft, note in LIVE_CASES:
            verifier_claims = report.verify(draft, NOTEBOOK)
            decision = claims.gate_decision(
                claims.check_draft(draft, NOTEBOOK), verifier_claims)
            # The corrupted draft must be blocked, and blocked for a verifier
            # reason, not merely a matcher one - this is the verifier's eval.
            verifier_caught = verifier_claims is not None and any(
                c.get("verdict") != "supported" for c in verifier_claims)
            result(not decision["publish"] and verifier_caught, case_id,
                   f"verifier_claims={verifier_claims} ({note})")
    elif not offline_only:
        print("SKIP  live tier (ANTHROPIC_API_KEY not set - offline tier still binding)")

    print(f"\n{passed}/{passed + failed} passed")
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
