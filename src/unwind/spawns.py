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

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .callstack import CallstackIndex, TaskNode
from .fork_detect import ForkDetector
from .projects import project_jsonl_listing
from .jsonl import (
    RETURN_RE as _RETURN_RE,
    YIELD_RE as _YIELD_RE,
    extract_assistant_text as _assistant_text,
    iter_lines,
    read_records,
    parse_ts as _parse_ts,
    stringify_tool_result as _stringify_result,
)
from .subagents import SUBAGENT_PREFIX, SubagentIndex


# Tool names whose tool_use spawns child sessions/agents we drill into.
CALLSTACK_TOOL_NAMES = frozenset(
    {
        # Legacy (kept so historical sessions still resolve).
        "mcp__plugin_callstack_call__invoke",
        "mcp__plugin_callstack_call__invoke_parallel",
        "mcp__plugin_callstack_call__invoke_resume",
        "mcp__plugin_callstack_call__call",
        "mcp__plugin_callstack_call__resume",
    }
)
SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task"})

# These regexes used to live in messages.py; centralised here so the
# anchor pass owns all tool_result parsing in one place.
_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f]{8,})")
_INVOKE_ID_RE = re.compile(
    r'\\?"invoke_id\\?"\s*:\s*\\?"([0-9A-Za-z._-]+)\\?"'
)
def compute_invoke_index_for_project(project_dir: Path) -> dict[str, str]:
    """Scan every JSONL in ``project_dir`` for callstack tool_use/tool_result
    envelopes and return an ``invoke_id → parent_session_id`` map.

    First-wins on collision (invoke_ids are timestamp+random; collision is
    essentially impossible). Safe to call repeatedly — the registry caches
    the result by directory state.
    """
    out: dict[str, str] = {}
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
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") in CALLSTACK_TOOL_NAMES
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
                        if invoke_id not in out:
                            out[invoke_id] = parent_sid
    return out


@dataclass
class Spawn:
    """One ``parent_session → child_session`` invocation, from any source.

    A Spawn is always known once the child's session id is known. The
    ``parent_tool_use_id`` is filled in lazily when anchoring against a
    parsed message stream — it stays ``None`` for spawns that have no
    tool_use anchor in the parent (e.g. callstack-Skill JSON envelopes).
    """

    parent_session_id: str
    child_session_id: str            # ``agent-<id>`` for subagents
    kind: str                        # ``"call"`` | ``"subagent"``
    label: str
    status: str                      # ``running`` | ``yielded`` | ``complete`` | ``failed``
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    invoke_id: Optional[str] = None
    parent_tool_use_id: Optional[str] = None
    # Source tag for debugging / precedence — not surfaced to consumers.
    source: str = "callstack"        # ``callstack`` | ``fork`` | ``subagent``
    # How the child session was launched, when ``kind == "call"``. Drives
    # the icon Unwind renders for each spawn row:
    #   "fork"                — git-fork icon (inherits parent context)
    #   "fresh"               — leaf icon (isolated session, same project)
    #   "fresh_cross_project" — different leaf (isolated, different project)
    # Always ``"fork"`` for ``kind == "subagent"`` (subagents use the
    # Sparkles icon; ``call_type`` is ignored).
    call_type: str = "fork"


