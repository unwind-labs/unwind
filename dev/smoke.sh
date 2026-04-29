#!/usr/bin/env bash
# Smoke test: starts the server against examples/parallel_calls, hits every
# endpoint over HTTP + WS, then shuts down.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
SAMPLE="${SAMPLE_PROJECT:-/Users/amolk/work/agent-callstack/agent-callstack/examples/parallel_calls}"
PORT=${PORT:-8767}
SLUG=$(python3 -c "import re,sys,pathlib; p=pathlib.Path(sys.argv[1]).resolve(); print(re.sub(r'[/._]','-',str(p)))" "$SAMPLE")

source "$HERE/.venv/bin/activate"

echo "=== starting uvicorn on :$PORT"
UNWIND_DEFAULT_PATH="$SAMPLE" UNWIND_DEFAULT_SLUG="$SLUG" \
  uvicorn unwind.server:create_app --factory --port "$PORT" --log-level warning &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 2

echo "=== /api/health"
curl -fsS "http://127.0.0.1:$PORT/api/health" | python3 -m json.tool

echo "=== /api/projects"
curl -fsS "http://127.0.0.1:$PORT/api/projects" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d)} projects')"

echo "=== /api/projects/<slug>/sessions"
COUNT=$(curl -fsS "http://127.0.0.1:$PORT/api/projects/$SLUG/sessions" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
echo "  $COUNT sessions"

# Pick the newest session id for tree/messages tests
SESSION=$(curl -fsS "http://127.0.0.1:$PORT/api/projects/$SLUG/sessions" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['session_id'] if d else '')")
echo "=== newest session: $SESSION"

if [ -n "$SESSION" ]; then
  echo "=== /tree"
  curl -fsS "http://127.0.0.1:$PORT/api/projects/$SLUG/sessions/$SESSION/tree" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"  has_logs={d['has_callstack_logs']} children={len(d['children'])}\")"
  echo "=== /messages"
  curl -fsS "http://127.0.0.1:$PORT/api/projects/$SLUG/sessions/$SESSION/messages" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"  msgs={len(d['messages'])}\")"
fi

echo "=== ws handshake"
python3 - <<PY
import asyncio, websockets
async def main():
    uri = "ws://127.0.0.1:$PORT/api/ws?project=$SLUG"
    async with websockets.connect(uri) as ws:
        msg = await asyncio.wait_for(ws.recv(), 2)
        print("  ready:", msg[:80])
asyncio.run(main())
PY

echo "=== all good"
