#!/usr/bin/env bash
# Start the chat UI, then the agent server (langgraph dev, with Studio).
# Run from the deep_agent directory: ./start.sh
#
# The chat UI is the course's agent-chat-ui fork. Point AGENT_CHAT_UI_DIR at
# another checkout if yours lives elsewhere.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_CHAT_UI_DIR="${AGENT_CHAT_UI_DIR:-$HOME/lca-deepagents/agent-chat-ui}"

OLD_PID=$(lsof -ti :3000 2>/dev/null || true)
if [ -n "$OLD_PID" ]; then
    echo "Port 3000 already in use (PID $OLD_PID) - killing it ..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
fi

if [ -d "$AGENT_CHAT_UI_DIR" ]; then
    echo "Starting agent-chat-ui on http://localhost:3000 ..."
    ENV_FILE="$SCRIPT_DIR/.env" "$AGENT_CHAT_UI_DIR/start.sh" &
    UI_PID=$!
else
    echo "agent-chat-ui not found at $AGENT_CHAT_UI_DIR - starting the server only."
    UI_PID=""
fi

cleanup() {
    [ -n "$UI_PID" ] && kill "$UI_PID" 2>/dev/null || true
    # pnpm run dev spawns `next dev` as a child, so killing the parent alone
    # can leave next-server orphaned.
    pkill -f "next dev" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$SCRIPT_DIR"
uv run langgraph dev "$@"
