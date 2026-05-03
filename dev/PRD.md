# PRD: unwind

## 1. Summary

`unwind` is a local web app that visualizes Claude Code sessions and their call hierarchies for a given project folder, without wrapping or intercepting Claude Code itself.

The user launches Claude Code however they normally do. Separately, they run `unwind` in a project folder. A browser tab opens showing every Claude session that has ever run in that folder, the fork/call tree between them, and a live-updating view of the selected session's conversation.

`unwind` never touches the Claude Code process, never proxies its I/O, never renders its terminal. Everything it shows is derived from on-disk artifacts:

- `~/.claude/projects/<slug>/*.jsonl` — Claude Code's own session logs.
- `<project>/.claude/callstack/log/**` — [callstack](https://github.com/unwind-labs/callstack) plugin invocation artifacts.
- `meta` events inside the JSONLs — used to detect Claude Code's native `/fork` and subagent invocations even when callstack isn't installed.

## 2. Problem

When a user runs Claude Code in a terminal and work forks — either via the `callstack` plugin's `/call` or via Claude Code's experimental `CLAUDE_CODE_FORK_SUBAGENT=1` `/fork` — each child becomes a separate process writing its own JSONL. From any one terminal, you see one session. There is no coherent view of:

- how many Claude sessions are active in this project
- which session spawned which (the call tree)
- what each child is doing right now
- how long a background fork has been running and whether it's stuck

A prior attempt wrapped Claude Code in tmux and rendered captured frames inside a TUI. That approach forced the tool to keep up with Claude Code's terminal rendering forever. The observer architecture avoids the whole problem: we don't render Claude, we render a model of what Claude has written to disk.

## 3. Core insight

Claude Code already persists everything we need. `~/.claude/projects/<slug>/<session-id>.jsonl` is an append-only, structured, timestamped log of every message, tool call, tool result, and meta event. The callstack plugin additionally writes a structured log whenever a parent session invokes a child via `/call`. For native `/fork`, parent/child links can be inferred from JSONL meta events. Together, these sources are sufficient to reconstruct the full session graph without any in-process integration.

The right shape for the product is therefore:

- a passive observer, not a wrapper
- a web UI, not a TUI (call trees and long tool outputs are bad fits for terminal panes)
- zero configuration from the user's side (no hook edits, no env vars, no daemon)

## 4. Product goal

When a user is running Claude Code work in a project folder — especially branching work via `/call` (callstack) or `/fork` (Claude Code native) — `unwind` should give them, in one tab, a complete picture of:

- every session in this project, ordered by recency
- for the selected session: its parent (if any), its children, and its descendants, as a collapsible nested list (and an optional canvas view)
- for the selected session or selected child: the conversation, rendered with tool calls expandable, streaming in live as the JSONL grows

## 5. Non-goals

- Rendering the live Claude Code terminal UI. Claude owns its terminal.
- Forwarding input to Claude. The user types in their own Claude terminal.
- Being an authoring tool. `unwind` is read-only over observed state.
- Cross-project rollup dashboards, analytics, billing, token accounting. Out of scope for v1.
- Modifying any Claude Code state (no delete, no edit, no send).
- Multi-user access. `unwind` binds to `127.0.0.1` only.
- Windows support.

## 6. User experience

### 6.1 Launching

```bash
cd /path/to/project
unwind
```

This:
1. Resolves the CWD to a Claude project slug.
2. Starts a local web server on an ephemeral port bound to `127.0.0.1`.
3. Opens the default browser at `http://127.0.0.1:<port>/`.
4. Stays in the foreground until `Ctrl-C`.

Flags:
- `unwind [path]` — serve a folder other than CWD.
- `unwind --port <n>` — fix the port.
- `unwind --no-browser` — print the URL, don't open.
- `unwind --all` — show a project picker; index every project under `~/.claude/projects/`.
- `unwind --host 0.0.0.0` — bind beyond loopback (no auth; use deliberately).

### 6.2 Layout

Three resizable panes.

**Left — Session list.**
- One row per Claude session in the project.
- Sorted by most-recent activity descending.
- Each row shows: title (derived from first user prompt), status dot (live / idle / done), last-activity timestamp, message count, top-level call count, git branch.
- Root sessions vs forks are distinguished at a glance.

**Middle — Trace / canvas.**
- `TracePane`: collapsible nested list of the call hierarchy rooted at the selected session. Children include both `/call` invocations (from callstack logs) and `/fork` / native subagent invocations (from JSONL meta inference). Each row shows: short task label, child session id, status, elapsed.
- `CanvasPane`: optional canvas-style layout of the same tree using SVG elbow edges, for sessions with wide branching.

**Right — Thread viewer.**
- Renders the conversation of the row selected in the middle pane (or the root session if no node selected).
- Custom shadcn-based renderer (no `assistant-ui` dependency): user/assistant bubbles, markdown, syntax-highlighted code, collapsible tool-call cards pairing `tool_use` with its `tool_result`.
- A toggle hides meta events (snapshots, attachments, sidechain noise) by default.
- New messages stream in without a page reload when the JSONL grows.

### 6.3 Live behavior

- When a JSONL file in the watched project changes, the affected session's message count and last-activity update in the left pane immediately.
- When a new JSONL appears (new session or fork spawned), it shows up in the left pane and — if it's a child of the selected session — in the middle pane.
- When the currently-selected thread's JSONL grows, new messages append to the right pane.
- Status (`live` / `idle` / `done`) is project-scoped: `live` if a JSONL was modified in the last ~45 s and a `claude` process is running in the project cwd; `idle` if the process exists but the JSONL is quiet; `done` otherwise. Per-session PID mapping is not possible today because Claude Code doesn't expose the running session id.

### 6.4 Empty and error states

- No sessions for this project yet → empty state with the project slug shown and a hint to run Claude Code here.
- Can't read `~/.claude/projects/` → error banner; CLI exits 1.
- Malformed JSONL line → skipped silently with a debug-log entry; the render never crashes.

## 7. Data model

### 7.1 Session

```
Session {
  id:                  string   # Claude session uuid
  project_slug:        string
  project_path:        string
  title:               string   # first user prompt, truncated
  custom_title:        string | null
  first_timestamp:     datetime
  last_timestamp:      datetime
  message_count:       int
  top_level_call_count: int
  cwd:                 string | null
  git_branch:          string | null
  status:              "live" | "idle" | "done"
}
```

### 7.2 Call edge

Sources, in priority order:
1. `<project>/.claude/callstack/log/<invoke_id>/report.yaml` — explicit `/call` invocations.
2. JSONL meta events for native `/fork` and subagent dispatch — inferred via `subagents.py` + `fork_detect.py`.

```
CallEdge {
  invoke_id:           string            # e.g. 20260422T152829-c33d926b (callstack) or synthesized
  parent_session_id:   string
  child_session_id:    string | null     # null until child JSONL is created
  task:                string            # /call argument or /fork directive
  kind:                "call" | "fork" | "subagent"
  started_at:          datetime
  finished_at:         datetime | null
  status:              "running" | "complete" | "failed"
}
```

A single `invoke_parallel` `/call` produces N edges sharing an `invoke_id`.

### 7.3 Message

Normalized from a JSONL line:

```
Message {
  uuid:                string
  parent_uuid:         string | null     # intra-session threading
  session_id:          string
  timestamp:           datetime
  role:                "user" | "assistant" | "system" | "tool"
  type:                "message" | "tool_use" | "tool_result" | "attachment" | "snapshot" | …
  content:             structured        # text | tool_use block | tool_result block
  meta:                { promptId, model, cwd, … }
  spawn:               { kind, child_session_id, task } | null   # annotated when this message kicked off a child
}
```

## 8. Architecture

One `uvicorn` process, owned by the `unwind` CLI. It hosts the FastAPI app (REST + WebSocket), a `watchdog` Observer thread, and the prebuilt frontend bundle served as static files.

### 8.1 API surface

- `GET /api/health`
- `GET /api/projects` — projects under `~/.claude/projects/`, with last-activity timestamps.
- `GET /api/projects/default` — the project resolved from CWD at launch.
- `POST /api/projects/pick-folder` — invokes the native folder picker (`--all` mode).
- `GET /api/projects/{slug}/sessions` — session list for one project.
- `GET /api/projects/{slug}/sessions/{session_id}` — session metadata.
- `GET /api/projects/{slug}/sessions/{session_id}/messages?since_uuid=` — thread messages.
- `GET /api/projects/{slug}/sessions/{session_id}/tree` — call hierarchy rooted here.
- `WS /api/ws?project=<slug>` — typed event stream.

### 8.2 Live update protocol

WebSocket events the server emits:

```
{ "type": "session_updated",   "session_id": "...", "last_timestamp": "...", "message_count": 42, "status": "live" }
{ "type": "session_created",   "session_id": "...", "title": "..." }
{ "type": "call_edge",         "invoke_id": "...", "parent": "...", "child": "..." }
{ "type": "messages_appended", "session_id": "...", "since_uuid": "...", "messages": [...] }
```

The frontend uses these to patch the TanStack Query cache; panes re-render from cache without polling.

### 8.3 Packaging

- Build backend: `poetry-core` (PEP 621 `[project]` table).
- Frontend: `npm run build` in `web/` emits directly into `src/unwind/static/`.
- Single PyPI wheel `unwind` includes the static bundle as package data; one `pip install` ships the full app.

## 9. Success criteria

With `unwind` running in `examples/parallel_calls`:

1. The left pane lists every session in that folder, live-updating when a new one starts.
2. Selecting a root session with `callstack` children shows the full fork tree in the middle pane, with descendants nested correctly.
3. Selecting a session that used native `/fork` also shows the fork as a child node, derived from JSONL meta events without callstack logs.
4. Clicking a child row loads that child's conversation in the right pane, with tool calls collapsible and code syntax-highlighted.
5. Starting a new `/call` or `/fork` in Claude Code causes a new row to appear in the middle pane within ~1 second, without a manual refresh.
6. The `unwind` process is stopped with `Ctrl-C` and leaves no background state.

## 10. Risks and open questions

- **Per-session live PID mapping.** Claude Code doesn't expose a running session id on its process. Status is project-scoped today. Watch upstream for an exposure path.
- **Large JSONLs.** Some sessions reach tens of MB; the thread loads in one shot today. Pagination is post-v1.
- **Native `/fork` inference correctness.** Detection lives in `subagents.py` + `fork_detect.py`. As `/fork` evolves upstream, the inference may need updates.
- **Out-of-order JSONL appends.** Mostly in order, but parallel internal activity can cause late arrivals; the renderer tolerates them by uuid-keying.
