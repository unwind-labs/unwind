"""Session endpoints: list sessions, get messages."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel

import hashlib
import json as _json

from ..canvas_tree import build_canvas_tree
from ..jsonl import collect_uuids
from ..messages import Message, annotate_spawns, base_uuid, read_messages, status_for_spawn
from .. import status as _status
from ..processes import LIVE_MTIME_WINDOW_SEC, project_activity
from ..registry import (
    callstack_for_slug,
    canvas_tree_builder_for_slug,
    fork_detector_for_slug,
    index_for_slug,
    project_state_signature,
    spawn_resolver_for_slug,
    subagent_index_for_slug,
    workflow_index_for_slug,
)
from ..security import SessionIdPath, SlugPath
from ..subagents import SUBAGENT_PREFIX
from ..workflows import WorkflowRun
from .request_state import RequestState, get_request_state

# Workflow run / phase nodes are synthetic — their synthetic session id is
# the run id (``wf_<hex>``) or ``wf_<hex>::p<n>``. Both start with this.
WORKFLOW_PREFIX = "wf_"

router = APIRouter(tags=["sessions"])


def _rs_index(rs: RequestState, slug: str):
    return rs.memoize(("index", slug), lambda: index_for_slug(slug))


def _rs_callstack(rs: RequestState, slug: str):
    return rs.memoize(("callstack", slug), lambda: callstack_for_slug(slug))


def _rs_resolver(rs: RequestState, slug: str):
    return rs.memoize(("resolver", slug), lambda: spawn_resolver_for_slug(slug))


def _rs_builder(rs: Optional[RequestState], slug: str):
    """Request-scoped CanvasTreeBuilder. ``rs is None`` falls through to
    an uncached lookup so non-request callers (tests, scripts) still work."""
    if rs is None:
        return canvas_tree_builder_for_slug(slug)
    return rs.memoize(
        ("canvas_builder", slug), lambda: canvas_tree_builder_for_slug(slug)
    )


def _rs_active_session(rs: RequestState, slug: str, index, project_path):
    return rs.memoize(
        ("active_session", slug),
        lambda: _active_session_for_project(index, project_path),
    )


class SessionRow(BaseModel):
    session_id: str
    title: str
    custom_title: Optional[str] = None
    first_timestamp: Optional[datetime]
    last_timestamp: Optional[datetime]
    message_count: int
    top_level_call_count: int = 0
    cwd: Optional[str]
    git_branch: Optional[str]
    status: str = "done"


@router.get(
    "/projects/{slug}/sessions",
    response_model=list[SessionRow],
)
def list_sessions(
    slug: SlugPath,
    include_forks: bool = Query(False),
    rs: RequestState = Depends(get_request_state),
) -> list[SessionRow]:
    """List Claude sessions in the project.

    By default, sessions that are callstack forks (i.e. appear as a child task
    in some ``report.yaml``) are hidden — they show up nested under their
    parent in the call-tree pane instead. Pass ``include_forks=true`` to see
    everything.
    """
    index = _rs_index(rs, slug)
    project_path = str(index.paths.source_path) if index.paths.has_project_dir else None

    ci = _rs_callstack(rs, slug)
    fork_ids: set[str] = set()
    if not include_forks:
        if ci.has_logs:
            fork_ids |= ci.all_child_session_ids()
        # Heuristic detector: any session sharing its head uuid with another
        # older session is a fork. Catches in-flight forks before report.yaml
        # is written.
        fork_ids |= fork_detector_for_slug(slug).fork_session_ids()

    # Compute the project's active session ONCE per request so each
    # row's status check doesn't re-walk the session list.
    active_session_id = _rs_active_session(rs, slug, index, project_path)

    rows: list[SessionRow] = []
    for s in index.list_sessions():
        if s.session_id in fork_ids:
            continue
        last_epoch = s.last_timestamp.timestamp() if s.last_timestamp else None
        # Top-level call count = direct children in the callstack tree rooted
        # at this session (cheap because reports_by_parent is cached).
        if ci.has_logs:
            tlc = sum(
                len(rep.tasks) for rep in ci.reports_by_parent().get(s.session_id, [])
            )
        else:
            tlc = 0

        # Status priority — designed so the user's *main* session (the one
        # they're driving in the terminal) shows live, while completed forks
        # still show done even if a claude process is alive for the project.
        #
        # 1. Forks (sessions that ARE a callstack task) trust the
        #    callstack verdict directly — yielded → yield, running →
        #    live, terminal → done — even when no claude process is
        #    alive for the project (the fork ran and finished; its
        #    lifecycle is fully captured).
        # 2. Main sessions (only ever a ``parent_session``) gate the
        #    ``yield`` upgrade on the session being live (process up +
        #    recent activity). A historical main session whose
        #    descendants happened to yield somewhere in the chain
        #    isn't "currently waiting for the user" — it's finished
        #    and resumable. Without this gate the entire backlog of
        #    historical sessions lights up amber.
        status = _compute_session_status(
            index,
            ci,
            s.session_id,
            s.cwd,
            last_epoch,
            project_path,
            slug=slug,
            active_session_id=active_session_id,
            rs=rs,
        )

        rows.append(
            SessionRow(
                session_id=s.session_id,
                title=s.title,
                custom_title=s.custom_title,
                first_timestamp=s.first_timestamp,
                last_timestamp=s.last_timestamp,
                message_count=s.message_count,
                top_level_call_count=tlc,
                cwd=s.cwd,
                git_branch=s.git_branch,
                status=status,
            )
        )
    return rows


@router.get(
    "/projects/{slug}/sessions/{session_id}",
    response_model=SessionRow,
)
def get_session(
    slug: SlugPath,
    session_id: SessionIdPath,
    rs: RequestState = Depends(get_request_state),
) -> SessionRow:
    index = _rs_index(rs, slug)
    summary = index.get_session(session_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="session not found")
    project_path = (
        str(index.paths.source_path) if index.paths.has_project_dir else None
    )
    ci = _rs_callstack(rs, slug)
    last_epoch = (
        summary.last_timestamp.timestamp() if summary.last_timestamp else None
    )
    status = _compute_session_status(
        index,
        ci,
        summary.session_id,
        summary.cwd,
        last_epoch,
        project_path,
        slug=slug,
        rs=rs,
    )
    return SessionRow(
        session_id=summary.session_id,
        title=summary.title,
        first_timestamp=summary.first_timestamp,
        last_timestamp=summary.last_timestamp,
        message_count=summary.message_count,
        cwd=summary.cwd,
        git_branch=summary.git_branch,
        status=status,
    )


def _active_session_for_project(
    index, project_path: Optional[str]
) -> Optional[str]:
    """The most-recently-touched session in the project, OR ``None``
    when no claude process is running OR no session has fresh activity.

    Sessions in the same project all share one claude process; we
    can't tell from process state alone WHICH session is being driven.
    The most-recent activity heuristic picks the right one in
    practice — claude writes to the active session continuously, so
    any other session's last_timestamp will lag.
    """
    import time as _time

    if project_path is None:
        return None
    if not project_activity(project_path).claude_running:
        return None
    sessions = index.list_sessions()
    candidates = [s for s in sessions if s.last_timestamp is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.last_timestamp, reverse=True)
    most_recent = candidates[0]
    if (_time.time() - most_recent.last_timestamp.timestamp()) >= 300:
        return None
    return most_recent.session_id


def _fork_task_still_running(
    ci, builder, sid: str, project_path: Optional[str] = None
) -> bool:
    """True iff ``sid`` is a callstack fork-task whose callstack status
    is ``running``/``in_progress`` AND whose JSONL doesn't yet end in a
    RETURN envelope AND the project still has a live claude process
    (or the JSONL was touched within the live-mtime window).

    Three independent guards against a perpetually-``running`` report:

    1. **JSONL terminal envelope** — the callstack runtime sometimes
       fails to update the report after the child returns, leaving the
       task marked ``running`` even though its JSONL ends in
       ``{"op":"return"}``. The tail check corrects that.
    2. **Process liveness** — if every ``claude`` process for this
       project has exited and the fork's JSONL hasn't been touched
       within ``LIVE_MTIME_WINDOW_SEC``, the fork cannot still be
       running, regardless of what its report says. Catches the case
       where claude was killed mid-MCP-call (no return envelope ever
       written) and the tree would otherwise stay amber forever.
    3. ``project_path is None`` skips the process gate — happens when
       we're scanning a Claude-Code project dir that no longer maps to
       a real cwd; fall back to report+JSONL signals only.

    Mirrors :func:`unwind.processes.session_status`'s main-session
    bridge (process up OR mtime recent → live) so forks and main
    sessions agree on what "alive" means.
    """
    if not ci.has_logs or not ci.is_callstack_task(sid):
        return False
    if ci.aggregate_status_for_session(sid) != "live":
        return False
    scan = builder.get_scan(sid)
    if scan.has_returned:
        return False
    if project_path is not None and not project_activity(
        project_path
    ).claude_running:
        # No claude is running here — only call this fork live if its
        # JSONL was just touched (bridges the race where the process
        # appears moments after writing a record).
        if scan.mtime <= 0 or (time.time() - scan.mtime) >= LIVE_MTIME_WINDOW_SEC:
            return False
    return True


def _compute_session_status(
    index,
    ci,
    session_id: str,
    cwd: Optional[str],
    last_epoch: Optional[float],
    project_path: Optional[str],
    *,
    slug: str,
    active_session_id: Optional[str] = None,
    rs: Optional[RequestState] = None,
) -> str:
    """Single-source-of-truth for session status. See ``list_sessions`` for
    the full semantics; reused by ``get_session`` so the two endpoints
    can't drift.

    ``active_session_id`` (the project's most-recently-touched session,
    or ``None``) is computed once per request. When omitted we look it
    up via ``rs`` (memoized request-scoped); falls back to a direct call
    when ``rs`` is also missing.
    """
    del cwd  # currently unused; reserved for future per-session checks
    cs_status = ci.aggregate_status_for_session(session_id) if ci.has_logs else None
    is_fork = ci.has_logs and ci.is_callstack_task(session_id)

    if is_fork:
        if cs_status == "yield":
            return "yield"
        builder = _rs_builder(rs, slug)
        return (
            "live"
            if _fork_task_still_running(ci, builder, session_id, project_path)
            else "done"
        )

    # Non-fork "main" session: alive only if it's THE active session
    # for this project. ``last_epoch`` is the timestamp from the last
    # record, not file mtime — but that's not enough to disambiguate
    # which of multiple recent sessions is being driven, so we defer
    # to ``active_session_id``.
    del last_epoch
    if active_session_id is None:
        if rs is not None:
            active_session_id = _rs_active_session(rs, slug, index, project_path)
        else:
            active_session_id = _active_session_for_project(index, project_path)
    if session_id != active_session_id:
        return "done"
    if cs_status == "yield":
        return "yield"
    # Reuse the canvas builder's cached SessionScan (mtime/size-keyed)
    # instead of re-walking the JSONL: the at-user-prompt state machine
    # is identical, and sharing the cache means /sessions and /canvas
    # pay the scan once between them.
    builder = _rs_builder(rs, slug)
    if builder.get_scan(session_id).at_user_prompt:
        return "yield"
    return "live"


class SpawnCard(BaseModel):
    """A callstack spawn that's not anchored to any tool_use in this JSONL.

    Callstack child sessions invoke further calls via a JSON envelope in the
    assistant's response (parsed by callstack's runtime), not via an MCP
    tool_use. So /task-c's spawn of /task-e/task-f never appears as a
    tool_use in /task-c's JSONL — but the report.yaml records it.
    """

    invoke_id: str
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    # Canonical status (``done|live|yield|failed``) or ``None`` when
    # unresolved. The wire used to send the raw report.yaml string here,
    # forcing every consumer (web/derive-rows, TracePane extras) to
    # re-translate against ``"running"|"in_progress"`` literals. Now
    # there's exactly one translator: ``messages.status_for_spawn``.
    status: Optional[str]
    children: list[str]
    tasks: list[str]  # task labels like "/task-e"


class MessagesResponse(BaseModel):
    session_id: str
    messages: list[dict]
    last_uuid: Optional[str]
    file_offset: int
    extra_spawns: list[SpawnCard] = []
    # The parent's verdict on this session, translated to canonical
    # (``done|live|yield|failed`` or ``None`` when this session isn't a
    # callstack task). Source: the parent's ``report.yaml`` TaskNode,
    # pulled through ``status.from_raw`` so the wire format matches
    # WindowNode.status / spawn_status everywhere else.
    terminal_status: Optional[str] = None
    terminal_error: Optional[str] = None


@router.get(
    "/projects/{slug}/sessions/{session_id}/messages",
    response_model=MessagesResponse,
)
def get_messages(
    slug: SlugPath,
    session_id: SessionIdPath,
    include_meta: bool = Query(False),
    since_uuid: Optional[str] = Query(None),
    rs: RequestState = Depends(get_request_state),
) -> MessagesResponse:
    """Return normalized messages for a session.

    Pass ``since_uuid`` (typically the ``last_uuid`` from a prior response)
    to get only messages whose record landed AFTER that uuid. The wire
    payload shrinks to the delta while ``file_offset`` and ``last_uuid``
    still reflect the full file so the client can resume tailing.
    ``extra_spawns`` always reflects the full file (it's cheap to recompute
    from the cached records and the client expects it to be authoritative
    on every response).
    """
    ci = _rs_callstack(rs, slug)
    si = rs.memoize(("subagents", slug), lambda: subagent_index_for_slug(slug))
    resolver = _rs_resolver(rs, slug)

    # Subagent traces resolve through a separate index.
    if session_id.startswith(SUBAGENT_PREFIX):
        sa_path = si.resolve(session_id)
        if sa_path is None:
            raise HTTPException(status_code=404, detail="subagent not found")
        page = read_messages(sa_path, include_meta=include_meta)
        annotate_spawns(
            page.messages,
            slug_callstack=ci,
            current_session_id=session_id,
            spawn_resolver=resolver,
        )
        tail = _slice_after_uuid(page.messages, since_uuid)
        return MessagesResponse(
            session_id=session_id,
            messages=[m.to_dict() for m in tail],
            last_uuid=page.last_uuid,
            file_offset=page.file_offset,
        )

    # Workflow run / phase nodes have no transcript of their own — synthesise
    # a summary page (phases, totals, result, logs) from the rollup so the
    # node is clickable and its card shows what the run did. Agent leaves
    # (``agent-<id>``) already resolved through the subagent branch above.
    if session_id.startswith(WORKFLOW_PREFIX):
        wf = rs.memoize(("workflows", slug), lambda: workflow_index_for_slug(slug))
        run = wf.resolve_run(session_id)
        if run is None:
            raise HTTPException(status_code=404, detail="workflow run not found")
        messages = _workflow_summary_messages(session_id, run)
        return MessagesResponse(
            session_id=session_id,
            messages=[m.to_dict() for m in _slice_after_uuid(messages, since_uuid)],
            last_uuid=messages[-1].uuid if messages else None,
            file_offset=0,
        )

    index = _rs_index(rs, slug)
    jsonl = index.jsonl_path_for(session_id)
    if jsonl is None:
        raise HTTPException(status_code=404, detail="session not found")

    page = read_messages(jsonl, include_meta=include_meta)
    # Anchor spawns to tool_uses in one pass — the resolver handles
    # callstack reports + fork detector + subagent index uniformly.
    spawns = resolver.anchor_to_messages(session_id, page.messages)
    annotate_spawns(
        page.messages,
        slug_callstack=ci,
        current_session_id=session_id,
        spawn_resolver=resolver,
    )

    # Fork delta: when this session has callstack ancestors, the JSONL begins
    # with the inherited prefix (same uuids as the parent's content). Strip
    # those so the inline trace only shows what THIS fork did since diverging.
    if ci.has_logs:
        chain = ci.parent_chain(session_id)
        ancestor_uuids: set[str] = set()
        for ancestor_id in chain:
            anc_path = index.jsonl_path_for(ancestor_id)
            if anc_path is not None:
                ancestor_uuids |= collect_uuids(anc_path)
        if ancestor_uuids:
            page.messages = [
                m for m in page.messages
                if base_uuid(m.uuid) not in ancestor_uuids
            ]

    # Surface spawns that don't have a tool_use anchor in this JSONL —
    # the resolver already knows which spawns lack ``parent_tool_use_id``.
    # One SpawnCard per unanchored Spawn (the frontend buckets them into
    # per-window child cards using ``started_at``).
    extra: list[SpawnCard] = []
    for s in spawns:
        if s.parent_tool_use_id or s.kind != "call":
            continue
        # Single source of truth for "what's this spawn's status?" —
        # same function the per-tool_use spawn_status decoration uses,
        # so an extras CALL row and an anchored CALL row for the same
        # child can never disagree.
        canonical = status_for_spawn(s, ci)
        extra.append(
            SpawnCard(
                invoke_id=s.invoke_id or "",
                started_at=s.started_at,
                ended_at=s.ended_at,
                status=canonical,
                children=[s.child_session_id],
                tasks=[s.label] if s.label else [],
            )
        )

    # Pull the parent's verdict on this session straight from its
    # report.yaml TaskNode, then canonicalise so every consumer compares
    # against the same vocabulary as WindowNode.status / spawn_status.
    # The child's own JSONL never records this — the call runtime
    # classifies the result (e.g. "child emitted no parseable envelope")
    # after the child exits.
    terminal_status: Optional[str] = None
    terminal_error: Optional[str] = None
    task_node = ci.task_node_for_session(session_id)
    if task_node is not None:
        terminal_status = _status.from_raw(task_node.status)
        terminal_error = task_node.error

    tail = _slice_after_uuid(page.messages, since_uuid)
    return MessagesResponse(
        session_id=session_id,
        messages=[m.to_dict() for m in tail],
        last_uuid=page.last_uuid,
        file_offset=page.file_offset,
        extra_spawns=extra,
        terminal_status=terminal_status,
        terminal_error=terminal_error,
    )


def _slice_after_uuid(messages, since_uuid):
    """Return messages whose record landed AFTER ``since_uuid``.

    Matching is by ``base_uuid`` because the normalizer can fan one record
    into multiple ``f"{uuid}:{order}"`` messages — they all share the same
    base uuid and must be skipped together. Unknown / missing uuid → full
    list (the client probably reset its cache).
    """
    if not since_uuid:
        return messages
    last_idx = -1
    for i, m in enumerate(messages):
        if base_uuid(m.uuid) == since_uuid:
            last_idx = i
    if last_idx < 0:
        return messages
    return messages[last_idx + 1:]


def _workflow_summary_messages(
    session_id: str, run: WorkflowRun
) -> list[Message]:
    """Synthesise a few pseudo-messages describing a workflow run, for the
    run/phase nodes (which have no transcript of their own). Rendered in the
    node's card body and the trace pane like any other message stream."""
    counts: dict[int, int] = {}
    for a in run.agents:
        counts[a.phase_index] = counts.get(a.phase_index, 0) + 1

    head = [
        f"# Workflow: {run.name}",
        "",
        f"- Run id: `{run.run_id}`",
        f"- Status: {run.status}",
        f"- Agents: {len(run.agents)}",
        f"- Total tokens: {run.total_tokens:,}",
    ]
    if run.partial:
        head.append("- _running — summary is partial until the run completes_")
    head += ["", "## Phases"]
    for p in run.phases:
        title = p.title or "(unnamed)"
        head.append(f"{p.index}. {title} — {counts.get(p.index, 0)} agent(s)")

    msgs = [
        Message(
            uuid=f"{session_id}:summary",
            session_id=session_id,
            role="assistant",
            timestamp=run.started_at,
            text="\n".join(head),
            raw_type="assistant",
        )
    ]
    if run.result_preview:
        msgs.append(
            Message(
                uuid=f"{session_id}:result",
                session_id=session_id,
                role="assistant",
                timestamp=run.ended_at or run.started_at,
                text="## Result\n\n```\n" + run.result_preview + "\n```",
                raw_type="assistant",
            )
        )
    if run.log_lines:
        msgs.append(
            Message(
                uuid=f"{session_id}:logs",
                session_id=session_id,
                role="assistant",
                timestamp=run.ended_at or run.started_at,
                text="## Logs\n\n" + "\n".join(run.log_lines[-40:]),
                raw_type="assistant",
            )
        )
    return msgs


