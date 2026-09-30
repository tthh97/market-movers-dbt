"""
Verification of a research draft: what code can check, then what Jev decides.

research.py produces a draft (an Explanation). This module decides whether it
stands. Code checks what code can check: every cited source was returned by
the search in this run, dated inside the move's window. Jev (TypeSafe) then
makes the typed decisions in one call, reading the move, the claim, and the
search snippets of the cited sources: does the cause push the price the way it
moved, does each source report it, do the sources together report the cause,
and which category it is. decide() applies the thresholds to those answers, so
the verdict and the category are never text an LLM wrote.

Nothing here calls a model or needs an API key until verify() runs, so
decide() and code_checks() can be tested offline.
"""

from __future__ import annotations

import os

from langchain_typesafe import Choice, Noul, NoulCriteria, TypeSafeClassifier
from pydantic import BaseModel, Field

# Pinned rather than jev-latest, so a model upgrade is a visible change to the
# thresholds below instead of a silent one.
JEV = os.environ.get("RESEARCH_JEV_MODEL", "jev-1.13.0")

# The fixed category set, each with the definition Jev chooses against.
CATEGORY_CRITERIA = {
    "earnings": "The company reported quarterly or annual results.",
    "guidance": "The company changed its outlook, forecast, or targets, outside an earnings report.",
    "regulatory": "A regulator, court, government, or legal action affected the company or sector.",
    "product": "A product launch, product problem, contract win, or partnership of the company.",
    "m_and_a": "A merger, acquisition, divestment, or takeover bid involving the company.",
    "macro": "Economy-wide news: interest rates, inflation, jobs data, central banks, tariffs, or a broad market sell-off or rally.",
    "sector_sympathy": "A peer company or the wider sector had news, and this name moved along with it.",
    "crypto_specific": "News specific to crypto assets: ETF flows, exchanges, protocol events, or crypto regulation.",
    "commodity_supply": "Supply or demand of a commodity changed: OPEC, inventories, production, weather, or shipping.",
    "unknown": "The claim does not name a cause, or the cause fits none of the other categories.",
}

# Jev thresholds. A noul is the probability the answer is yes. On the first
# eight live units, drafts the sources plainly support scored 0.62-0.94 and
# unsupported ones 0.10-0.18, so the bar is the documented even-cost default of
# 0.5, not a stricter one that rejected correct causes. Set on those eight
# units only: the hand-labelled eval set is what should move it. HIGH is the
# bar for the "high" confidence label.
YES, HIGH, CATEGORY_FLOOR = 0.5, 0.85, 0.5

class Source(BaseModel):
    url: str
    title: str
    published: str = Field(description="YYYY-MM-DD, as the search tool reported it")


class Explanation(BaseModel):
    """The investigator's draft: the cause in words and the sources it rests on."""
    cause_summary: str = Field(description="One or two sentences. What happened, not advice. "
                                           "If nothing explains the move, say so and name no cause.")
    sources: list[Source] = Field(description="Only URLs the news-researcher returned. Empty when no cause was found.")


def code_checks(exp: Explanation, seen: dict, start: str, end: str) -> list[str]:
    problems = []
    for s in exp.sources:
        hit = seen.get(s.url)
        if hit is None:
            problems.append(f"source not returned by search in this run: {s.url}")
            continue
        published = hit["published"]
        if not published or not (start <= published <= end):
            problems.append(f"source outside window {start}..{end}: {s.url} ({published})")
    return problems


def direction(unit: dict) -> str:
    signs = {a["daily_return"] >= 0 for a in unit["alerts"]}
    return "mixed" if len(signs) > 1 else ("up" if signs.pop() else "down")


def jev_questions(exp: Explanation, move_direction: str) -> dict:
    """One call's questions. Each is a single literal judgment, and the state
    carries only what they need: the move, the claim, the cited snippets."""
    if not exp.sources:
        return {"names_cause": Noul(
            instructions="Does `claim` name a specific cause for the move?",
            criteria=NoulCriteria(
                true="The claim says what event or news drove the move.",
                false="The claim says no clear cause was found."))}
    qs = {}
    if move_direction != "mixed":
        qs["direction"] = Noul(
            instructions="Would the event in `claim` push the price of the assets in "
                         "`move` in the direction given by `move.direction`?",
            criteria=NoulCriteria(
                true="Good news for these assets and the move is up, or bad news and the move is down.",
                false="The event would push the price the other way, or has no clear direction."))
    for i in range(len(exp.sources)):
        qs[f"supports_{i}"] = Noul(
            instructions=f"Does `sources[{i}]` report the event described in `claim`?",
            criteria=NoulCriteria(
                true="The source describes the same event the claim gives as the cause.",
                false="The source only mentions the company or sector, or describes a different event."))
    # Asked about the cause only. A whole-claim "is every fact sourced" noul
    # also scored the move restated from the task, and flipped between drafts
    # the sources plainly support. Detail beyond the cause is not checked here.
    qs["grounded"] = Noul(
        instructions="Is the cause that `claim` gives for the move reported in `sources`?")
    qs["category"] = Choice(
        instructions="Which category describes the cause of the move given in `claim`?",
        criteria=CATEGORY_CRITERIA)
    return qs


