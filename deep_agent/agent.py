"""
Market Movers deep agent: a coordinator with two specialists.

The coordinator plans, delegates, and writes. The warehouse-analyst is the only
path to the data; the fact-checker reviews anything written before it is
handed over. Most of the behavior lives in markdown under workspace/:

    workspace/AGENTS.md                         operating manual (memory)
    workspace/agents/warehouse-analyst/AGENTS.md  the analyst's own memory
    workspace/skills/<name>/SKILL.md            one playbook per workflow
    workspace/outputs/                          what the agent produces

The backend is rooted at workspace/ rather than this folder, so the agent's
file tools can never read this code or the .env next to it.

Serve it with ./start.sh, or `uv run langgraph dev` on its own.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))

# Two env files with one owner each: this folder's .env holds LangSmith, engine,
# and model settings; agent/.env holds the read-only Snowflake identity and the
# Anthropic key, shared with the chat agent rather than copied. This folder's
# .env overrides the shell, matching what `langgraph dev` does with it, so a
# script and the server always agree on the engine.
load_dotenv(os.path.join(HERE, ".env"), override=True)
load_dotenv(os.path.join(os.path.dirname(HERE), "agent", ".env"), override=False)

from deepagents import FilesystemPermission, create_deep_agent  # noqa: E402
from deepagents.backends import FilesystemBackend  # noqa: E402
from langchain.chat_models import init_chat_model  # noqa: E402

from subagents import build_subagents  # noqa: E402
from tools import render_bar_chart  # noqa: E402

SYSTEM_PROMPT = """\
You are the coordinator of the Market Movers desk: you answer questions about a \
20-ticker watchlist and produce short market write-ups from its analytics marts. \
Follow your operating manual (loaded from memory) and use the matching playbook \
from /skills/ for each task.\
"""

model = init_chat_model(
    os.environ.get("DEEP_AGENT_MODEL", "anthropic:claude-sonnet-5"),
    timeout=120,
    max_retries=2,
)

backend = FilesystemBackend(root_dir=os.path.join(HERE, "workspace"), virtual_mode=True)

# First match wins. Changes to memory or playbooks change how the agent behaves
# next time, so they pause for approval. Drafts for the fact-checker go to
# /drafts/ and deliverables to /outputs/ freely. Nothing else is writable. The
# general-purpose subagent inherits these rules.
PERMISSIONS = [
    FilesystemPermission(
        operations=["write"],
        paths=["/AGENTS.md", "/agents/**", "/skills/**"],
        mode="interrupt",
    ),
    FilesystemPermission(
        operations=["write"], paths=["/drafts/**", "/outputs/**"], mode="allow"
    ),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]

graph = create_deep_agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=[render_bar_chart],
    subagents=build_subagents(backend, model),
    skills=["/skills"],
    memory=["/AGENTS.md"],
    backend=backend,
    permissions=PERMISSIONS,
    name="market-movers-desk",
).with_config({"recursion_limit": 60})
