"""
Research agent: explain why each flagged price move happened.

Detection is done. fct_price_alerts (dbt, SQL only) decides which moves count,
how big they were, and whether each was market-wide, sector-wide, or specific
to one stock. This script only asks "why", once per research unit (one cause
per market-wide day, per sector-wide day, or per stock move), and writes the
answers as JSON lines. It never computes or changes a number, and it never
writes to the warehouse: deep_agent/load_explanations.py loads the file as the
loader role, the same propose-then-load split as the rest of the project.

Per unit:
    investigator (strong model)
      -> news-researcher subagent (fast model, date-windowed news search)
      -> structured draft: the cause in words, and the sources it rests on
    then code checks what code can check: every cited source was really
    returned by the search tool in this run, with a publish date inside the
    move's window. A source the model wrote from memory fails here no matter
    how plausible it looks.
    then Jev (TypeSafe) makes the typed decisions in one call, reading the
    move, the claim, and the search snippets of the cited sources: does the
    cause push the price the way it moved, does each source report it, do the
    sources together report the cause, and which category it is. Code applies
    the thresholds, so the verdict and the category are never text an LLM
    wrote. A failed draft goes back to the investigator with the exact
    problems, at most twice, and every Jev answer is kept in the run folder.

    uv run python research.py              # latest 8 unexplained units
    uv run python research.py --limit 3    # fewer
    uv run python research.py --unit <research_unit_id>   # redo one unit
    uv run python load_explanations.py runs/<run_id>/explanations.jsonl   # as LOADER
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import uuid

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"), override=True)
load_dotenv(os.path.join(os.path.dirname(HERE), "agent", ".env"), override=False)

from deepagents import create_deep_agent  # noqa: E402
from langchain.chat_models import init_chat_model  # noqa: E402
from langchain.tools import tool  # noqa: E402
from langchain_typesafe import Choice, Noul, NoulCriteria, TypeSafeClassifier  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from tavily import TavilyClient  # noqa: E402

# The read-only QueryRunner: REPORTER on Snowflake, read_only DuckDB offline.
from tools import _get_runner  # noqa: E402

STRONG = os.environ.get("DEEP_AGENT_MODEL", "anthropic:claude-sonnet-5")
FAST = os.environ.get("RESEARCH_FAST_MODEL", "anthropic:claude-haiku-4-5")
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
CATEGORIES = tuple(CATEGORY_CRITERIA)

# Jev thresholds. A noul is the probability the answer is yes. On the first
# eight live units, drafts the sources plainly support scored 0.62-0.94 and
# unsupported ones 0.10-0.18, so the bar is the documented even-cost default of
# 0.5, not a stricter one that rejected correct causes. Set on those eight
# units only: the hand-labelled eval set is what should move it. HIGH is the
# bar for the "high" confidence label.
YES, HIGH, CATEGORY_FLOOR = 0.5, 0.85, 0.5

# A source counts for a move on day D if it was published from D-3 to D+1.
# The day after is allowed because a close-to-close move is often reported the
# next morning. Anything later is hindsight, anything earlier is stale.
WINDOW_BEFORE, WINDOW_AFTER = 3, 1


# ---------------------------------------------------------------------------
# The search tool. Every result it returns is recorded, so the code check can
# later confirm a cited URL came from a real search in this run.
# ---------------------------------------------------------------------------

_tavily = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY"))
_SEEN: dict[str, dict] = {}   # url -> {"title", "published", "snippet"}, reset per unit


def _iso(published: str | None) -> str | None:
    """Tavily dates arrive as RFC 1123 or ISO. Normalise to YYYY-MM-DD."""
    if not published:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(published.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", published)
    return m.group(1) if m else None


@tool
def news_search(query: str, start_date: str, end_date: str) -> str:
    """Search news published between start_date and end_date (YYYY-MM-DD).

    Use the window you were given for the move. Returns up to 6 results with
    url, title, published date, and a snippet. Prefer primary sources: company
    filings and press releases, regulators, exchanges, the EIA or OPEC for oil
    and gas. Run a few focused queries rather than one broad one.
    """
    try:
        res = _tavily.search(
            query, topic="news", start_date=start_date, end_date=end_date,
            max_results=6, search_depth="advanced",
        )
    except Exception as e:  # returned so the agent can adjust the query
        return f"SEARCH ERROR: {e}"
    out = []
    for r in res.get("results", []):
        url = r.get("url")
        if not url:
            continue
        published = _iso(r.get("published_date"))
        snippet = (r.get("content") or "")[:600]
        _SEEN[url] = {"title": r.get("title", ""), "published": published, "snippet": snippet}
        out.append({"url": url, "title": r.get("title", ""), "published": published,
                    "snippet": snippet})
    return json.dumps(out, indent=1) if out else "No results in that window."


# ---------------------------------------------------------------------------
# The agent.
# ---------------------------------------------------------------------------

class Source(BaseModel):
    url: str
    title: str
    published: str = Field(description="YYYY-MM-DD, as the search tool reported it")


class Explanation(BaseModel):
    cause_summary: str = Field(description="One or two sentences. What happened, not advice. "
                                           "If nothing explains the move, say so and name no cause.")
    sources: list[Source] = Field(description="Only URLs the news-researcher returned. Empty when no cause was found.")


RESEARCHER_PROMPT = """\
You are a news researcher. You get one price move: the ticker(s), the date, \
the direction and size, and a date window. Find what caused it.