def jev_state(unit: dict, exp: Explanation, seen: dict, move_direction: str) -> dict:
    return {
        "move": {"date": unit["date"].isoformat(), "direction": move_direction,
                 "scope": unit["scope"].replace("_", "-"),
                 "assets": [f"{a['ticker']} ({a['name']}, {a['sector']})" for a in unit["alerts"]]},
        "claim": exp.cause_summary,
        "sources": [{"title": s.title, "published": (seen.get(s.url) or {}).get("published"),
                     "text": (seen.get(s.url) or {}).get("snippet", "")} for s in exp.sources],
    }


def decide(nouls: dict[str, float], category: tuple[str, float] | None,
           titles: list[str]) -> dict:
    """Turn Jev's answers into a verdict. Pure, so it is testable offline.

    nouls: question id -> probability of yes. category: (choice, confidence),
    or None when no cause was claimed. titles: the cited sources, in order.
    """
    if category is None:
        named = nouls["names_cause"]
        ok = named < 1 - YES
        return {"verified": ok, "category": "unknown", "confidence": "low",
                "reason": "no source in the search window explains the move",
                "kept": [],
                "problems": [] if ok else [
                    f"the claim names a cause ({named:.2f}) but cites no source: cite the "
                    "sources that report it, or say no clear cause was found"]}
    problems = []
    d = nouls.get("direction")
    if d is not None and d < YES:
        problems.append(f"direction ({d:.2f}): the cause as written does not clearly push the "
                        "price the way it moved")
    # A source that does not report the cause is dropped, not fatal: one weak
    # link among several good ones says nothing against the cause. At least one
    # source must report it.
    support = [nouls[f"supports_{i}"] for i in range(len(titles))]
    kept = [i for i, p in enumerate(support) if p >= YES]
    if not kept:
        problems.append("no cited source reports the claimed cause: "
                        + ", ".join(f"{t[:60]} ({p:.2f})" for t, p in zip(titles, support)))
    g = nouls["grounded"]
    if g < YES:
        problems.append(f"grounded ({g:.2f}): the sources do not report the cause the claim gives")
    choice, conf = category
    if choice == "unknown":
        problems.append("category: the cause fits none of the fixed categories")
    elif conf < CATEGORY_FLOOR:
        problems.append(f"category ({choice}, confidence {conf:.2f}): the cause fits no single "
                        "category clearly")
    checks = [x for x in (d, g, *(support[i] for i in kept)) if x is not None]
    high = min(checks) >= HIGH and conf >= 0.8
    parts = ([f"cause fits the direction {d:.2f}"] if d is not None else []) + [
        "each source reports it " + ", ".join(f"{support[i]:.2f}" for i in kept),
        f"sources report the cause {g:.2f}", f"category {conf:.2f}"]
    return {"verified": not problems, "category": choice,
            "confidence": "low" if problems else ("high" if high else "medium"),
            "reason": "Jev probability that " + "; ".join(parts), "problems": problems,
            "kept": kept}


def verify(unit: dict, exp: Explanation, seen: dict, classifier: TypeSafeClassifier,
           config: dict) -> tuple[dict, dict]:
    """One Jev call for a draft that passed the code checks: (decision, audit record)."""
    move_direction = direction(unit)
    state = jev_state(unit, exp, seen, move_direction)
    resp = classifier.invoke({"state": state, "questions": jev_questions(exp, move_direction)},
                             config=config)
    nouls = {k: v.noul for k, v in resp.nouls.items()}
    cat = resp.choices.get("category")
    decision = decide(nouls, (cat.choice, cat.confidence) if cat else None,
                      [s.title for s in exp.sources])
    record = {"model": resp.model, "request_id": resp.request_id, "state": state,
              "nouls": nouls,
              "category": {"choice": cat.choice, "confidence": cat.confidence,
                           "probabilities": cat.probabilities} if cat else None,
              "decision": decision}
    return decision, record
