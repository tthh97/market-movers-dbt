"""
Layered diagnostic for the deep agent, run against the live server.

Each layer checks one capability end to end, through the same API the chat UI
uses. Expected answers are computed from the warehouse at run time, never
hardcoded, because the nightly refreshes the marts.

    ./start.sh                               # terminal 1
    uv run python test_diagnostic.py         # terminal 2

Layers:
  1. graph        - the server loaded the graph
  2. answer       - a data question comes back with the right ticker
  3. memory gate  - "remember X" pauses on edit_file of /AGENTS.md; reject
                    leaves the file unchanged
  4. write deny   - a write outside /outputs/ is refused
  5. skill        - sector-snapshot writes a note and a chart to /outputs/

Layer 2 approves any memory edit it hits, so on a first run the analyst's
schema notes are recorded along the way.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from dotenv import load_dotenv
from langgraph_sdk import get_client

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(HERE, "workspace")
OUTPUTS = os.path.join(WORKSPACE, "outputs")
# Same precedence as agent.py and the server, so both sides use one engine.
load_dotenv(os.path.join(HERE, ".env"), override=True)
load_dotenv(os.path.join(os.path.dirname(HERE), "agent", ".env"), override=False)

from tools import ENGINE, get_runner  # noqa: E402

API_URL = os.environ.get("DEEP_AGENT_API_URL", "http://127.0.0.1:2024")
ASSISTANT = "agent"


def last_ai_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("type") != "ai":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def pending_interrupts(state: dict) -> list[dict]:
    return [i for t in state.get("tasks", []) for i in (t.get("interrupts") or [])]


def interrupted_paths(interrupts: list[dict]) -> list[str]:
    """File paths named in pending edit/write approvals."""
    paths = []
    for intr in interrupts:
        for req in (intr.get("value") or {}).get("action_requests", []):
            path = (req.get("args") or {}).get("file_path")
            if path:
                paths.append(path)
    return paths


async def ask(client, thread_id: str, text: str, on_interrupt: str | None = "approve"):
    """Run one turn. Resolve interrupts with `on_interrupt`, or stop at the
    first one when it is None. Returns (final state, every interrupt seen)."""
    seen: list[dict] = []
    await client.runs.wait(
        thread_id, ASSISTANT, input={"messages": [{"role": "user", "content": text}]}
    )
    for _ in range(6):
        state = await client.threads.get_state(thread_id)
        pending = pending_interrupts(state)
        if not pending:
            return state, seen
        seen.extend(pending)
        if on_interrupt is None:
            return state, seen
        n = sum(len((i.get("value") or {}).get("action_requests", [])) or 1 for i in pending)
        decisions = [{"type": on_interrupt}] * n
        if len(pending) == 1:
            resume = {"decisions": decisions}
        else:
            resume = {
                i["id"]: {"decisions": [{"type": on_interrupt}] * max(1, len(i["value"]["action_requests"]))}
                for i in pending
            }
        await client.runs.wait(thread_id, ASSISTANT, command={"resume": resume})
    raise RuntimeError("still interrupted after 6 resumes")


def expected_top_mover() -> tuple[str, str]:
    """The biggest non-benchmark 1-day return, straight from the warehouse."""
    r = get_runner().run(
        "select ticker, as_of_date from mart_movers where sector <> 'benchmark' "
        "order by ret_1d desc limit 1"
    )
    if r.is_error:
        raise RuntimeError(r.error)
    ticker, as_of = r.rows[0]
    return str(ticker), str(as_of)


async def layer_graph(client):
    graph = await client.assistants.get_graph(ASSISTANT)
    ok = len(graph.get("nodes", [])) > 0
    return ok, f"{len(graph.get('nodes', []))} nodes"


async def layer_answer(client):
    ticker, as_of = expected_top_mover()
    thread = await client.threads.create()
    state, seen = await ask(
        client,
        thread["thread_id"],
        "Excluding benchmarks, which ticker had the biggest 1-day return on its "
        "latest date? Answer in one sentence with the ticker and the date.",
    )
    reply = last_ai_text(state["values"]["messages"])
    ok = ticker in reply
    gated = interrupted_paths(seen)
    note = f"approved memory edits: {gated}" if gated else "no memory edits"
    return ok, f"expected {ticker} ({as_of}); {note}; reply: {reply[:120]!r}"


async def layer_memory_gate(client):
    path = os.path.join(WORKSPACE, "AGENTS.md")
    before = open(path).read()
    thread = await client.threads.create()
    _, seen = await ask(
        client,
        thread["thread_id"],
        "Remember this for the future: I prefer returns shown as percents with "
        "two decimals.",
        on_interrupt="reject",
    )
    after = open(path).read()
    paths = interrupted_paths(seen)
    ok = "/AGENTS.md" in paths and before == after
    return ok, f"interrupt paths {paths}; file unchanged: {before == after}"


async def layer_write_deny(client):
    target = os.path.join(WORKSPACE, "diag-note.txt")
    if os.path.exists(target):
        os.remove(target)
    thread = await client.threads.create()
    await ask(
        client,
        thread["thread_id"],
        "Use write_file to save the text 'hello' to /diag-note.txt, exactly that "
        "path. Then tell me whether it worked.",
        on_interrupt="reject",
    )
    ok = not os.path.exists(target)
    return ok, "file absent" if ok else "file was written"


async def layer_skill(client):
    start = time.time()
    thread = await client.threads.create()
    state, _ = await ask(client, thread["thread_id"], "Give me a sector snapshot.")
    fresh = [
        f for f in os.listdir(OUTPUTS)
        if f.startswith("sector-") and os.path.getmtime(os.path.join(OUTPUTS, f)) >= start
    ]
    has_md = any(f.endswith(".md") for f in fresh)
    has_png = any(f.endswith(".png") for f in fresh)
    reply = last_ai_text(state["values"]["messages"])
    return has_md and has_png, f"new files {sorted(fresh)}; reply: {reply[:100]!r}"


LAYERS = [
    ("1 graph loaded", layer_graph),
    ("2 data answer matches warehouse", layer_answer),
    ("3 memory edit pauses, reject holds", layer_memory_gate),
    ("4 write outside /outputs refused", layer_write_deny),
    ("5 sector-snapshot skill writes note + chart", layer_skill),
]


async def main() -> int:
    only = set(sys.argv[1:])
    client = get_client(url=API_URL)
    failed = 0
    print(f"engine={ENGINE} server={API_URL}")
    for label, fn in LAYERS:
        if only and label.split()[0] not in only:
            continue
        t0 = time.time()
        try:
            ok, detail = await fn(client)
        except Exception as e:  # a crashed layer is a failed layer
            ok, detail = False, f"{type(e).__name__}: {e}"
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}  ({time.time() - t0:.0f}s)\n      {detail}")
    print(f"\n{len(LAYERS) - failed if not only else '-'} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
