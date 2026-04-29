# Implementation Plan — unwind

This document describes the current implementation as built, plus the v1 release polish list. It supersedes the original tracer-bullet plan; phases 0–5 are landed.

## Current architecture

### Backend (`src/unwind/`)

| Module | Role |
|---|---|
| `cli.py` | Typer entry point — resolves CWD → slug, picks port, opens browser, runs uvicorn. |
| `server.py` | FastAPI app factory; mounts API routers and the static frontend bundle. |
| `server_state.py` | Process-wide singletons (registry, default project, etc.). |
| `projects.py` | CWD → slug, `~/.claude/projects/<slug>/` resolution, callstack log dir. |
| `jsonl.py` | Line-streaming parser; `iter_lines`, `extract_session_summary`, `collect_uuids`. |
| `sessions.py` | Per-slug session index with mtime/size caching. |
| `messages.py` | JSONL → normalized `Message` sequence; tool_use ↔ tool_result correlation; spawn-event annotation. |
| `callstack.py` | Reads `<project>/.claude/callstack/log/<invoke_id>/report.yaml` → `CallEdge` list. |
| `subagents.py` | Detects Claude Code's native subagent / `/fork` invocations from JSONL meta events. |
| `fork_detect.py` | Reconstructs cross-session parent/child links when callstack logs aren't available. |
| `processes.py` | `psutil` scan to determine `live` / `idle` / `done` status. |
| `registry.py` | Per-slug caches: session index, callstack index, subagent index, fork detector. |
| `dialog.py` | Native folder picker for the `--all` project picker flow. |
| `events.py` | In-process event hub with WebSocket fan-out. |
| `watcher.py` | `watchdog` observer; debounces filesystem events into typed events. |
| `api/projects.py` | `GET /api/projects`, `GET /api/projects/default`, `POST /api/projects/pick-folder`. |
| `api/sessions_api.py` | `GET /api/projects/{slug}/sessions`, `…/{session_id}`, `…/messages`, `…/tree`. |
| `api/ws.py` | `WS /api/ws?project=<slug>` — typed event stream. |

### Frontend (`web/src/`)

| Module | Role |
|---|---|
| `App.tsx` | Three-pane layout (resizable). |
| `main.tsx` | TanStack Query provider, mount root. |
| `api/client.ts`, `api/types.ts` | REST fetchers + Pydantic-mirroring types. |
| `ws/client.ts` | Reconnecting WebSocket; patches the query cache on each event. |
| `store/ui.ts` | zustand: selected project, selected session, selected tree node, pane sizes. |
| `panes/SessionListPane.tsx` | Left pane — sessions newest-first, status dot, message count. |
| `panes/TracePane.tsx` | Middle pane — nested call tree (callstack + subagent + fork detection). |
| `panes/CanvasPane.tsx` | Optional canvas-style render of the call tree with elbow edges. |
| `panes/CompactCard.tsx` | Right pane — message renderer (no `assistant-ui`; custom shadcn-based). |
| `panes/ElbowEdge.tsx` | SVG elbow connectors for the canvas view. |
| `panes/ProjectPicker.tsx` | `--all` mode home screen. |
| `components/ui/` | Hand-written shadcn primitives (Badge, Collapsible, Resizable, ScrollArea). |

### Packaging

- Build backend: `poetry-core` (PEP 621 `[project]` table).
- `npm run build` in `web/` writes the SPA bundle into `src/unwind/static/`, which is included automatically as package data.
- Distributed as a single PyPI wheel (`unwind`); installs the `unwind` console script.

## v1 release polish (open)

1. **Doc sweep** — verify every README/PRD/PLAN reference matches the current package name `unwind` and module path `src/unwind/`.
2. **License field** — `LICENSE` is in place; ensure the PyPI metadata picks it up correctly on first build.
3. **PyPI name reservation** — claim `unwind` on PyPI and configure GitHub trusted publishing for `pypi.org/p/unwind`.
4. **Smoke test in CI** — `dev/smoke.sh` currently runs locally; consider lifting it into the publish workflow as a pre-publish gate.
5. **Pagination for very large JSONLs** — sessions reaching tens of MB load the full thread today (Phase 7 in the original plan, not yet landed).
6. **Per-session live PID mapping** — Claude Code doesn't expose a session id on its process; status is project-scoped. Watch upstream for this; if exposed, narrow the live signal.

## Out of scope for v1

- Compaction assist hooks (moves to v2 — see top-level [`RELEASE_PLAN.md`](../../RELEASE_PLAN.md)).
- Cross-project rollup / analytics.
- Windows support.
- Authoring or input-forwarding to Claude Code.

## Known unknowns that resolved in flight

1. **callstack `report.yaml` schema** — verified directly against `examples/parallel_calls`; `callstack.py` consumes it.
2. **`assistant-ui` runtime fit** — did not adopt; built a custom renderer (`CompactCard.tsx` + `TracePane.tsx`) on shadcn primitives instead. This was the biggest pre-build risk.
3. **tool_use ↔ tool_result correlation** — `tool_result` arrives as a subsequent `user`-role message with `tool_use_id`; handled in `messages.py`.
4. **Native `/fork` detection** — added `subagents.py` + `fork_detect.py` after the original plan, to handle Claude Code's experimental `CLAUDE_CODE_FORK_SUBAGENT` flow that the callstack plugin doesn't log.
