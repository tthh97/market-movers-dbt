"""
Deterministic claim checking for the weekly report agent.

Why this file exists: the verifier (report.py) is still an LLM, and an LLM
checking another LLM can false-pass the same way a writer can fabricate.
Numbers are the one class of claim code can check outright, so code checks
them. The verifier is left only what code cannot judge: direction words
("rose", "fell"), superlatives ("biggest", "led"), and causal phrasing.

The contract:

  - Every numeric token in the draft must be traceable to a number in the
    evidence notebook (the saved run_sql results), allowing for rounding to
    the draft's own precision and for percent/fraction scale (a draft "3.2%"
    is supported by an evidence 0.0321).
  - Every date in the draft, ISO or prose, must appear in the evidence.
  - Matching is on absolute value: "fell 8.1%" against an evidence -0.081 is
    the matcher's PASS. Whether "fell" is the right word is the verifier's
    job. The split is deliberate - sign errors are direction errors.

Deliberate exemptions, listed so the limitation is visible rather than
discovered:

  - window terminology: "5-day", "20-day moving average" - method, not claim
  - "top N" phrasing
  - standalone years (1900-2100) outside a full date

The gate decision also lives here, because it is pure logic and the eval
suite (report_evals.py) must run it with no API key and no warehouse. The
gate fails closed: a draft whose verifier never ran is unpublishable, the
same way a dbt build with skipped tests is not a green build.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# House punctuation. The project bans em dashes; a prompt asking for that is a
# request, not a guarantee, and the model ignored it for as long as nothing
# checked. So the prompt asks and this function enforces, the same split as
# numbers above: the model is told, and then code makes it true.
#
# HTML entities are included deliberately. &mdash; puts an em dash on the page
# exactly as surely as typing one, and writing entities is how the rule got
# broken the last time.
# ---------------------------------------------------------------------------

_ENTITY_DASHES = {
    "&mdash;": "—", "&#8212;": "—", "&#x2014;": "—",
    "&ndash;": "–", "&#8211;": "–", "&#x2013;": "–",
    "&minus;": "−", "&#8722;": "−",
}

# Em dash and horizontal bar are parenthetical: "A - B" reads correctly, so an
# unspaced one gains spaces. En dash, figure dash and minus are range and sign
# characters, where "1.3-1.8" and "-3.3%" are what is wanted - no padding.
_PARENTHETICAL_RE = re.compile(r"[ \t]*[—―][ \t]*")
_TIGHT_DASHES = ("–", "‒", "−")

FORBIDDEN_DASHES = ("—", "―", "–", "‒", "−",
                    *_ENTITY_DASHES)


def normalize_punctuation(text: str) -> str:
    """Rewrite every dash that is not a plain hyphen into one."""
    for entity, char in _ENTITY_DASHES.items():
        text = text.replace(entity, char)
    text = _PARENTHETICAL_RE.sub(" - ", text)
    for char in _TIGHT_DASHES:
        text = text.replace(char, "-")
    return text


def find_forbidden_punctuation(text: str) -> list[str]:
    """Every banned dash present, for tests and failure reports."""
    return [d for d in FORBIDDEN_DASHES if d in text]


# ---------------------------------------------------------------------------
# Evidence side: what the saved query results actually contain.
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def evidence_numbers(notebook: list[dict]) -> set[float]:
    """Every numeric value in every saved result, as absolute floats."""
    vals: set[float] = set()
    for entry in notebook:
        text = str(entry.get("result", "")).replace(",", "")
        for m in _NUM_RE.finditer(text):
            try:
                vals.add(abs(float(m.group())))
            except ValueError:
                pass
    return vals


def evidence_dates(notebook: list[dict]) -> set[str]:
    """Every ISO date appearing in any saved result."""
    dates: set[str] = set()
    for entry in notebook:
        dates.update(_ISO_DATE_RE.findall(str(entry.get("result", ""))))
    return dates


# ---------------------------------------------------------------------------
# Draft side: which tokens are claims, and which are exempt.
# ---------------------------------------------------------------------------

_MONTHS = ["january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]
_MONTH_ALT = "|".join(_MONTHS + [m[:3] for m in _MONTHS if m != "may"])

# "August 5, 2026" / "Aug 5 2026"
_PROSE_MDY_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
# "5 August 2026"
_PROSE_DMY_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})\b", re.I)

_EXEMPT_RES = [
    # method windows: 5-day return, 20 day MA, 1-month, "past 5 days"
    re.compile(r"\b\d+[-\s](?:day|days|week|weeks|month|months|d|dma|ma)\b", re.I),
    re.compile(r"\btop\s+\d+\b", re.I),
    re.compile(r"\b(?:19|20)\d{2}\b"),  # standalone year; full dates checked above
]

_TOKEN_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


def _month_num(name: str) -> int:
    name = name.lower()[:3]
    for i, m in enumerate(_MONTHS, start=1):
        if m.startswith(name):
            return i
    raise ValueError(name)


def _spans(regexes, text: str) -> list[tuple[int, int]]:
    return [m.span() for rx in regexes for m in rx.finditer(text)]


def _inside(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] >= a and span[1] <= b for a, b in spans)


def _supported(value: float, decimals: int, evidence: set[float]) -> bool:
    """True if some evidence number, at raw / x100 / /100 scale, rounds to it."""
    target = round(abs(value), decimals)
    for e in evidence:
        for scale in (1.0, 100.0, 0.01):
            if round(e * scale, decimals) == target:
                return True
    return False


def check_draft(draft: str, notebook: list[dict]) -> list[dict]:
    """Return the list of unsupported claims. Empty list = every number traced.

    Each failure: {"token", "context", "reason"} - context is the surrounding
    text so the writer (on the rewrite pass) and the human (in the failure
    report) can find the sentence without diffing.
    """
    ev_nums = evidence_numbers(notebook)
    ev_dates = evidence_dates(notebook)
    failures: list[dict] = []

    date_spans: list[tuple[int, int]] = []

    # ISO dates in the draft.
    for m in _ISO_DATE_RE.finditer(draft):
        date_spans.append(m.span())
        if m.group() not in ev_dates:
            failures.append({"token": m.group(),
                             "context": draft[max(0, m.start() - 40):m.end() + 40],
                             "reason": "date not present in any query result"})

    # Prose dates, both orders.
    for rx, order in ((_PROSE_MDY_RE, "mdy"), (_PROSE_DMY_RE, "dmy")):
        for m in rx.finditer(draft):
            date_spans.append(m.span())
            g = m.groups()
            month, day, year = (g[0], g[1], g[2]) if order == "mdy" else (g[1], g[0], g[2])
            try:
                iso = f"{int(year):04d}-{_month_num(month):02d}-{int(day):02d}"
            except ValueError:
                continue
            if iso not in ev_dates:
                failures.append({"token": m.group(),
                                 "context": draft[max(0, m.start() - 40):m.end() + 40],
                                 "reason": f"date ({iso}) not present in any query result"})

    exempt_spans = _spans(_EXEMPT_RES, draft)

    for m in _TOKEN_RE.finditer(draft):
        if _inside(m.span(), date_spans) or _inside(m.span(), exempt_spans):
            continue
        raw = m.group().lstrip("+-$").rstrip("%").replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        if not _supported(value, decimals, ev_nums):
            failures.append({"token": m.group(),
                             "context": draft[max(0, m.start() - 40):m.end() + 40],
                             "reason": "number not traceable to any query result"})
    return failures


# ---------------------------------------------------------------------------
# The gate. Ordinary code, and the only thing that decides publication.
# ---------------------------------------------------------------------------

def gate_decision(matcher_failures: list[dict],
                  verifier_claims: list[dict] | None) -> dict:
    """Mechanical rule: no claim ships unverified.

    verifier_claims is the verifier's claim-by-claim output, or None when the
    verifier did not run (no API key, parse failure). None BLOCKS - failing
    closed is the point. A verdict other than "supported" blocks. An empty
    list passes: the verifier ran and found nothing it could not support.
    """
    reasons = [f"unverified number {f['token']!r}: {f['reason']}"
               for f in matcher_failures]
    if verifier_claims is None:
        reasons.append("verifier did not run - failing closed, draft not publishable")
    else:
        for c in verifier_claims:
            if c.get("verdict") != "supported":
                reasons.append(f"verifier: {c.get('verdict', 'missing verdict')} - "
                               f"{c.get('text', '(claim text missing)')!r}")
    return {"publish": not reasons, "reasons": reasons}
