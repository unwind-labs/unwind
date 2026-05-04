"""Session endpoints: list sessions, get messages."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel

import hashlib
import json as _json
from pathlib import Path

from ..canvas_tree import build_canvas_tree
from ..jsonl import collect_uuids, iter_lines
from ..messages import annotate_spawns, base_uuid, read_messages
from ..processes import project_activity, session_status
from ..registry import (
    callstack_for_slug,
    canvas_tree_builder_for_slug,
    fork_detector_for_slug,
    index_for_slug,
    subagent_index_for_slug,
)
from ..subagents import SUBAGENT_PREFIX

router = APIRouter(tags=["sessions"])


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
    slug: str,
    include_forks: bool = Query(False),
) -> list[SessionRow]:
    """List Claude sessions in the project.

    By default, sessions that are callstack forks (i.e. appear as a child task
    in some ``report.yaml``) are hidden — they show up nested under their
    parent in the call-tree pane instead. Pass ``include_forks=true`` to see
    everything.
    """
    index = index_for_slug(slug)
    project_path = str(index.paths.source_path) if index.paths.has_project_dir else None

    ci = callstack_for_slug(slug)
    fork_ids: set[str] = set()
    if not include_forks:
        if ci.has_logs:
            fork_ids |= ci.all_child_session_ids()
        # Heuristic detector: any session sharing its head uuid with another
        # older session is a fork. Catches in-flight forks before report.yaml
        # is written.
        fork_ids |= fork_detector_for_slug(slug).fork_session_ids()

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
        # still show done even if a claude process is alive for the project:
        #
        # 1. callstack ``yielded`` → ``yield``: a child is paused waiting
        #    for the user; trust this regardless of process state.
        # 2. callstack ``running``/``in_progress`` → ``live``.
        # 3. callstack ``complete``/``failed``/``error``:
        #     a. If this session IS a callstack task (a fork) → ``done``.
        #        callstack tracked its lifecycle precisely; trust the
        #        terminal verdict even with a live project process.
        #     b. Otherwise (main session — only ever a ``parent_session``) →
        #        defer to process detection. The descendants finished but
        #        the user's claude process is still active.
        # 4. No callstack entry at all → defer to process detection.
        status = "done"
        cs_status = ci.aggregate_status_for_session(s.session_id) if ci.has_logs else None
        if cs_status is not None:
            cs_norm = cs_status.lower()
            if cs_norm == "yielded":
                status = "yield"
            elif cs_norm in ("running", "in_progress"):
                status = "live"
            elif ci.is_callstack_task(s.session_id):
                status = "done"
            else:
                status = session_status(s.cwd or project_path, last_epoch)
        else:
            status = session_status(s.cwd or project_path, last_epoch)

        # Process-detection ``live`` upgrade: if the JSONL's last record
        # is an ``away_summary`` system message, Claude Code wrote the
        # recap right before yielding control back to the user (e.g. an
        # MFA prompt awaiting input). The process is alive but actually
        # paused — surface that as ``yield`` so the canvas highlights
        # the node. The away_summary signal is strong enough to override
        # ``live`` regardless of how we arrived at it.
        if status == "live":
            jsonl = index.jsonl_path_for(s.session_id)
            if jsonl is not None and _is_at_user_yield(jsonl):
                status = "yield"

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
def get_session(slug: str, session_id: str) -> SessionRow:
    index = index_for_slug(slug)
    summary = index.get_session(session_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionRow(
        session_id=summary.session_id,
        title=summary.title,
        first_timestamp=summary.first_timestamp,
        last_timestamp=summary.last_timestamp,
        message_count=summary.message_count,
        cwd=summary.cwd,
        git_branch=summary.git_branch,
    )


class AncestorRef(BaseModel):
    session_id: str
    title: Optional[str] = None


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
    status: str
    children: list[str]
    tasks: list[str]  # task labels like "/task-e"


class MessagesResponse(BaseModel):
    session_id: str
    messages: list[dict]
    last_uuid: Optional[str]
    file_offset: int
    ancestors: list[AncestorRef] = []
    extra_spawns: list[SpawnCard] = []


@router.get(
    "/projects/{slug}/sessions/{session_id}/messages",
    response_model=MessagesResponse,
)
def get_messages(
    slug: str,
    session_id: str,
    include_meta: bool = Query(False),
) -> MessagesResponse:
    ci = callstack_for_slug(slug)

    si = subagent_index_for_slug(slug)

    fd = fork_detector_for_slug(slug)

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
            subagent_index=si,
            fork_detector=fd,
        )
        return MessagesResponse(
            session_id=session_id,
            messages=[m.to_dict() for m in page.messages],
            last_uuid=page.last_uuid,
            file_offset=page.file_offset,
            ancestors=[],
        )

    index = index_for_slug(slug)
    jsonl = index.jsonl_path_for(session_id)
    if jsonl is None:
        raise HTTPException(status_code=404, detail="session not found")

    page = read_messages(jsonl, include_meta=include_meta)
    annotate_spawns(
        page.messages,
        slug_callstack=ci,
        current_session_id=session_id,
        subagent_index=si,
        fork_detector=fd,
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

    # Surface callstack invocations whose parent_session is this session but
    # which don't have a matching tool_use in the JSONL (the common case for
    # all non-root callstack children — they spawn via JSON envelope, not via
    # an MCP tool call).
    extra: list[SpawnCard] = []
    anchored = {
        sid
        for m in page.messages
        if m.spawn_kind == "call"
        for sid in (m.spawn_session_ids or [])
    }
    if ci.has_logs:
        # Walk every report's task tree to surface direct child
        # invocations that aren't already anchored by an MCP tool_use in
        # this session's JSONL. ``direct_invocations_of`` returns one
        # TaskNode per invocation (NOT deduplicated by session_id) so
        # that a parent which called the same child three times produces
        # three SpawnCards, one per invocation, each with its own
        # ``started_at`` and ``invoke_id``. The frontend buckets these
        # into per-window child cards just like MCP-anchored spawns.
        invocations = ci.direct_invocations_of(session_id)
        for inv in invocations:
            if not inv.session_id or inv.session_id in anchored:
                continue
            # An invocation is "running" only when it hasn't ended yet.
            # ``yielded`` and ``complete`` both have an ``ended_at`` set
            # in the report — the child returned control to the parent
            # in either case, so the parent's call row should drop the
            # in-progress dots and show a checkmark. Only invocations
            # without an ``ended_at`` (or explicitly running with no
            # end recorded yet) keep the running indicator.
            status_lc = (inv.status or "").lower()
            actually_running = inv.ended_at is None and status_lc in (
                "running",
                "in_progress",
                "pending",
                "",
            )
            extra.append(
                SpawnCard(
                    invoke_id=inv.invoke_id or "",
                    started_at=inv.started_at,
                    ended_at=inv.ended_at,
                    status="running" if actually_running else "complete",
                    children=[inv.session_id],
                    tasks=[inv.task] if inv.task else [],
                )
            )
    else:
        # No ``.claude/callstack/log/`` for this project — but the fork
        # detector still classifies sibling JSONLs that share this session's
        # head uuid as forks (e.g. ``deep-rewrite`` runs that spawn
        # ``claude --fork-session`` subprocesses without going through
        # callstack's MCP tool). Surface them so the canvas can render the
        # tree we know is there.
        fork_sids = [s for s in fd.children_of(session_id) if s not in anchored]
        if fork_sids:
            tasks: list[str] = []
            for fsid in fork_sids:
                # Best-effort label: the divergent text the fork started with
                # (for callstack forks this is "/task-x"; otherwise the first
                # user message).
                fd.find_session_by_divergence_text(session_id, "")  # warm cache
                text = fd.divergence_text_for(fsid) or ""
                tasks.append(text.strip() or fsid[:8])
            extra.append(
                SpawnCard(
                    invoke_id="",
                    started_at=None,
                    ended_at=None,
                    status="complete",
                    children=fork_sids,
                    tasks=tasks,
                )
            )

    return MessagesResponse(
        session_id=session_id,
        messages=[m.to_dict() for m in page.messages],
        last_uuid=page.last_uuid,
        file_offset=page.file_offset,
        ancestors=[],
        extra_spawns=extra,
    )


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
    slug: str,
    session_id: str,
    if_none_match: Optional[str] = Header(default=None, alias="If-None-Match"),
) -> Response:
    """Return the canvas window-tree for a root session.

    Computed once per request (cheap — scans are cached per project
    in the CanvasTreeBuilder). Wraps in an ETag for HTTP-level
    caching, so polling clients get a 304 when nothing's changed.
    """
    index = index_for_slug(slug)
    if index.jsonl_path_for(session_id) is None and not any(
        s.session_id == session_id for s in index.list_sessions()
    ):
        raise HTTPException(status_code=404, detail="session not found")

    ci = callstack_for_slug(slug)
    si = subagent_index_for_slug(slug)
    builder = canvas_tree_builder_for_slug(slug)

    project_path = (
        str(index.paths.source_path) if index.paths.has_project_dir else None
    )
    claude_running = (
        project_path is not None
        and project_activity(project_path).claude_running
    )
    summaries = {s.session_id: s for s in index.list_sessions()}

    def is_live(sid: str) -> bool:
        # A session is "live" iff a claude process is up for the project AND
        # this session's JSONL was touched recently. We reuse session_status
        # which already encodes both signals.
        s = summaries.get(sid)
        last_epoch = s.last_timestamp.timestamp() if s and s.last_timestamp else None
        cwd = (s.cwd if s else None) or project_path
        return claude_running and session_status(cwd, last_epoch) == "live"

    def title_for(sid: str) -> Optional[str]:
        s = summaries.get(sid)
        if s is None:
            return None
        return s.custom_title or s.title or None

    root, all_windows = build_canvas_tree(
        index.paths.project_dir,
        session_id,
        ci,
        subagent_index=si,
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
    etag = '"' + hashlib.sha1(serialized).hexdigest() + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
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
def get_tree(slug: str, session_id: str) -> TreeResponse:
    from ..callstack import TaskNode

    ci = callstack_for_slug(slug)
    children = ci.build_subtree(session_id)
    fd = fork_detector_for_slug(slug)

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


def _is_at_user_yield(jsonl: Path) -> bool:
    """Whether ``jsonl``'s last record indicates Claude paused for input.

    The actual yield signal is an ``assistant`` message containing a
    ``{"op": "yield"}`` envelope inside its text — that's the message
    where Claude paused to ask the user something. The session is
    "currently yielded" iff the most recent assistant message carries
    that envelope AND no user reply has arrived since (which would
    mean the session has resumed).

    Note: ``type: system / subtype: away_summary`` is NOT a yield —
    it's the recap Claude writes when finishing work while the user
    was away. Treating it as yield (the previous logic) caused
    completed sessions to render as paused in the left pane.

    Returns ``False`` on any error so the caller safely falls back
    to the prior status.
    """
    try:
        last_yield = False
        for rec in iter_lines(jsonl):
            t = rec.get("type")
            if t == "assistant":
                msg = rec.get("message")
                text = ""
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        parts = []
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "text"
                                and isinstance(block.get("text"), str)
                            ):
                                parts.append(block["text"])
                        text = "\n".join(parts)
                last_yield = '"op": "yield"' in text or '"op":"yield"' in text
            elif t == "user":
                # A user reply after a yield = resumed; clear the flag.
                last_yield = False
        return last_yield
    except OSError:
        return False


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