class CanvasTreeResponse(BaseModel):
    """The canvas window-tree rooted at one session.

    ``root`` is a recursive WindowNode dict; ``all_windows`` is the
    flat list (also recursive — but useful as an index for lookups
    on the frontend without walking the tree).
    """

    root: dict
    all_windows: list[dict]


@router.get(
    "/projects/{slug}/sessions/{session_id}/canvas",
)
def get_canvas_tree(
    slug: SlugPath,
    session_id: SessionIdPath,
    if_none_match: Optional[str] = Header(default=None, alias="If-None-Match"),
    rs: RequestState = Depends(get_request_state),
) -> Response:
    """Return the canvas window-tree for a root session.

    Computed once per request (cheap — scans are cached per project
    in the CanvasTreeBuilder). Wraps in an ETag for HTTP-level
    caching, so polling clients get a 304 when nothing's changed.
    """
    index = _rs_index(rs, slug)
    if index.jsonl_path_for(session_id) is None and not any(
        s.session_id == session_id for s in index.list_sessions()
    ):
        raise HTTPException(status_code=404, detail="session not found")

    project_path = (
        str(index.paths.source_path) if index.paths.has_project_dir else None
    )
    claude_running = (
        project_path is not None
        and project_activity(project_path).claude_running
    )

    # Cheap ETag derived from filesystem state (no body serialization).
    # If the (JSONLs + callstack reports) fingerprint matches the
    # client's If-None-Match we can 304 without touching CanvasTree at
    # all. The session_id is mixed in because the same project state
    # produces different trees per root, and claude_running is mixed
    # in because it flips "live" badges without changing any file.
    state_sig = (project_state_signature(slug), session_id, claude_running)
    etag = '"' + hashlib.sha1(
        _json.dumps(state_sig, default=str, sort_keys=True).encode("utf-8")
    ).hexdigest() + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    builder = _rs_builder(rs, slug)
    resolver = _rs_resolver(rs, slug)
    ci = _rs_callstack(rs, slug)
    summaries = {s.session_id: s for s in index.list_sessions()}

    # Compute the project's active session once per request; the
    # canvas tree only renders one tree per call but build_canvas_tree
    # may invoke ``is_live`` for many sessions (subagents, callstack
    # children) — none should flip live just because their JSONL was
    # touched recently when they aren't the active main session.
    active_session_id = _rs_active_session(rs, slug, index, project_path)

    def is_live(sid: str) -> bool:
        # The project's active main session is always live. Fork-tasks
        # are never the active session (the parent is), so they need
        # their own "still running" check — shared with
        # ``_compute_session_status`` so the two surfaces agree.
        return sid == active_session_id or _fork_task_still_running(
            ci, builder, sid, project_path
        )

    def title_for(sid: str) -> Optional[str]:
        s = summaries.get(sid)
        if s is None:
            return None
        return s.custom_title or s.title or None

    root, all_windows = build_canvas_tree(
        index.paths.project_dir,
        session_id,
        spawn_resolver=resolver,
        builder=builder,
        is_live_session=is_live,
        title_for=title_for,
    )
    body = {
        "root": root.to_dict(),
        "all_windows": [w.to_dict() for w in all_windows],
    }
    serialized = _json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return Response(
        content=serialized,
        media_type="application/json",
        headers={"ETag": etag},
    )


