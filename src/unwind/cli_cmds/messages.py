"""``unwind messages`` verbs: dump, grep."""
from __future__ import annotations

import json
import re
from typing import Optional

import typer

from ..jsonl import collect_uuids
from ..messages import Message, annotate_spawns, base_uuid, read_messages
from ..registry import (
    callstack_for_slug,
    index_for_slug,
    spawn_resolver_for_slug,
    subagent_index_for_slug,
)
from ..subagents import SUBAGENT_PREFIX
from . import _common, _render

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _load_messages(
    slug: str,
    session_id: str,
    *,
    include_meta: bool,
    strip_inherited: bool,
) -> list[Message]:
    ci = callstack_for_slug(slug)
    si = subagent_index_for_slug(slug)
    resolver = spawn_resolver_for_slug(slug)
    if session_id.startswith(SUBAGENT_PREFIX):
        sa_path = si.resolve(session_id)
        if sa_path is None:
            raise _common.not_found(f"subagent {session_id!r} not found in {slug}")
        page = read_messages(sa_path, include_meta=include_meta)
        annotate_spawns(
            page.messages,
            slug_callstack=ci,
            current_session_id=session_id,
            spawn_resolver=resolver,
        )
        return page.messages

    index = index_for_slug(slug)
    jsonl = index.jsonl_path_for(session_id)
    if jsonl is None:
        raise _common.not_found(f"session {session_id!r} not found in {slug}")
    page = read_messages(jsonl, include_meta=include_meta)
    annotate_spawns(
        page.messages,
        slug_callstack=ci,
        current_session_id=session_id,
        spawn_resolver=resolver,
    )

    if strip_inherited and ci.has_logs:
        chain = ci.parent_chain(session_id)
        ancestor_uuids: set[str] = set()
        for ancestor_id in chain:
            anc_path = index.jsonl_path_for(ancestor_id)
            if anc_path is not None:
                ancestor_uuids |= collect_uuids(anc_path)
        if ancestor_uuids:
            page.messages = [
                m for m in page.messages if base_uuid(m.uuid) not in ancestor_uuids
            ]
    return page.messages


@app.command("dump")
def dump_messages(
    session_id: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    fmt: str = typer.Option(
        "text", "--format", help="text | json | markdown.", case_sensitive=False
    ),
    include_meta: bool = typer.Option(False, "--include-meta"),
    role: Optional[list[str]] = typer.Option(
        None, "--role", help="Filter to one or more roles."
    ),
    tool: Optional[list[str]] = typer.Option(
        None, "--tool", help="Filter to one or more tool names."
    ),
    limit: Optional[int] = typer.Option(None, "--limit", min=1),
    strip_inherited: bool = typer.Option(False, "--strip-inherited"),
) -> None:
    """Dump a session's normalized messages."""
    fmt_norm = (fmt or "text").lower()
    if fmt_norm not in ("text", "json", "markdown"):
        raise _common.usage_error("--format must be one of: text, json, markdown")
    paths = _common.resolve_project(project, harness)
    messages = _load_messages(
        paths.slug,
        session_id,
        include_meta=include_meta,
        strip_inherited=strip_inherited,
    )

    if role:
        wanted = {r.lower() for r in role}
        messages = [m for m in messages if m.role.lower() in wanted]
    if tool:
        wanted_tools = {t for t in tool}
        messages = [
            m
            for m in messages
            if (m.role == "tool_use" and (m.tool_name or "") in wanted_tools)
        ]
    if limit is not None:
        messages = messages[:limit]

    if fmt_norm == "json":
        _common.echo_json([m.to_dict() for m in messages])
    elif fmt_norm == "markdown":
        _render.render_messages_markdown(messages)
    else:
        _render.render_messages_text(messages, include_meta=include_meta)


@app.command("grep")
def grep_messages(
    session_id: str = typer.Argument(...),
    pattern: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    regex: bool = typer.Option(False, "--regex"),
) -> None:
    """Grep a session's messages for a literal substring or regex pattern."""
    paths = _common.resolve_project(project, harness)
    messages = _load_messages(
        paths.slug, session_id, include_meta=False, strip_inherited=False
    )
    matcher = re.compile(pattern) if regex else None
    for m in messages:
        haystack = _haystack_for(m)
        if not haystack:
            continue
        if matcher is not None:
            if not matcher.search(haystack):
                continue
        else:
            if pattern not in haystack:
                continue
        ts = m.timestamp.isoformat() if m.timestamp else "-"
        snippet = haystack.replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:197] + "…"
        typer.echo(f"{ts}  {m.role:13s}  {snippet}")


def _haystack_for(m: Message) -> str:
    if m.role == "tool_use":
        try:
            return f"{m.tool_name or ''} {json.dumps(m.tool_input, default=str)}"
        except (TypeError, ValueError):
            return f"{m.tool_name or ''} {m.tool_input!r}"
    if m.role == "tool_result":
        r = m.tool_result
        if isinstance(r, str):
            return r
        if isinstance(r, list):
            parts = []
            for block in r:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            return "\n".join(parts)
        return str(r) if r is not None else ""
    return m.text or ""
