# TODO — Architectural Review Follow-ups (2026-05-15)

Ordered for incremental, low-risk execution. Each item is one commit.
Status: `[ ]` pending · `[~]` in progress · `[x]` done

## Phase 1 — Pure deletions (zero behavior risk)

- [x] **T1** Delete `_is_at_user_yield` in `sessions_api.py`; route `_compute_session_status` through `canvas_tree_builder_for_slug(slug).get_scan(session_id).at_user_prompt`. (Review: C4)
- [x] **T2** Delete dead `read_messages_with_lineage`, `annotate_origins`, `Message.origin_session_id`, `Message.is_inherited`, `MessagePage.ancestors`, `AncestorRef`. Also `ancestors=[]` returns in `sessions_api.py`. (Review: C5)
- [x] **T3** Delete unused `CallstackIndex` methods: `task_status_for_session`, `reports_with_session_node`, `children_in_report`, `children_for_invoke`, `report_for_invoke`. (Review: D-H1)

## Phase 2 — Internal refactors (DRY win, small surface)

- [x] **T4** Add `_text_blocks(msg, sep)` in `jsonl.py`; collapse `extract_assistant_text`, `_extract_user_text`, and the two inline copies in `sessions_api.py` / `fork_detect.py`. (Review: D-H5)
- [x] **T5** Consolidate registry per-slug lock-dance into one `_per_slug(cache, slug, factory)` helper; drive `forget_slug` / `_upgrade_to_real_path` from one cache list. (Review: D-H2)
- [x] **T6** Replace bespoke mtime caches in `CallstackIndex._cache`, `ForkDetector._probes`, `CanvasTreeBuilder._scans` with `_PathCache`. (Review: D-H3)
- [x] **T7** Bound `PathCache` (LRU `maxsize≈512`) so long-running process doesn't retain every JSONL ever parsed. Add registry-level LRU caps. (Review: C3)
- [x] **T8** Single `EPOCH` constant in `jsonl.py`; remove 5 inline copies. Unify `_file_birth_dt` / `_file_birth_ts`. Move `_YIELD_RE`/`_RETURN_RE` into one module. (Review: M2, M3, L1)

## Phase 3 — Perf hot path

- [x] **T9** Cache `_latest_view` and `reports_by_parent` on `CallstackIndex` keyed by callstack-log signature; hoist precomputation in `list_sessions` so per-row helpers do dict lookups. (Review: C1)
- [x] **T10** Cache subagent `_build_one` per `(path, mtime, size)`; in `SpawnResolver.spawns_by_parent`, probe `<sid>/subagents/` via `stat` before opening JSONLs. (Review: C2)
- [x] **T11** Add TTL (~1 s) or signature-based skip to `ForkDetector._refresh`. (Review: P-H1)
- [x] **T12** `list_projects` should use lightweight `os.scandir` for `last_activity` / `session_count`; defer full indexing to project-open. (Review: P-H2)
- [ ] **T13** Watcher: incremental session-summary update from new records only; full re-parse only on cold start or file shrink. (Review: P-H3)
- [ ] **T14** `compute_invoke_index_for_project` → cached `read_records` instead of uncached `iter_lines`. (Review: P-H4)
- [ ] **T15** Centralize project `*.jsonl` listing on the registry (cached by dir mtime); registry/canvas_tree/spawns/fork_detect/watcher all read from it. (Review: M4)

## Phase 4 — API contract (additive)

- [ ] **T16** `GET /messages` accepts `since_offset`/`since_uuid`; client (`ws/client.ts`) uses delta fetch on reconnect. (Review: P-H5)

## Phase 5 — Security

- [ ] **T17** Refuse non-loopback `--host` unless `UNWIND_AUTH_TOKEN` set; Bearer-token middleware when set. Treat missing `Origin` as untrusted for state-changing endpoints. (Review: S-H1+S-H2)
- [ ] **T18** `pick_folder_endpoint`: single in-flight lock (409 if busy), drop timeout to 120 s, per-process nonce from prior GET. (Review: S-H3)
- [ ] **T19** `_pick_with_tk`: pass `initial` via argv/env, not f-string `{initial!r}`. (Review: S-H4)
- [ ] **T20** Harden `UNWIND_ALLOWED_ORIGINS` parsing: reject `null`, `*`, scheme-less entries; warn. (Review: M)
- [ ] **T21** Cap `iter_lines_from` per-tick read to ~16 MiB. (Review: M)
- [ ] **T22** SPA static fallback: assert `is_relative_to(static_root)` and reject symlinks pre-resolve. (Review: M)

## Phase 6 — Architecture & polish

- [ ] **T23** Coalesce watcher `session_updated` events to ≤1 emit per 1–2 s of activity per session. (Review: M)
- [ ] **T24** Per-request state-pack on `Request.state` (resolved `SpawnResolver`, precomputed `_latest_view`, active session, project JSONL listing). (Review: cross-cutting)
- [ ] **T25** Central `Settings` object loaded once at startup; remove env-on-every-accessor pattern. (Review: A-H3+A-H4)
- [ ] **T26** Drain WS pending tasks on disconnect (`await asyncio.gather(*pending, return_exceptions=True)`). (Review: L)
- [ ] **T27** `Message.to_dict` → `dataclasses.asdict` with datetime post-processing. (Review: M)
