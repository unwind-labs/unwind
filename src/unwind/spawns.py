"""Unified spawn resolution.

Three sources tell us "session X spawned session Y":

* :class:`~unwind.callstack.CallstackIndex` reads ``report.yaml`` files
  (authoritative once written, but only at invocation completion).
* :class:`~unwind.fork_detect.ForkDetector` reads child JSONL prologue
  markers (available the instant a callstack-spawned child writes its
  first record — well before any report exists).
* :class:`~unwind.subagents.SubagentIndex` reads ``<session>/subagents/
  agent-<id>.{jsonl,meta.json}`` (available the instant Claude Code
  spawns the subagent).

Each source has a different time-when-available. We merge them once
into a canonical ``list[Spawn]`` so the two consumers — the parent's
compact-card spawn rows (`messages.annotate_spawns`) AND the canvas
window tree (`canvas_tree.build_canvas_tree`) — don't each re-implement
the fallback ladder.

This is the single source of truth for "what session spawned what".
Adding a new source means adding one branch here; both consumers
pick it up automatically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional, TypeAlias, Union

from .callstack import CallstackIndex, TaskNode
from .fork_detect import ForkDetector
from .projects import project_jsonl_listing
from .jsonl import (
    collect_uuids,
    read_records,
    stringify_tool_result as _stringify_result,
)
from .session_scan import SessionScan
from .status import from_raw as _from_raw_status
from .subagents import SubagentIndex


# Signature for the optional per-session scan accessor. Production wires
# ``CanvasTreeBuilder.get_scan`` (mtime-cached); tests can pass ``None``
# to short-circuit the fork-status read.
SessionScanner: TypeAlias = "Callable[[str], Optional[SessionScan]]"


# Tool names whose tool_use refers to a callstack invocation. The list
# is split because the two groups bind differently:
#
#   * ``CALLSTACK_SPAWNING_TOOL_NAMES`` — tool_uses that *start* a call
#     (``call``, ``resume``, legacy ``invoke*``). Each tool_use here is
#     a fresh anchor: its tool_input lists requested tasks and its
#     result envelope carries the freshly-minted ``invoke_id``.
#
#   * ``CALLSTACK_AWAITING_TOOL_NAMES`` — tool_uses that *refer back to*
#     an already-running call without spawning anything new (``await_call``
#     for ``run_in_background=True`` reconciliation). The tool_input
#     carries an existing ``invoke_id``; binding is purely by that id
#     onto the spawn the original spawning tool_use already claimed.
#
# ``CALLSTACK_TOOL_NAMES`` is the union — used everywhere the question
# is "is this a callstack tool_use at all" (e.g. message classification,
# invoke-index scanning). Sites that need to distinguish the two
# behaviors check the narrower sets directly.
CALLSTACK_SPAWNING_TOOL_NAMES = frozenset(
    {
        # Legacy (kept so historical sessions still resolve).
        "mcp__plugin_callstack_call__invoke",
        "mcp__plugin_callstack_call__invoke_parallel",
        "mcp__plugin_callstack_call__invoke_resume",
        "mcp__plugin_callstack_call__call",
        "mcp__plugin_callstack_call__resume",
    }
)
CALLSTACK_AWAITING_TOOL_NAMES = frozenset(
    {
        "mcp__plugin_callstack_call__await_call",
    }
)
CALLSTACK_TOOL_NAMES = CALLSTACK_SPAWNING_TOOL_NAMES | CALLSTACK_AWAITING_TOOL_NAMES
SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task"})

# These regexes used to live in messages.py; centralised here so the
# anchor pass owns all tool_result parsing in one place.
_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f]{8,})")
_INVOKE_ID_RE = re.compile(
    r'\\?"invoke_id\\?"\s*:\s*\\?"([0-9A-Za-z._-]+)\\?"'
)
# The ``call``/``resume`` result envelope echoes the absolute on-disk
# ``report_path`` (see callstack/mcp_server.py). The callstack runtime anchors
# its log dir to the ROOT invocation's cwd, so when a session's own cwd differs
# from the root's (a forked worker in another project, or a harness driving
# calls from a different folder) the report.yaml lands OUTSIDE this project's
# ``<cwd>/.claude/callstack/log``. Recovering the path from the transcript lets
# us read those out-of-tree reports instead of guessing directories.
_REPORT_PATH_RE = re.compile(
    r'\\?"report_path\\?"\s*:\s*\\?"([^"\\]+\.yaml)\\?"'
)


def compute_report_paths_for_project(project_dir: Path) -> list[Path]:
    """Absolute ``report.yaml`` paths referenced by callstack tool_results in
    this project's JSONLs, deduped and existence-checked, in discovery order.

    These are the reports the project's own sessions actually produced, even
    when the runtime wrote them under a different project's log dir (see
    ``_REPORT_PATH_RE``). The caller feeds them to :class:`CallstackIndex` as
    extra report sources so cross-project / harness-driven runs still resolve
    their call trees. Safe to call repeatedly; the registry caches by directory
    signature.
    """
    out: list[Path] = []
    seen: set[str] = set()
    if not project_dir.is_dir():
        return out
    for entry in project_jsonl_listing(project_dir):
        for rec in read_records(entry.path):
            if rec.get("type") != "user":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                result_text = _stringify_result(block.get("content"))
                for m in _REPORT_PATH_RE.finditer(result_text):
                    raw = m.group(1)
                    if raw in seen:
                        continue
                    seen.add(raw)
                    p = Path(raw)
                    if p.is_file():
                        out.append(p)
    return out
def compute_invoke_index_for_project(
    project_dir: Path,
) -> dict[str, list[str]]:
    """Scan every JSONL in ``project_dir`` for callstack tool_use/tool_result
    envelopes and return an ``invoke_id → [candidate_session_id, ...]`` map.

    Multiple sessions can surface the same invoke_id in their JSONLs: the
    callstack plugin has been observed echoing the OUTER invoke_id in
    tool_results for inner ``/call``s made by a forked child. So a single
    invoke_id can appear with valid tool_use+tool_result pairs in both the
    real parent's JSONL and one or more child JSONLs. Returning every
    candidate (deduped, in discovery order) lets the consumer pick the
    right one with extra context — typically by filtering out sessions
    that themselves appear as a task in the matching report. Safe to call
    repeatedly — the registry caches the result by directory state.
    """
    out: dict[str, list[str]] = {}
    if not project_dir.is_dir():
        return out
    for entry in project_jsonl_listing(project_dir):
        parent_sid = entry.sid
        tool_use_names: dict[str, str] = {}
        for rec in read_records(entry.path):
            rtype = rec.get("type")
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            if rtype == "assistant":
                for block in content:
                    # Spawning names only: this index answers "which
                    # session originated the call with this invoke_id".
                    # ``await_call`` echoes the invoke_id in its result
                    # too, but it doesn't mint anything — including it
                    # would let a session that merely polls a call get
                    # mis-identified as the real parent.
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") in CALLSTACK_SPAWNING_TOOL_NAMES
                    ):
                        tu_id = block.get("id")
                        if isinstance(tu_id, str):
                            tool_use_names[tu_id] = block["name"]
            elif rtype == "user":
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tu_id = block.get("tool_use_id")
                    if not isinstance(tu_id, str) or tu_id not in tool_use_names:
                        continue
                    result_text = _stringify_result(block.get("content"))
                    m = _INVOKE_ID_RE.search(result_text)
                    if m:
                        invoke_id = m.group(1)
                        candidates = out.setdefault(invoke_id, [])
                        if parent_sid not in candidates:
                            candidates.append(parent_sid)
    return out


@dataclass(kw_only=True)
class _SpawnBase:
    """Fields shared by every spawn variant.

    A Spawn is always known once the child's session id is known. The
    ``parent_tool_use_id`` is filled in lazily when anchoring against a
    parsed message stream — it stays ``None`` for spawns that have no
    tool_use anchor in the parent (e.g. callstack-Skill JSON envelopes).
    """

    parent_session_id: str
    child_session_id: str
    label: str
    status: str                      # ``running`` | ``yielded`` | ``complete`` | ``failed``
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    parent_tool_use_id: Optional[str] = None
    # Source tag for debugging / precedence — not surfaced to consumers.
    source: str = "callstack"        # ``callstack`` | ``fork`` | ``subagent``


@dataclass(kw_only=True)
class CallSpawn(_SpawnBase):
    """A ``/call`` invocation: parent invoked a child claude session via
    the callstack runtime (or the fork detector caught one in-flight).

    ``call_type`` drives the icon Unwind renders for each spawn row:
      "fork"                — git-fork icon (inherits parent context)
      "fresh"               — leaf icon (isolated session, same project)
      "fresh_cross_project" — different leaf (isolated, different project)
    """

    kind: Literal["call"] = "call"
    invoke_id: Optional[str] = None
    call_type: str = "fork"


@dataclass(kw_only=True)
class SubagentSpawn(_SpawnBase):
    """An Agent/Task subagent invocation. ``child_session_id`` is the
    ``agent-<hex>`` synthetic id used by the messages endpoint.
    """

    kind: Literal["subagent"] = "subagent"
    agent_id: str = ""  # bare hex id (without ``agent-`` prefix)


# Discriminated union — narrow with ``isinstance`` OR ``s.kind``.
Spawn: TypeAlias = Union[CallSpawn, SubagentSpawn]


class SpawnResolver:
    """Per-project unified spawn view. Cheap to instantiate per request."""

    def __init__(
        self,
        callstack: CallstackIndex,
        forks: ForkDetector,
        subagents: SubagentIndex,
        *,
        project_dir: Path,
        invoke_index: Optional[dict[str, list[str]]] = None,
        session_scanner: Optional[SessionScanner] = None,
    ) -> None:
        self._cs = callstack
        self._fd = forks
        self._sa = subagents
        self._project_dir = project_dir
        self._cached: Optional[dict[str, list[Spawn]]] = None
        # ``session_scanner(sid) -> SessionScan`` from canvas_tree. When
        # wired (by registry.spawn_resolver_for_slug), the fork-status
        # inference reads from the mtime-cached scan instead of walking
        # the child JSONL a second time. ``None`` is a valid value —
        # tests and ad-hoc construction skip the read.
        self._session_scanner: Optional[SessionScanner] = session_scanner
        # Pre-computed invoke_id → [candidate_session_id, ...]. ``None``
        # means "not provided — compute lazily on first need". Either
        # way the field is always present; readers don't getattr() it.
        self._invoke_index_cache: Optional[dict[str, list[str]]] = invoke_index
        # Memoized ``session_id -> union of every ancestor JSONL's uuid
        # set``. ``claude --fork-session`` copies the parent transcript
        # verbatim into the child's JSONL, so any record whose uuid is
        # in the parent's file represents work the parent already paid
        # for. Consumers that aggregate per-session usage skip events
        # whose uuid lands in this set; without that, a parent with N
        # forks gets its prefix tokens counted N+1 times.
        self._inherited_uuids_cache: dict[str, set[str]] = {}

    # --- enumeration ----------------------------------------------------

    def spawns_by_parent(self) -> dict[str, list[Spawn]]:
        """All known parent → child spawns, indexed by parent_sid.

        Three iterator sources merged in precedence order:

        1. ``_iter_callstack_spawns`` — one Spawn per task in every
           ``report.yaml``; uses the invoke-index to heal stale
           ``parent_session`` recordings (callstack runtime sometimes
           echoes outer invoke_ids in inner forks).
        2. ``_iter_fork_spawns`` — one Spawn per callstack-marked fork
           whose ``(parent, child)`` pair the callstack reports didn't
           already cover (in-flight forks before ``report.yaml`` lands).
        3. ``_iter_subagent_spawns`` — one Spawn per
           ``<session>/subagents/agent-<id>`` invocation; the parent
           list comes from ``SubagentIndex.parent_sids``.

        Result is cached for the resolver's lifetime; instantiate a fresh
        resolver for each request to pick up filesystem changes.
        """
        if self._cached is not None:
            return self._cached

        out: dict[str, list[Spawn]] = {}
        callstack_pairs: set[tuple[str, str]] = set()

        for spawn in self._iter_callstack_spawns(callstack_pairs):
            out.setdefault(spawn.parent_session_id, []).append(spawn)

        callstack_children: set[str] = {child for _, child in callstack_pairs}
        for spawn in self._iter_fork_spawns(skip=callstack_children):
            out.setdefault(spawn.parent_session_id, []).append(spawn)

        for spawn in self._iter_subagent_spawns():
            out.setdefault(spawn.parent_session_id, []).append(spawn)

        # Sort each parent's list chronologically. Stable for deterministic
        # canvas window assignment (K-th invocation → K-th window).
        for spawns in out.values():
            spawns.sort(
                key=lambda s: (
                    s.started_at.timestamp() if s.started_at else 0.0,
                    s.child_session_id,
                )
            )

        self._cached = out
        return out

    # --- per-source iterators -------------------------------------------

    def _iter_callstack_spawns(
        self, callstack_pairs: set[tuple[str, str]]
    ) -> Iterator[CallSpawn]:
        """Yield one CallSpawn per task across every callstack ``report.yaml``.

        ``report.yaml`` records ``parent_session`` at write time; the
        callstack runtime has been observed recording stale ids (state
        leak across cwd boundaries; unrelated sibling sessions in the
        same project). The tool_use → invoke_id binding in JSONLs is
        the corrective signal — the JSONL whose tool_use produced
        invoke X is the real emitter of X.

        The invoke index returns MULTIPLE candidates per invoke_id
        because ``--fork-session`` copies the parent's transcript
        (including its callstack tool_uses) into the child's JSONL.
        We trust the recorded ``parent_session`` whenever it's a
        corroborated candidate; otherwise we delegate to
        ``_pick_parent_candidate`` to heal.

        Side-effect: populates ``callstack_pairs`` with every emitted
        ``(parent_sid, child_sid)`` pair so the fork iterator can skip
        children that already have callstack coverage.
        """
        invoke_to_real_parent = self._invoke_id_to_parent_session()
        for rep in self._cs.all_reports():
            parent_sid = rep.parent_session
            candidates = invoke_to_real_parent.get(rep.invoke_id, [])
            if candidates:
                task_sids = _task_session_ids(rep.tasks)
                corroborated = (
                    parent_sid in candidates and parent_sid not in task_sids
                )
                if not corroborated:
                    healed = self._pick_parent_candidate(
                        candidates, task_sids, rep.started_at
                    )
                    if healed is not None:
                        parent_sid = healed
            for task in rep.tasks:
                yield from self._tasks_to_spawns(
                    callstack_pairs, parent_sid, task, rep
                )

    def _iter_fork_spawns(self, *, skip: set[str]) -> Iterator[CallSpawn]:
        """Yield one CallSpawn per callstack-marked fork not already in ``skip``.

        A fork is "covered" if it appears as a child in ANY callstack
        report, under any parent — not just the family root. Otherwise
        the detector would add phantom root→grandchild spawns for every
        nested descendant (they all share the same ``family_root``),
        which double-counts them on the root and creates spurious
        resume windows on the canvas.
        """
        for fork_sid in self._fd.fork_session_ids():
            if fork_sid in skip:
                continue
            root = self._fd.family_root(fork_sid)
            if root is None:
                continue
            label = self._fd.divergence_text_for(fork_sid) or fork_sid[:8]
            started_at = self._fork_birth(fork_sid)
            status, ended_at = self._infer_fork_status(fork_sid)
            yield CallSpawn(
                parent_session_id=root,
                child_session_id=fork_sid,
                label=label,
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                invoke_id=None,
                source="fork",
            )

    def _iter_subagent_spawns(self) -> Iterator[SubagentSpawn]:
        """Yield one SubagentSpawn per Agent/Task invocation in the project.

        Reads the parent list from ``SubagentIndex.parent_sids`` — the
        index owns the os.scandir walk for "which sids have a subagents/
        dir". One mtime-cached pass per project per resolver lifetime.
        """
        for parent_sid in self._sa.parent_sids():
            for sa in self._sa.list_for_session(parent_sid):
                yield SubagentSpawn(
                    parent_session_id=parent_sid,
                    child_session_id=sa.synthetic_session_id,
                    agent_id=sa.agent_id,
                    label=sa.description or sa.agent_type or sa.agent_id[:8],
                    status="complete",
                    started_at=sa.created_at,
                    ended_at=None,
                    source="subagent",
                )

    def for_parent(self, parent_sid: str) -> list[Spawn]:
        return list(self.spawns_by_parent().get(parent_sid, []))

    def inherited_uuids_for(self, session_id: str) -> set[str]:
        """Return uuids this session inherited from its callstack ancestors.

        Walks ``CallstackIndex.parent_chain`` and unions every ancestor
        JSONL's uuid set (``collect_uuids`` is mtime-cached, so repeated
        calls across a request are cheap). Returns an empty set for
        non-fork sessions and for forks whose parent chain isn't
        reflected in ``report.yaml`` yet.

        Memoized per resolver instance; the cache is implicitly bounded
        by the per-request resolver lifetime.

        TODO: non-callstack forks (manual ``claude --fork-session``
        outside the runtime) won't appear in ``parent_chain`` and so
        still over-count. ``ForkDetector.family_root`` could provide a
        one-level parent for those; left for a follow-up because the
        rest of the canvas already treats them as independent roots.
        """
        cached = self._inherited_uuids_cache.get(session_id)
        if cached is not None:
            return cached
        chain = self._cs.parent_chain(session_id) if self._cs.has_logs else []
        out: set[str] = set()
        for ancestor_id in chain:
            anc_path = self._project_dir / f"{ancestor_id}.jsonl"
            if anc_path.is_file():
                out |= collect_uuids(anc_path)
        self._inherited_uuids_cache[session_id] = out
        return out

    def subagent_jsonl_path(self, synthetic_id: str) -> Optional[Path]:
        """Resolve an ``agent-<id>`` synthetic session id to its real
        transcript at ``<parent>/subagents/agent-<id>.jsonl``.

        Subagents (Agent/Task tool spawns) log to their own file under the
        parent session's ``subagents/`` dir, NOT to a top-level
        ``<sid>.jsonl``. The canvas builder needs this real path to scan a
        subagent's ``message.usage`` — without it the synthetic session
        scans an empty file and the subagent's tokens go uncounted.
        Returns ``None`` for non-subagent ids or when no file exists.
        """
        return self._sa.resolve(synthetic_id)

    def child_display_order(
        self, parent_sid: str, messages: list[Any]
    ) -> dict[str, int]:
        """Position of each child_session_id in the parent's CALL row list.

        Mirrors the order ``derive-rows.ts`` produces on the frontend:
        anchored spawns first (in bind order = tool_use chronological ×
        requested-task order), then unanchored extras. Used by the
        canvas builder to sort each parent's child windows so the
        canvas columns match the parent's CALL row order (no crossing
        connectors).
        """
        order: dict[str, int] = {}
        for i, s in enumerate(self.anchor_to_messages(parent_sid, messages)):
            if s.child_session_id and s.child_session_id not in order:
                order[s.child_session_id] = i
        return order

    # --- anchoring ------------------------------------------------------

    def anchor_to_messages(
        self, parent_sid: str, messages: list[Any]
    ) -> list[Spawn]:
        """Return spawns for ``parent_sid`` with ``parent_tool_use_id`` set
        wherever a matching tool_use can be identified in ``messages``.

        ``messages`` is the list of ``messages.Message`` from the parent's
        parsed JSONL. Each returned ``Spawn`` is a fresh copy — mutating
        them does not affect ``spawns_by_parent``.

        Anchoring rules (each tool_use claims spawns once, in order):

        * Callstack tool_use whose tool_result carries ``invoke_id`` →
          bind by exact invoke_id.
        * Callstack tool_use without an invoke_id (in-flight) → bucket
          unanchored callstack spawns by ``label`` and pop one per
          requested task in ``tool_input``.
        * Agent/Task tool_use whose tool_result carries ``agentId`` →
          bind by agent_id.
        * Agent/Task tool_use without one → bind by ``description`` match.
        """
        spawns = [
            _copy_spawn(s) for s in self.spawns_by_parent().get(parent_sid, [])
        ]

        callstack_by_invoke: dict[str, list[Spawn]] = {}
        callstack_by_label: dict[str, list[Spawn]] = {}
        unbound_callstack: list[Spawn] = []
        subagent_by_agent: dict[str, Spawn] = {}
        subagent_by_desc: dict[str, list[Spawn]] = {}
        for s in spawns:
            if isinstance(s, CallSpawn):
                if s.invoke_id:
                    callstack_by_invoke.setdefault(s.invoke_id, []).append(s)
                callstack_by_label.setdefault(s.label or "", []).append(s)
                unbound_callstack.append(s)
            else:
                subagent_by_agent[s.agent_id] = s
                subagent_by_desc.setdefault(s.label or "", []).append(s)

        # Build tool_use → tool_result map and walk tool_uses in order.
        result_for: dict[str, Any] = {}
        for m in messages:
            if getattr(m, "role", None) == "tool_result" and m.tool_result_for:
                result_for[m.tool_result_for] = m

        bound: set[int] = set()  # id() of Spawns already anchored
        # Records the order spawns were bound across the whole anchoring
        # pass — the same order ``derive-rows.ts`` would emit them on
        # the frontend (tool_use chronological × requested-task order
        # within each). Used to re-sort the returned list so consumers
        # (canvas tree's child sort) see bind order naturally.
        bind_sequence: list[Spawn] = []

        def _bind(s: Spawn, tu_id: str) -> None:
            if id(s) in bound:
                return
            s.parent_tool_use_id = tu_id
            bound.add(id(s))
            bind_sequence.append(s)

        for m in messages:
            if getattr(m, "role", None) != "tool_use":
                continue
            tu_id = m.tool_use_id
            if not tu_id:
                continue
            name = m.tool_name or ""
            res = result_for.get(tu_id)

            if name in CALLSTACK_AWAITING_TOOL_NAMES:
                # ``await_call`` does NOT spawn — it polls an already-
                # running invocation. The originating ``call`` tool_use
                # already claimed the spawn (and owns parent_tool_use_id);
                # we leave the spawn alone here. Decoration of the
                # await_call message itself (so its row shows the same
                # child node) happens in ``messages.annotate_spawns``,
                # which doesn't need the spawn's parent_tool_use_id.
                continue

            if name in CALLSTACK_SPAWNING_TOOL_NAMES:
                # Two-step bind:
                #
                #  1. Determine the candidate pool — spawns belonging to
                #     this tool_use's invocation. If the tool_result
                #     carries an ``invoke_id``, the pool is exactly the
                #     report's spawns. Otherwise (in-flight, no result
                #     yet), the pool is *all* unbound callstack spawns
                #     from this parent.
                #  2. From the pool, pop one unbound spawn per requested
                #     task label (matched by name). Falls back to first-
                #     N when the tool_use has no requested-tasks input.
                invoke_id = _extract_invoke_id(res)
                if invoke_id and invoke_id in callstack_by_invoke:
                    pool = callstack_by_invoke[invoke_id]
                else:
                    pool = unbound_callstack
                pool_by_label: dict[str, list[Spawn]] = {}
                for s in pool:
                    if id(s) in bound:
                        continue
                    pool_by_label.setdefault(s.label or "", []).append(s)

                requested = _requested_tasks(m.tool_input)
                if requested:
                    for label in requested:
                        bucket = pool_by_label.get(label, [])
                        if not bucket:
                            continue
                        s = bucket.pop(0)
                        _bind(s, tu_id)
                else:
                    # No declared tasks (e.g. Skill-style callsite) —
                    # claim every unbound spawn in the pool.
                    for s in pool:
                        if id(s) in bound:
                            continue
                        _bind(s, tu_id)

            elif name in SUBAGENT_TOOL_NAMES:
                agent_id = _extract_agent_id(res)
                if agent_id and agent_id in subagent_by_agent:
                    s = subagent_by_agent[agent_id]
                    _bind(s, tu_id)
                else:
                    desc = ""
                    if isinstance(m.tool_input, dict):
                        raw = m.tool_input.get("description")
                        if isinstance(raw, str):
                            desc = raw.strip()
                    if desc:
                        for s in subagent_by_desc.get(desc, []):
                            if id(s) in bound:
                                continue
                            _bind(s, tu_id)
                            break

        # Re-order: bound spawns in the order they were bound (= the
        # order they'll appear as CALL rows in derive-rows.ts), then
        # unbound spawns (extras) in their original spawns_by_parent
        # order. The canvas tree consumes this order for child sorting.
        bound_set = {id(s) for s in bind_sequence}
        rest = [s for s in spawns if id(s) not in bound_set]
        return bind_sequence + rest

    # --- internals ------------------------------------------------------

    def _tasks_to_spawns(
        self,
        seen_pairs: set[tuple[str, str]],
        parent_sid: str,
        task: TaskNode,
        rep: Any,
    ) -> Iterator[CallSpawn]:
        """Yield one CallSpawn per task in the tree, in pre-order.

        ``seen_pairs`` is populated as a side effect so the fork
        iterator can skip pairs already covered here.

        Status override: when ``task.status`` is still non-terminal
        (``running`` / ``pending``) but the child's JSONL has emitted a
        terminal callstack envelope (``op: return`` / ``op: yield``),
        prefer the JSONL signal. ``report.yaml`` is only finalized when
        the parent reconciles via ``await_call`` — for backgrounded
        calls the parent may go long stretches without polling, leaving
        the report frozen at ``running`` while the child is in fact
        done. The child JSONL is the authoritative liveness signal.
        """
        if task.session_id and parent_sid:
            raw_status = (task.status or "complete").lower()
            status, ended_at = self._override_status_from_scan(
                task.session_id, raw_status, rep.ended_at
            )
            yield CallSpawn(
                parent_session_id=parent_sid,
                child_session_id=task.session_id,
                label=task.task or task.session_id[:8],
                status=status,
                started_at=rep.started_at,
                ended_at=ended_at,
                invoke_id=rep.invoke_id,
                source="callstack",
                call_type=task.call_type,
            )
            seen_pairs.add((parent_sid, task.session_id))
        next_parent = task.session_id or parent_sid
        for child in task.children:
            yield from self._tasks_to_spawns(
                seen_pairs, next_parent, child, rep
            )

    def _pick_parent_candidate(
        self,
        candidates: list[str],
        task_sids: set[str],
        rep_started_at: Optional[datetime],
    ) -> Optional[str]:
        """Pick the most plausible emitter of ``rep`` from invoke-index candidates.

        Rejects candidates that appear as a task in the report (self-loop:
        callstack echoes the outer invoke_id in inner /call tool_results
        from forked children) and candidates born after ``rep_started_at``
        (``--fork-session`` descendants that only carry the invoke_id via
        transcript copy). Among survivors, prefer the latest birth ≤
        ``rep_started_at`` (the deepest ancestor alive at invoke time).
        Falls back to discovery order when timestamps are unavailable.
        """
        non_task = [c for c in candidates if c not in task_sids]
        if not non_task:
            return None
        if rep_started_at is None:
            return non_task[0]
        rep_ts = rep_started_at.timestamp()
        before: list[tuple[float, str]] = []
        after: list[str] = []
        unstamped: list[str] = []
        for cand in non_task:
            birth = self._candidate_birth_ts(cand)
            if birth is None:
                unstamped.append(cand)
            elif birth <= rep_ts:
                before.append((birth, cand))
            else:
                after.append(cand)
        if before:
            before.sort(key=lambda kv: kv[0], reverse=True)
            return before[0][1]
        # No candidate predates the invoke (clock skew, or test fixtures
        # using past report timestamps with newly-created JSONLs). Fall
        # back to discovery order — better to return SOMETHING than to
        # leave a known-stale ``rep.parent_session`` in place.
        return unstamped[0] if unstamped else after[0]

    def _candidate_birth_ts(self, sid: str) -> Optional[float]:
        """Return the birth timestamp of ``sid``'s JSONL, or ``None``.

        Reuses the ForkDetector's cached probes when available (one
        ``os.stat`` per session across the project's lifetime); falls
        back to a direct stat when the probe is missing.
        """
        ts = self._fd.birth_ts(sid)
        if ts is not None:
            return ts
        path = self._project_dir / f"{sid}.jsonl"
        try:
            st = path.stat()
        except OSError:
            return None
        try:
            from .jsonl import file_birth_ts
            return file_birth_ts(path, fallback=st.st_mtime)
        except Exception:
            return st.st_mtime

    def _invoke_id_to_parent_session(self) -> dict[str, list[str]]:
        """Map ``invoke_id`` → list of session ids whose tool_use+result
        carries that invoke_id, in discovery order.

        Authoritative source for ``parent_session`` when a tool_use
        anchor exists: the containing JSONL is ground truth, overriding
        any ``parent_session`` value the runtime recorded in
        ``report.yaml``. Multiple candidates are returned because the
        callstack plugin has been observed echoing the OUTER invoke_id
        in inner ``/call`` tool_results made by a forked child; the
        consumer picks one with extra context (e.g. by filtering out
        sessions that appear as tasks in the matching report). Result
        is cached on the resolver instance, or (preferred) injected by
        the registry so all requests share one scan.
        """
        if self._invoke_index_cache is not None:
            return self._invoke_index_cache
        out = compute_invoke_index_for_project(self._project_dir)
        self._invoke_index_cache = out
        return out

    def _infer_fork_status(
        self, fork_sid: str
    ) -> tuple[str, Optional[datetime]]:
        """Status + ended_at for a fork-detected spawn with no callstack report.

        Reads from the canonical :class:`canvas_tree.SessionScan` (mtime/
        size-cached). The scan tracks the LAST callstack envelope seen
        in the child's JSONL via ``last_envelope_kind`` /
        ``last_envelope_ts``; that's the runtime's only persistent
        signal that the fork finished. Yield wins over return only if
        the yield is the LATER envelope — a yield-then-resumed-then-
        returned child surfaces as ``complete`` because that's its
        terminal state.

        Returns ``("running", None)`` when no scanner is wired so test
        fixtures without a CanvasTreeBuilder still get a sensible
        default. Production code always wires a scanner via
        :func:`unwind.registry.spawn_resolver_for_slug`.
        """
        scan = self._scan_for(fork_sid)
        if scan is None:
            return "running", None
        kind = scan.last_envelope_kind
        if kind is None:
            return "running", None
        return ("complete" if kind == "return" else "yielded"), scan.last_envelope_ts

    def _scan_for(self, sid: str) -> Optional[SessionScan]:
        """Return the cached SessionScan for ``sid`` if a scanner is wired."""
        if self._session_scanner is None:
            return None
        try:
            return self._session_scanner(sid)
        except OSError:
            # Filesystem race: the JSONL vanished between probe and read.
            # Higher-priority signals (callstack reports) usually cover
            # for it; return None and let the caller fall back.
            return None

    def _override_status_from_scan(
        self,
        child_sid: str,
        raw_status: str,
        report_ended_at: Optional[datetime],
    ) -> tuple[str, Optional[datetime]]:
        """If the child JSONL has a terminal envelope, prefer it over a
        non-terminal ``report.yaml`` status.

        Returns ``(status, ended_at)``. Falls back to
        ``(raw_status, report_ended_at)`` when:
          * no session scanner is wired (test fixtures, ad-hoc use), or
          * the child JSONL has no terminal envelope yet, or
          * the report already records a terminal status (``complete`` /
            ``failed`` / ``yielded``) — the report is authoritative once
            it lands, since the callstack runtime wrote it after seeing
            the same envelope.

        The ``raw_status`` strings are the lowercased ``task.status``
        values from ``report.yaml``; the canonical mapping in
        :mod:`unwind.status` translates them downstream.
        """
        canonical = _from_raw_status(raw_status)
        if canonical not in ("live", None):
            return raw_status, report_ended_at
        scan = self._scan_for(child_sid)
        if scan is None or scan.last_envelope_kind is None:
            return raw_status, report_ended_at
        new_status = (
            "complete" if scan.last_envelope_kind == "return" else "yielded"
        )
        return new_status, scan.last_envelope_ts or report_ended_at

    def _fork_birth(self, fork_sid: str) -> Optional[datetime]:
        """Birth timestamp of the fork's JSONL."""
        ts = self._fd.birth_ts(fork_sid)
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError):
            return None


