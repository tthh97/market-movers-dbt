"""
The orchestration loop.

Deliberately a hand-written loop rather than the SDK's tool runner: the point of
this harness is that every component is visible and separately debuggable. The
loop is the component that owns stopping conditions and tracing, so both live
here as lines in the loop rather than as decorators or callbacks around it.

Three independent stopping conditions, because each catches a different failure:

  MAX_TURNS    - the model keeps calling tools and never concludes
  TOKEN_BUDGET - the conversation grows expensive without looping visibly
  query budget - enforced in tools.py, so a chatty agent cannot hammer the
                 warehouse even inside a single turn

Usage:
    python3 agent.py "which sector moved most on the latest trading day?"
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

from dotenv import load_dotenv

import policy
import tools

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")
MAX_TURNS = 10
TOKEN_BUDGET = 200_000       # cumulative input+output across the run
MAX_TOKENS = 4096

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE_DIR = os.path.join(HERE, "traces")


class Trace:
    """One JSONL file per run, one line per turn, plus a row in summary.csv.

    Written as the run proceeds rather than at the end, so a run that hangs or
    crashes still leaves evidence of how far it got.
    """

    def __init__(self, question: str):
        os.makedirs(TRACE_DIR, exist_ok=True)
        self.id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.path = os.path.join(TRACE_DIR, f"{self.id}.jsonl")
        self.started = time.time()
        self.question = question
        self.in_tok = self.out_tok = 0
        self.queries = 0
        self._write({"event": "run_start", "question": question, "model": MODEL})

    def _write(self, obj: dict) -> None:
        obj["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        obj["trace_id"] = self.id
        with open(self.path, "a") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    def turn(self, n: int, resp, tool_calls: list[dict]) -> None:
        self.in_tok += resp.usage.input_tokens
        self.out_tok += resp.usage.output_tokens
        self.queries += sum(1 for c in tool_calls if c["name"] == "run_sql")
        self._write({
            "event": "turn",
            "turn": n,
            "stop_reason": resp.stop_reason,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0),
            "tool_calls": tool_calls,
        })

    def finish(self, answer: str, reason: str) -> None:
        elapsed = round(time.time() - self.started, 1)
        self._write({
            "event": "run_end", "stop": reason, "answer": answer,
            "turns_used": None, "elapsed_s": elapsed,
            "total_input_tokens": self.in_tok, "total_output_tokens": self.out_tok,
            "queries": self.queries,
        })
        summary = os.path.join(TRACE_DIR, "summary.csv")
        new = not os.path.exists(summary)
        with open(summary, "a") as f:
            if new:
                f.write("trace_id,question,stop,elapsed_s,input_tokens,output_tokens,queries\n")
            q = self.question.replace('"', "'")
            f.write(f'{self.id},"{q}",{reason},{elapsed},{self.in_tok},{self.out_tok},{self.queries}\n')


def _text(resp) -> str:
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def run(question: str, verbose: bool = True) -> str:
    import anthropic

    client = anthropic.Anthropic()
    tools.reset_budget()
    trace = Trace(question)
    messages = [{"role": "user", "content": question}]

    try:
        for turn in range(1, MAX_TURNS + 1):
            if trace.in_tok + trace.out_tok > TOKEN_BUDGET:
                answer = _text(resp) if turn > 1 else ""
                note = "Stopped: token budget reached before a final answer."
                trace.finish(answer or note, "token_budget")
                return answer or note

            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=policy.system_for(question),
                tools=tools.TOOL_SCHEMAS,
                messages=messages,
            )

            calls = [{"name": b.name, "input": b.input}
                     for b in resp.content if b.type == "tool_use"]
            trace.turn(turn, resp, calls)

            if resp.stop_reason != "tool_use":
                answer = _text(resp)
                trace.finish(answer, resp.stop_reason)
                return answer

            messages.append({"role": "assistant", "content": resp.content})

            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if verbose:
                    detail = block.input.get("query", "") if block.name == "run_sql" else ""
                    print(f"  [{block.name}] {detail[:110]}", file=sys.stderr)
                out = tools.dispatch(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                    # A tool that returned an error is still a completed call.
                    # Flagging it lets the model see the failure explicitly.
                    "is_error": out.startswith(("ERROR:", "SQL ERROR:", "SCHEMA ERROR:")),
                })
            messages.append({"role": "user", "content": results})

        answer = _text(resp)
        note = answer or f"Stopped after {MAX_TURNS} turns without reaching an answer."
        trace.finish(note, "max_turns")
        return note
    finally:
        tools.close()


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python3 agent.py "<question>"', file=sys.stderr)
        raise SystemExit(2)
    question = " ".join(sys.argv[1:])
    answer = run(question)
    print("\n" + answer)


if __name__ == "__main__":
    main()
