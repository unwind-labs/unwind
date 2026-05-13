# unwind — Prioritized TODO

Derived from the architectural review. Ordered by impact × urgency.

## P0 — Security (ship this week)

- [ ] **Path traversal in SPA fallback** — `src/unwind/server.py:101`
  Resolve `target` and require `STATIC_DIR.resolve()` is an ancestor before `FileResponse`.
- [ ] **WebSocket Origin check** — `src/unwind/api/ws.py:18`
  Add an Origin allow-list before `ws.accept()` (CORS does not apply to WS; DNS-rebinding risk).
- [ ] **Unauthenticated folder-picker endpoint** — `src/unwind/api/projects.py:110`
  Require an opaque per-launch token issued by the CLI to the spawned browser tab.
- [ ] **URL-encode path segments in frontend client** — `web/src/api/client.ts:56,74,91`
  Use `encodeURIComponent` for `slug`/`sessionId` (mirror `ws/client.ts:48`).
- [ ] **Validate `slug` and `session_id` at route layer** — `src/unwind/sessions.py:57`, `src/unwind/registry.py:69`
  Strict regex (UUID for session_id, `^[A-Za-z0-9-]+$` for slug). Reject `..` segments.
- [ ] **Latent AppleScript injection** — `src/unwind/dialog.py:46`
  Switch to `osascript -e SCRIPT -- ARG` or escape `"` / `\` even though `initial` is currently `None`.
- [ ] **Confirm uvicorn bind is `127.0.0.1`-only**; gate `/api/docs` behind a debug flag — `src/unwind/server.py:53`.

## P1 — Performance (request-rate hot paths)

- [ ] **Cache `SpawnResolver._invoke_id_to_parent_session`** — `src/unwind/spawns.py:425`
  Slug-level cache on the registry, mtime-keyed; invalidate from the watcher. Single biggest win.
- [ ] **Tail-read for `_is_at_user_yield`** — `src/unwind/api/sessions_api.py:520`
  Read last ~64 KB via `iter_lines_from(path, max(0, size-65536))` instead of full file.
- [ ] **Memoize `collect_uuids` by (slug, session_id, mtime, size)** — `src/unwind/api/sessions_api.py:326`.
- [ ] **Cache `read_messages_with_lineage`** ancestor parses — `src/unwind/messages.py:330`
  Same mtime+size invalidation pattern.
- [ ] **Canvas ETag from project-state hash** — `src/unwind/api/sessions_api.py:430`
  Derive ETag from `max(mtime)` of project_dir JSONLs + callstack_log_dir mtime; only serialize body on miss.
- [ ] **Drop frontend 3s polling, lean on WS** — `web/src/api/client.ts:58,78,98`
  Keep 30s safety net; ~10× fewer requests.
- [ ] **Lift `useMessages` out of CompactCardNode**; memoize `rows` — `web/src/panes/CompactCard.tsx:125`
  Pass memoized `messagesBySession` map down from `CanvasInner`.
- [ ] **Single flush thread in watcher** instead of `threading.Timer` per burst — `src/unwind/watcher.py:36`.
- [ ] **Drop pydantic for messages payload**; stream via `orjson` — `src/unwind/messages.py` + response layer.

## P2 — DRY / architecture

### Big consolidation: unify the two spawn stacks (~300 LOC out)

- [ ] Make `SpawnResolver` the only composer of `(CallstackIndex, ForkDetector, SubagentIndex)`. Delete `_resolver_from_legacy_args` in `src/unwind/messages.py:179` and the matching block in `src/unwind/canvas_tree.py:248-258`. Update tests to pass real resolvers.
- [ ] Move `Spawn → window` mapping (`_compute_windows` + `collect_invocations`) onto `SpawnResolver`; reduce `canvas_tree.py` (633 LOC → ~400) to pure layout.
- [ ] Move `annotate_spawns` from `src/unwind/messages.py:179` into `SpawnResolver.annotate(messages)` in `src/unwind/spawns.py`.

### Small duplications

- [ ] Collapse `_parse_ts` (5 copies) into one in `src/unwind/jsonl.py`: `jsonl.py:199`, `callstack.py:588`, `canvas_tree.py:210`, `messages.py:524`, `spawns.py:615`.
- [ ] Delete unused `CALLSTACK_TOOL_NAMES` from `src/unwind/canvas_tree.py:39` (kept "for symmetry", never read).
- [ ] Merge `_assistant_text` (`spawns.py:597`) and `_extract_assistant_text` (`canvas_tree.py:192`).
- [ ] Promote `_stringify_result` to `messages.py`; remove copies in `spawns.py:566` and `cli_cmds/_render.py:195`.
- [ ] Extract `MTimeCache` helper used across `fork_detect`, `subagents`, `callstack`, `canvas_tree.CanvasTreeBuilder`, `registry` (~40 LOC saved).
- [ ] Split `fork_detect.py`: move `_enrich_divergence_for_root` + `find_session_by_divergence_text` to `sessions.py`.

### Frontend DRY

- [ ] Extract `isTypingTarget(e)` into `web/src/lib/keyboard.ts`; replace copies in `App.tsx:41`, `CanvasPane.tsx:370`, `TracePane.tsx:60`.
- [ ] Hoist `filterExtrasByWindow` into `web/src/panes/instances.ts`; replace copies in `CompactCard.tsx:113` and `TracePane.tsx:227`.
- [ ] Replace `writeHistory` + `writeHistoryDirect` with one function plus `{ force?: boolean }` arg — `web/src/lib/url-sync.ts:63,163`.
- [ ] Centralize the three independent global `keydown` listeners (`App.tsx:71`, CanvasPane, TracePane) into a single dispatcher keyed off `focusedPane`.

## P3 — Bugs & polish

- [ ] **`applyUrlState` clobbers `threadSessionId`** with `rootSessionId` — `web/src/store/ui.ts:107`. URL-sync the field or drop it.
- [ ] **WS backoff lacks jitter + refocus reconnect** — `web/src/ws/client.ts:79`. Add ±25% jitter and reconnect on `visibilitychange`/`online`. Clear pending `setTimeout` on unmount.
- [ ] **CanvasPane `navStateRef` hack** — `web/src/panes/CanvasPane.tsx:345-366`. Split into `useCanvasKeyboard` + `useCanvasAutoFit` + `useCanvasTree`; file drops 761 → ~400 LOC.
- [ ] **Split TracePane renderers** into `MessageGroup` + `SpawnCard` components — `web/src/panes/TracePane.tsx`.
- [ ] **`ReactMarkdown` `urlTransform`** to strip `javascript:` URIs in link `href`s.

## P4 — Accessibility

- [ ] `PaneFrame` keyboard-focusable + labeled — `web/src/App.tsx:122` (add `tabIndex={0}`, `role="region"`, `aria-label`).
- [ ] Add `aria-keyshortcuts` to card buttons matching the README shortcuts.
- [ ] Broader `aria-*` pass across panes (only ~3 attributes today).