# --- helpers (lifted from messages.py) -----------------------------------


def _task_session_ids(tasks: list[TaskNode]) -> set[str]:
    """Every session_id appearing in a report's task tree (recursive)."""
    out: set[str] = set()

    def visit(t: TaskNode) -> None:
        if t.session_id:
            out.add(t.session_id)
        for c in t.children:
            visit(c)

    for t in tasks:
        visit(t)
    return out


def _copy_spawn(s: Spawn) -> Spawn:
    if isinstance(s, CallSpawn):
        return CallSpawn(
            parent_session_id=s.parent_session_id,
            child_session_id=s.child_session_id,
            label=s.label,
            status=s.status,
            started_at=s.started_at,
            ended_at=s.ended_at,
            invoke_id=s.invoke_id,
            parent_tool_use_id=s.parent_tool_use_id,
            source=s.source,
            call_type=s.call_type,
        )
    return SubagentSpawn(
        parent_session_id=s.parent_session_id,
        child_session_id=s.child_session_id,
        agent_id=s.agent_id,
        label=s.label,
        status=s.status,
        started_at=s.started_at,
        ended_at=s.ended_at,
        parent_tool_use_id=s.parent_tool_use_id,
        source=s.source,
    )


def _extract_invoke_id(result: Any) -> Optional[str]:
    if result is None or getattr(result, "tool_result", None) is None:
        return None
    text = _stringify_result(result.tool_result)
    m = _INVOKE_ID_RE.search(text)
    return m.group(1) if m else None


def _extract_agent_id(result: Any) -> Optional[str]:
    if result is None or getattr(result, "tool_result", None) is None:
        return None
    text = _stringify_result(result.tool_result)
    m = _AGENT_ID_RE.search(text)
    return m.group(1) if m else None




def _requested_tasks(tool_input: Any) -> list[str]:
    """Return task labels from a callstack invoke* tool_input.

    ``invoke_parallel(tasks=[...])`` → that list.
    ``invoke(task=...)`` → ``[task]``.
    Otherwise ``[]``.
    """
    if not isinstance(tool_input, dict):
        return []
    tasks = tool_input.get("tasks")
    if isinstance(tasks, list):
        return [str(t) for t in tasks]
    task = tool_input.get("task")
    if isinstance(task, str):
        return [task]
    return []






# Re-export the helpers messages.py still uses for tool_result-status
# extraction so we don't duplicate the regexes.
__all__ = [
    "CALLSTACK_TOOL_NAMES",
    "SUBAGENT_TOOL_NAMES",
    "CallSpawn",
    "Spawn",
    "SpawnResolver",
    "SubagentSpawn",
]
