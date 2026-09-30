# The deep agent

A coordinator with two specialists over the same marts the chat agent reads,
built on `deepagents` and served by LangGraph. It answers questions and writes
fact-checked briefs, and like every other harness in this repo it is
propose-only: nothing it does writes to the warehouse.

```bash
cd deep_agent
uv sync
cp .env.example .env      # then paste LANGSMITH_API_KEY
./start.sh                # server :2024 + Studio + chat UI :3000
uv run python test_diagnostic.py
```

The Snowflake reader identity and `ANTHROPIC_API_KEY` come from `../agent/.env`,
shared with the chat agent rather than copied. This folder's `.env` holds only
LangSmith, engine, and model settings, and it overrides the shell, the same way
`langgraph dev` treats it.

## Shape

| Part | Where | What it owns |
|---|---|---|
| Coordinator | `agent.py` | Plans, delegates, writes; `render_bar_chart` |
| warehouse-analyst | `subagents.py` | `run_sql` + `get_schema`, the only path to data |
| fact-checker | `subagents.py` | No tools; PASS/FAIL on a draft against its SQL evidence |
| Operating manual | `workspace/AGENTS.md` | Data rules, who does what, house rules |
| Analyst memory | `workspace/agents/warehouse-analyst/AGENTS.md` | Schema notes it records itself |
| Playbooks | `workspace/skills/*/SKILL.md` | mover-analysis, sector-snapshot, weekly-brief |
| Deliverables | `workspace/outputs/` | Notes and charts, named by as-of date |

The file backend is rooted at `workspace/`, so the agent's file tools cannot
read this code or `.env`.

## Guardrails

1. **One path to the data.** `run_sql` is only on the analyst. deepagents adds
   a general-purpose subagent that inherits the coordinator's tools, so a data
   tool on the coordinator would be reachable around the analyst.
2. **Read-only three times over.** Statement-shape check, then
   `agent/query.py`'s `QueryRunner` checks again, then the REPORTER role's
   grants refuse any write.
3. **Budget per delegation.** 12 queries per `task` call, counted from the
   subagent's own messages, so threads on one server cannot share a counter.
4. **Memory changes need approval.** Writes to `AGENTS.md`, `/agents/**`, and
   `/skills/**` pause for approve / edit / reject. Writes to `/outputs/**` go
   through. Every other write is denied.
5. **Fact-check before delivery.** Numbers, dates, direction words,
   superlatives, and scope claims are checked against the rows in a fresh
   context.

## Observability

With `LANGSMITH_TRACING=true`, every coordinator step, delegation, and query
lands as a trace in the `LANGSMITH_PROJECT` project. `langgraph dev` also
opens LangSmith Studio against the local server. `start.sh` launches the
course's agent-chat-ui (`AGENT_CHAT_UI_DIR`, default
`~/lca-deepagents/agent-chat-ui`), which renders approvals as buttons.

## Diagnostic

`test_diagnostic.py` drives the running server through the chat UI's API and
checks five layers: graph loads, a data answer matches the warehouse, a memory
edit pauses and a reject holds, a write outside `/outputs/` is refused, and the
sector-snapshot playbook writes its note and chart. Expected answers are
queried live, never hardcoded. Pass layer numbers to run a subset:
`uv run python test_diagnostic.py 2 5`.
