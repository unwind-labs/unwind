# unwind

Local web observer for Claude Code sessions. Run `unwind` in a project folder; a browser tab opens showing every Claude Code session that has run there, the call tree between forked sessions, and a live-updating view of each session's conversation.

`unwind` is an **outside-in observer**. It never wraps or intercepts Claude Code. It reads:

- `~/.claude/projects/<slug>/*.jsonl` — Claude Code's own session logs
- `<project>/.claude/callstack/log/` — [callstack](https://github.com/amolk/agent-callstack) plugin invocation reports

…and renders them in a web UI. Claude Code itself keeps running in your terminal, untouched.

## What you get

- **Left pane** — every session in the project, newest first. Status dot (green = live, amber = idle, grey = done), title (first user prompt), relative time, message count, git branch.
- **Middle pane** — the call tree rooted at the selected session. Nested rows for callstack forks. Chevrons collapse/expand; each row shows task label, status badge, duration, short session id.
- **Right pane** — the thread for the selected session (or selected tree node). Markdown rendered, tool calls in collapsible cards pairing `tool_use` with its matching `tool_result`, meta events togglable.
- **Live updates** — new sessions, new messages, and new callstack children appear without a refresh, driven by a WebSocket fed from a `watchdog` filesystem observer.
- **Keyboard** — `j`/`k` step through sessions, `/` focuses search.
- **`--all`** — project picker across every `~/.claude/projects/` entry.

## Install

```bash
pip install unwind
```

From source:

```bash
git clone https://github.com/amolk/agent-callstack
cd agent-callstack/unwind
poetry install
cd web && npm install && npm run build && cd ..
poetry run unwind
```

## Usage

```bash
cd /any/claude-project
unwind
```

A browser tab opens on an ephemeral `127.0.0.1` port with that project's sessions.

Flags:

| Flag | Purpose |
| ---- | ------- |
| `unwind [path]` | Serve a folder other than CWD. |
| `unwind --port 8765` | Fix the port. |
| `unwind --no-browser` | Print the URL, don't open. |
| `unwind --all` | Project picker over every `~/.claude/projects/` dir. |
| `unwind --host 0.0.0.0` | Bind beyond loopback (use with care — no auth). |

## Dev loop

Two terminals:

```bash
# terminal 1 — Python backend with autoreload
poetry shell
UNWIND_DEFAULT_PATH=/some/project \
UNWIND_DEFAULT_SLUG=-some-project \
  uvicorn unwind.server:create_app --factory --reload --port 8765

# terminal 2 — Vite dev server (proxies /api + WebSocket to :8765)
cd web && npm run dev
# open http://localhost:5173
```

Smoke test:

```bash
./dev/smoke.sh
```

## Architecture

See [dev/PRD.md](dev/PRD.md) and [dev/PLAN.md](dev/PLAN.md) for the full design. Short version:

- **Backend** (`src/unwind/`): FastAPI app factory, per-project `SessionIndex` with mtime/size caching, `CallstackIndex` reading `report.yaml`, `ProjectWatcher` (watchdog) publishing typed events to a per-slug `EventBus`, WebSocket endpoint that fans out events to subscribers.
- **Frontend** (`web/`): Vite + React 18 + TypeScript + Tailwind v3 + hand-written shadcn primitives. TanStack Query for REST; a reconnecting WebSocket client patches the query cache so panes update without polling. zustand for UI state.
- **Packaging**: `npm run build` in `web/` emits directly into `src/unwind/static/`, which is included in the wheel automatically as package data. One `pip install` = full app.

## Scope / non-goals

unwind does **not**:
- run or control Claude Code
- send input to any session
- modify or delete any JSONL or callstack data
- talk to any remote service (binds to `127.0.0.1` by default)

If you want compaction assist, session control, or forking UX, that belongs in Claude Code itself or in a plugin — unwind stays a read-only view.

## Status

Early alpha. Tested only on macOS / Python 3.12. Windows is out of scope for v1. Known rough edges:

- `claude` process detection can't map a PID to a specific session id (Claude doesn't expose that), so status is scoped to the project. A session counts as "live" if its JSONL was modified in the last ~45s *and* any `claude` process is running in the project cwd.
- Very large JSONLs (tens of MB) load the whole thread in one shot. Pagination is a Phase 7 item.