class SpawnResolver:
    """Per-project unified spawn view. Cheap to instantiate per request."""

    def __init__(
        self,
        callstack: CallstackIndex,
        forks: ForkDetector,
        subagents: SubagentIndex,
        *,
        project_dir: Path,
        invoke_index: Optional[dict[str, str]] = None,
    ) -> None:
        self._cs = callstack
        self._fd = forks
        self._sa = subagents
        self._project_dir = project_dir
        self._cached: Optional[dict[str, list[Spawn]]] = None
        # Pre-computed invoke_id → parent_session_id. When provided (by
        # registry.spawn_resolver_for_slug), we skip the per-request full-
        # project JSONL scan in _invoke_id_to_parent_session.
        if invoke_index is not None:
            self._invoke_index_cache = invoke_index

    # --- enumeration ----------------------------------------------------

    def spawns_by_parent(self) -> dict[str, list[Spawn]]:
        """All known parent → child spawns, indexed by parent_sid.

        Sources merged in precedence order:

        1. Every callstack ``report.yaml`` task tree contributes one Spawn
           per invocation (not deduped — three resumes of the same child
           produce three Spawns).
        2. The fork detector contributes a Spawn for each callstack-marked
           child whose ``(family_root, child)`` pair isn't already covered
           by step 1. These represent in-flight forks before
           ``report.yaml`` lands.
        3. The subagent index contributes one Spawn per
           ``<session>/subagents/agent-<id>`` invocation.

        Result is cached for the resolver's lifetime; instantiate a fresh
        resolver for each request to pick up filesystem changes.
        """
        if self._cached is not None:
            return self._cached

        out: dict[str, list[Spawn]] = {}

        # 1. Callstack reports. Walk every report's task tree once.
        #
        # A ``report.yaml`` records ``parent_session`` at the time the
        # callstack runtime wrote it; we've seen runtimes record stale
        # session ids from unrelated projects when callstack state from a
        # prior run leaked across cwd boundaries. The tool_use → invoke_id
        # binding in the actual parent JSONL is authoritative: if a
        # session has a callstack tool_use whose tool_result carries
        # invoke_id X, THAT session invoked X — regardless of what
        # ``report.parent_session`` claims. Only override when the
        # recorded parent has no JSONL in this project (the common
        # symptom of a stale/wrong parent_session); leave correctly
        # recorded reports alone.
        invoke_to_real_parent = self._invoke_id_to_parent_session()
        callstack_pairs: set[tuple[str, str]] = set()
        for rep in self._cs.all_reports():
            parent_sid = rep.parent_session
            if (
                not (self._project_dir / f"{parent_sid}.jsonl").is_file()
                and rep.invoke_id in invoke_to_real_parent
            ):
                parent_sid = invoke_to_real_parent[rep.invoke_id]
            for task in rep.tasks:
                self._absorb_callstack(
                    out, callstack_pairs, parent_sid, task, rep
                )

        # 2. Fork detector — only for sessions not already covered above.
        # A fork is "covered" if it appears as a child in ANY callstack
        # report, under any parent — not just the family root. Otherwise
        # the detector adds phantom root→grandchild spawns for every
        # nested descendant (they all share the same ``family_root``),
        # which double-counts them on the root and creates spurious
        # resume windows on the canvas.
        callstack_children: set[str] = {child for _, child in callstack_pairs}
        fork_sids = self._fd.fork_session_ids()
        for fork_sid in fork_sids:
            root = self._fd.family_root(fork_sid)
            if root is None:
                continue
            if fork_sid in callstack_children:
                continue
            # Trigger divergence-text computation lazily for every fork
            # under this root (cheap if cached).
            self._fd._enrich_divergence_for_root(root)  # type: ignore[attr-defined]
            label = self._fd.divergence_text_for(fork_sid) or fork_sid[:8]
            started_at = self._fork_birth(fork_sid)
            status, ended_at = self._infer_fork_status(fork_sid)
            out.setdefault(root, []).append(
                Spawn(
                    parent_session_id=root,
                    child_session_id=fork_sid,
                    kind="call",
                    label=label,
                    status=status,
                    started_at=started_at,
                    ended_at=ended_at,
                    invoke_id=None,
                    source="fork",
                )
            )

        # 3. Subagents. We only care about session ids whose
        # ``<sid>/subagents/`` directory exists on disk — those are the
        # only sessions that *can* have a subagent trace. One os.scandir
        # walk of project_dir is far cheaper than globbing every ``*.jsonl``
        # just to harvest session ids; most sessions don't have subagents.
        candidate_parents: set[str] = set(out.keys())
        if self._project_dir.is_dir():
            try:
                with os.scandir(self._project_dir) as it:
                    for entry in it:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        sub_dir = os.path.join(entry.path, "subagents")
                        try:
                            if os.path.isdir(sub_dir):
                                candidate_parents.add(entry.name)
                        except OSError:
                            pass
            except OSError:
                pass
        for parent_sid in candidate_parents:
            for sa in self._sa.list_for_session(parent_sid):
                out.setdefault(parent_sid, []).append(
                    Spawn(
                        parent_session_id=parent_sid,
                        child_session_id=sa.synthetic_session_id,
                        kind="subagent",
                        label=sa.description or sa.agent_type or sa.agent_id[:8],
                        status="complete",
                        started_at=sa.created_at,
                        ended_at=None,
                        invoke_id=None,
                        source="subagent",
                    )
                )

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

    def for_parent(self, parent_sid: str) -> list[Spawn]:
        return list(self.spawns_by_parent().get(parent_sid, []))

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
            if s.kind == "call":
                if s.invoke_id:
                    callstack_by_invoke.setdefault(s.invoke_id, []).append(s)
                callstack_by_label.setdefault(s.label or "", []).append(s)
                unbound_callstack.append(s)
            elif s.kind == "subagent":
                aid = s.child_session_id.removeprefix(SUBAGENT_PREFIX)
                subagent_by_agent[aid] = s
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

            if name in CALLSTACK_TOOL_NAMES:
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

    def _absorb_callstack(
        self,
        out: dict[str, list[Spawn]],
        seen_pairs: set[tuple[str, str]],
        parent_sid: str,
        task: TaskNode,
        rep: Any,
    ) -> None:
        if task.session_id and parent_sid:
            spawn = Spawn(
                parent_session_id=parent_sid,
                child_session_id=task.session_id,
                kind="call",
                label=task.task or task.session_id[:8],
                status=(task.status or "complete").lower(),
                started_at=rep.started_at,
                ended_at=rep.ended_at,
                invoke_id=rep.invoke_id,
                source="callstack",
                call_type=task.call_type,
            )
            out.setdefault(parent_sid, []).append(spawn)
            seen_pairs.add((parent_sid, task.session_id))
        next_parent = task.session_id or parent_sid
        for child in task.children:
            self._absorb_callstack(out, seen_pairs, next_parent, child, rep)

    def _invoke_id_to_parent_session(self) -> dict[str, str]:
        """Map ``invoke_id`` → the session id whose tool_use produced it.

        Heals reports whose ``parent_session`` doesn't match any real
        JSONL — the tool_use's containing JSONL is the ground truth.
        Result is cached on the resolver instance, or (preferred)
        injected by the registry so all requests share one scan.
        """
        cached = getattr(self, "_invoke_index_cache", None)
        if cached is not None:
            return cached
        out = compute_invoke_index_for_project(self._project_dir)
        self._invoke_index_cache = out
        return out

    def _infer_fork_status(
        self, fork_sid: str
    ) -> tuple[str, Optional[datetime]]:
        """Status + ended_at for a fork-detected spawn with no callstack report.

        Scans the child JSONL for the callstack return/yield envelope in an
        assistant message. The envelope is the runtime's only persistent
        signal that the fork finished — without it (no envelope, no report)
        the spawn is genuinely in-flight, so we keep ``running``.

        Yield wins over return only if the yield is the LATER envelope;
        otherwise a yield-then-resumed-then-returned child would show as
        yielded. We can't tell the two apart from the JSONL alone, so we
        take the LAST envelope seen as the terminal state.
        """
        path = self._project_dir / f"{fork_sid}.jsonl"
        if not path.is_file():
            return "running", None
        last_kind: Optional[str] = None
        last_ts: Optional[datetime] = None
        for rec in iter_lines(path):
            if rec.get("type") != "assistant":
                continue
            text = _assistant_text(rec)
            if not text:
                continue
            is_return = bool(_RETURN_RE.search(text))
            is_yield = bool(_YIELD_RE.search(text))
            if not (is_return or is_yield):
                continue
            last_kind = "complete" if is_return else "yielded"
            last_ts = _parse_ts(rec.get("timestamp"))
        if last_kind is None:
            return "running", None
        return last_kind, last_ts

    def _fork_birth(self, fork_sid: str) -> Optional[datetime]:
        """Birth timestamp of the fork's JSONL."""
        probe = self._fd._probes.get(fork_sid)  # type: ignore[attr-defined]
        if probe is None:
            return None
        try:
            return datetime.fromtimestamp(probe.birth_ts, tz=timezone.utc)
        except (OSError, ValueError):
            return None


# --- helpers (lifted from messages.py) -----------------------------------


def _copy_spawn(s: Spawn) -> Spawn:
    return Spawn(
        parent_session_id=s.parent_session_id,
        child_session_id=s.child_session_id,
        kind=s.kind,
        label=s.label,
        status=s.status,
        started_at=s.started_at,
        ended_at=s.ended_at,
        invoke_id=s.invoke_id,
        parent_tool_use_id=s.parent_tool_use_id,
        source=s.source,
        call_type=s.call_type,
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
    "Spawn",
    "SpawnResolver",
]