How to work:
1. Run 2 to 4 focused news_search queries inside the window you were given. \
Name the company or commodity and the event type, e.g. "Nvidia earnings \
guidance", "OPEC output cut crude", "Bitcoin ETF outflows".
2. Prefer primary sources (filings, press releases, regulators, EIA/OPEC) and \
major wires over opinion pieces.
3. For a sector-wide or market-wide move, look for the shared cause (a macro \
release, a regulator, a sector peer's results), not each name separately.

Reply with up to 3 candidate causes. For each: one sentence, then the url, \
title, and published date exactly as news_search returned them, and whether \
the direction in the source matches the move. If nothing in the window \
explains it, say "no clear cause found". Never cite a URL that news_search \
did not return.\
"""

INVESTIGATOR_PROMPT = """\
You explain why a flagged price move happened. The move itself is fixed by \
SQL and given to you: never restate a figure that is not in the task, and \
never compute one.

How to work:
1. Delegate the search to the news-researcher with the move facts and the \
exact date window.
2. Return one explanation: the cause in one or two sentences, and the \
sources that report it. Cite only sources the researcher returned, with \
their published date. Say only what those sources report. For a name that \
moved because a peer or its sector did, name that peer or sector news.
3. If nothing in the window explains the move, say that no clear cause was \
found and return no sources. That is a correct answer, not a failure.

Your explanation is then checked: that each source was returned by the \
search inside the window, that the cause pushes the price the way it moved, \
and that each source reports the cause. If a check fails you get the exact problems back. Fix exactly \
those, or fall back to no clear cause.\
"""


def build_agent():
    strong = init_chat_model(STRONG, timeout=120, max_retries=2)
    fast = init_chat_model(FAST, timeout=120, max_retries=2)
    return create_deep_agent(
        model=strong,
        system_prompt=INVESTIGATOR_PROMPT,
        subagents=[
            {
                "name": "news-researcher",
                "description": "Searches dated news for the cause of one price move. Give it the move facts and the date window.",
                "system_prompt": RESEARCHER_PROMPT,
                "tools": [news_search],
                "model": fast,
            },
        ],
        response_format=Explanation,
        name="Alert_Investigator",
    )


# ---------------------------------------------------------------------------
# Input: research units from the warehouse, newest first.
# ---------------------------------------------------------------------------

Q_ALERTS = """
select a.alert_id, a.research_unit_id, a.ticker, w.name, a.sector, a.trade_date,
       a.daily_return, a.z, a.move_scope, a.severity, a.market_z,
       a.peers_moving_with, a.peer_n
from fct_price_alerts a
join stg_watchlist w using (ticker)
order by a.trade_date desc, abs(a.z) desc
"""

# Skip what was explained recently. An "unknown" or a rejected explanation is
# retried after a day, since news often lands late.
Q_DONE = """
select research_unit_id, category, verifier_status, researched_at
from fct_alert_explanations
"""


def load_units(limit: int, only: set[str] | None = None) -> list[dict]:
    runner = _get_runner()
    res = runner.run(Q_ALERTS, limit=None)
    if res.is_error:
        raise SystemExit(f"Could not read fct_price_alerts: {res.error}")
    cols = [c.lower() for c in res.columns]
    alerts = [dict(zip(cols, r)) for r in res.rows]

    done = runner.run(Q_DONE, limit=None)
    skip = set()
    if not done.is_error:
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        for unit_id, category, status, at in done.rows:
            recent = at and (now - at) < dt.timedelta(hours=24)
            settled = status == "verified" and category != "unknown"
            if settled or recent:
                skip.add(unit_id)
    elif "does not exist" not in done.error.lower():
        raise SystemExit(f"Could not read fct_alert_explanations: {done.error}")

    units: dict[str, dict] = {}
    for a in alerts:
        if only is not None:
            if a["research_unit_id"] not in only:
                continue
        elif a["research_unit_id"] in skip:
            continue
        u = units.setdefault(a["research_unit_id"], {
            "research_unit_id": a["research_unit_id"], "date": a["trade_date"],
            "scope": a["move_scope"], "sector": a["sector"], "market_z": a["market_z"],
            "alerts": []})
        u["alerts"].append(a)
    return list(units.values())[:limit]


def describe(unit: dict) -> tuple[str, str, str]:
    d = unit["date"]
    start = (d - dt.timedelta(days=WINDOW_BEFORE)).isoformat()
    end = (d + dt.timedelta(days=WINDOW_AFTER)).isoformat()
    scope = {"market_wide": "market-wide (the S&P 500 moved hard the same way)",
             "sector": f"sector-wide ({unit['sector']} peers moved with it)",
             "stock": "specific to this name (its sector and the market did not move with it)"}[unit["scope"]]
    lines = [f"Move date: {d.isoformat()} (close to close)",
             f"Scope: {scope}",
             f"Search window: {start} to {end}", "", "Alerting names:"]
    for a in unit["alerts"]:
        lines.append(
            f"- {a['ticker']} ({a['name']}, {a['sector']}): {a['daily_return'] * 100:+.2f}% "
            f"on the day, {abs(a['z']):.1f}x its own typical daily move, "
            f"{a['peers_moving_with']} of {a['peer_n']} sector peers moved the same way")
    if unit["market_z"] is not None:
        lines.append(f"S&P 500 (SPY) that day: z {unit['market_z']:+.1f}")
    return "\n".join(lines), start, end


# ---------------------------------------------------------------------------
# Verification. Code checks what code can check. Jev makes the typed
# decisions, and code applies the thresholds to its answers. No verdict and no
# category is ever read out of text a model wrote.
# ---------------------------------------------------------------------------

def code_checks(exp: Explanation, start: str, end: str) -> list[str]:
    problems = []
    for s in exp.sources:
        seen = _SEEN.get(s.url)
        if seen is None:
            problems.append(f"source not returned by search in this run: {s.url}")
            continue
        published = seen["published"]
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


def jev_state(unit: dict, exp: Explanation, move_direction: str) -> dict:
    return {
        "move": {"date": unit["date"].isoformat(), "direction": move_direction,
                 "scope": unit["scope"].replace("_", "-"),
                 "assets": [f"{a['ticker']} ({a['name']}, {a['sector']})" for a in unit["alerts"]]},
        "claim": exp.cause_summary,
        "sources": [{"title": s.title, "published": (_SEEN.get(s.url) or {}).get("published"),
                     "text": (_SEEN.get(s.url) or {}).get("snippet", "")} for s in exp.sources],
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


def verify(unit: dict, exp: Explanation, classifier: TypeSafeClassifier,
           config: dict) -> tuple[dict, dict]:
    move_direction = direction(unit)
    state = jev_state(unit, exp, move_direction)
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


MAX_REVISIONS = 2


def research(unit: dict, agent, classifier: TypeSafeClassifier, run_id: str,
             audit) -> list[dict]:
    facts, start, end = describe(unit)
    _SEEN.clear()
    config = {
        "run_name": f"explain {unit['date']} {unit['scope']}",
        "tags": ["alert-research", f"run:{run_id}", f"date:{unit['date']}"],
        "metadata": {"research_unit_id": unit["research_unit_id"],
                     "alert_ids": [a["alert_id"] for a in unit["alerts"]]},
        "recursion_limit": 60,
    }
    messages = [{"role": "user", "content": facts}]
    for attempt in range(MAX_REVISIONS + 1):
        result = agent.invoke({"messages": messages}, config=config)
        exp: Explanation = result["structured_response"]
        problems = code_checks(exp, start, end)
        if problems:
            # A made-up or out-of-window source is not worth a Jev call.
            decision = {"verified": False, "category": "unknown", "confidence": "low",
                        "reason": "failed the code checks before verification",
                        "problems": problems, "kept": list(range(len(exp.sources)))}
            jev_model = None
            audit.write(json.dumps({"research_unit_id": unit["research_unit_id"],
                                    "attempt": attempt, "code_checks": problems}) + "\n")
        else:
            decision, record = verify(unit, exp, classifier, {
                **config, "run_name": f"jev verify {unit['date']} {unit['scope']}"})
            jev_model = record["model"]
            audit.write(json.dumps({"research_unit_id": unit["research_unit_id"],
                                    "attempt": attempt, **record}) + "\n")
        audit.flush()
        if decision["verified"] or attempt == MAX_REVISIONS:
            break
        messages = result["messages"] + [{"role": "user", "content": (
            "The checks rejected this explanation:\n"
            + "\n".join(f"- {p}" for p in decision["problems"])
            + "\nFix exactly these, searching again if needed, or say no clear cause was found.")}]

    notes = "passed" if decision["verified"] else "; ".join(decision["problems"])
    dropped = [s for i, s in enumerate(exp.sources) if i not in decision["kept"]]
    if decision["verified"] and dropped:
        notes += "; dropped sources that do not report the cause: " + ", ".join(s.url for s in dropped)
    if attempt:
        notes += f" (after {attempt} revision{'s' if attempt > 1 else ''})"
    # Record the dates the search tool reported, not the model's copy of them.
    sources = [{"url": s.url, "title": s.title,
                "published": (_SEEN.get(s.url) or {}).get("published") or s.published}
               for i, s in enumerate(exp.sources) if i in decision["kept"]]
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    model = f"{STRONG} + {FAST}" + (f"; verifier typesafe:{jev_model}" if jev_model else "")
    return [{
        "alert_id": a["alert_id"], "research_unit_id": unit["research_unit_id"],
        "cause_summary": exp.cause_summary, "category": decision["category"],
        "sources": json.dumps(sources), "confidence": decision["confidence"],
        "confidence_reason": decision["reason"],
        "verifier_status": "verified" if decision["verified"] else "rejected",
        "verifier_notes": notes[:1000], "model": model, "run_id": run_id,
        "researched_at": now,
    } for a in unit["alerts"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8, help="research units per run")
    ap.add_argument("--unit", action="append", default=None,
                    help="research_unit_id to (re)research; repeatable, ignores the skip rules")
    args = ap.parse_args()

    units = load_units(args.limit, set(args.unit) if args.unit else None)
    if not units:
        print("Nothing to research.")
        return
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    out_dir = os.path.join(HERE, "runs", run_id)
    os.makedirs(out_dir)
    out_path = os.path.join(out_dir, "explanations.jsonl")
    agent = build_agent()
    classifier = TypeSafeClassifier(model=JEV, timeout=60)

    print(f"run {run_id}: {len(units)} research units")
    with open(out_path, "w") as f, open(os.path.join(out_dir, "jev.jsonl"), "w") as audit:
        for u in units:
            tickers = ", ".join(a["ticker"] for a in u["alerts"])
            try:
                rows = research(u, agent, classifier, run_id, audit)
            except Exception as e:  # one failed unit does not sink the run
                print(f"  ! {u['date']} {u['scope']:<11} {tickers}: {type(e).__name__}: {e}")
                continue
            for r in rows:
                f.write(json.dumps(r) + "\n")
            f.flush()
            r = rows[0]
            print(f"  {u['date']} {u['scope']:<11} {tickers:<24} {r['verifier_status']:<8} "
                  f"{r['category']:<16} {r['confidence']:<6} {r['cause_summary'][:90]}")
    print(f"\nwrote {os.path.relpath(out_path, HERE)}")


if __name__ == "__main__":
    main()