class TreeResponse(BaseModel):
    session_id: str
    children: list[dict]
    has_callstack_logs: bool


@router.get(
    "/projects/{slug}/sessions/{session_id}/tree",
    response_model=TreeResponse,
)
def get_tree(
    slug: SlugPath,
    session_id: SessionIdPath,
    rs: RequestState = Depends(get_request_state),
) -> TreeResponse:
    from ..callstack import TaskNode

    ci = _rs_callstack(rs, slug)
    children = ci.build_subtree(session_id)
    fd = rs.memoize(
        ("fork_detector", slug), lambda: fork_detector_for_slug(slug)
    )

    # Resolve in-flight tree rows that don't yet have session_ids by matching
    # the task name against fork sessions' first divergent user message.
    def resolve(node, parent_sid: str) -> None:
        if not node.session_id and parent_sid and node.task:
            sid = fd.find_session_by_divergence_text(parent_sid, node.task)
            if sid is not None:
                node.session_id = sid
        for c in node.children:
            resolve(c, node.session_id or parent_sid)
        # Attach subagents recursively under each call-tree node.
        if node.session_id:
            for sa in _subagent_nodes(slug, node.session_id, depth=node.depth + 1):
                node.children.append(sa)

    for ch in children:
        resolve(ch, session_id)

    # If there were no callstack reports at all, fall back to "fork detector
    # children" so the user still sees a tree for in-flight runs.
    if not children:
        fork_sids = fd.children_of(session_id)
        if fork_sids:
            fd.find_session_by_divergence_text(session_id, "")
            for fork_sid in fork_sids:
                text = fd.divergence_text_for(fork_sid)
                children.append(
                    TaskNode(
                        session_id=fork_sid,
                        task=(text or "(in-flight)").strip(),
                        status="running",
                        depth=1,
                        duration_seconds=None,
                        summary=None,
                        error=None,
                        invoke_id=None,
                    )
                )

    # Subagents directly invoked by the root session.
    children.extend(_subagent_nodes(slug, session_id, depth=1))

    return TreeResponse(
        session_id=session_id,
        children=[n.to_dict() for n in children],
        has_callstack_logs=ci.has_logs,
    )


def _subagent_nodes(slug: str, parent_session_id: str, depth: int):
    from ..callstack import TaskNode

    if parent_session_id.startswith(SUBAGENT_PREFIX):
        # Subagents don't recursively have their own subagent index in this
        # repo layout — Claude only persists subagents under real session ids.
        return []
    si = subagent_index_for_slug(slug)
    out = []
    for inv in si.list_for_session(parent_session_id):
        out.append(
            TaskNode(
                session_id=inv.synthetic_session_id,
                task=inv.description,
                status="complete",
                depth=depth,
                duration_seconds=None,
                summary=f"{inv.agent_type} · {inv.message_count} msgs",
                error=None,
                started_at=inv.created_at,
                kind="subagent",
            )
        )
    return out
