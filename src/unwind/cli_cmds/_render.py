"""Rich/text renderers used by the noun-verb CLI commands."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from rich.console import Console
from rich.table import Table

from ..messages import Message


_console = Console()


def _fmt_ts(ts: Optional[datetime]) -> str:
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%Y-%m-%d %H:%M")


def render_project_table(rows: list[Any]) -> None:
    table = Table(title="Projects", show_lines=False)
    table.add_column("slug", style="cyan", overflow="fold")
    table.add_column("path", overflow="fold")
    table.add_column("sessions", justify="right")
    table.add_column("last activity")
    for r in rows:
        table.add_row(
            r.slug,
            r.source_path,
            str(r.session_count),
            _fmt_ts(r.last_activity),
        )
    _console.print(table)


def render_project_show(row: Any) -> None:
    _console.print(f"[bold]slug[/]: {row.slug}")
    _console.print(f"[bold]path[/]: {row.source_path}")
    _console.print(f"[bold]sessions[/]: {row.session_count}")
    _console.print(f"[bold]last activity[/]: {_fmt_ts(row.last_activity)}")


def render_session_table(rows: list[Any]) -> None:
    table = Table(title="Sessions", show_lines=False)
    table.add_column("session_id", style="cyan", overflow="fold")
    table.add_column("status")
    table.add_column("title", overflow="fold")
    table.add_column("msgs", justify="right")
    table.add_column("calls", justify="right")
    table.add_column("last")
    for r in rows:
        table.add_row(
            r.session_id,
            r.status,
            (r.custom_title or r.title or "")[:80],
            str(r.message_count),
            str(r.top_level_call_count),
            _fmt_ts(r.last_timestamp),
        )
    _console.print(table)


def render_session_show(row: Any) -> None:
    _console.print(f"[bold]session_id[/]: {row.session_id}")
    _console.print(f"[bold]title[/]: {row.title}")
    if row.custom_title:
        _console.print(f"[bold]custom_title[/]: {row.custom_title}")
    _console.print(f"[bold]status[/]: {row.status}")
    _console.print(f"[bold]messages[/]: {row.message_count}")
    _console.print(f"[bold]first[/]: {_fmt_ts(row.first_timestamp)}")
    _console.print(f"[bold]last[/]: {_fmt_ts(row.last_timestamp)}")
    if row.cwd:
        _console.print(f"[bold]cwd[/]: {row.cwd}")
    if row.git_branch:
        _console.print(f"[bold]git_branch[/]: {row.git_branch}")


def render_task_tree(
    root_session_id: str,
    children: list[dict],
    *,
    depth_limit: Optional[int] = None,
) -> None:
    """Render a task tree as an indented Unicode tree on stdout."""
    _console.print(f"[bold cyan]{root_session_id}[/]")
    _render_children(children, prefix="", depth=0, depth_limit=depth_limit)


def _render_children(
    nodes: list[dict],
    prefix: str,
    depth: int,
    depth_limit: Optional[int],
) -> None:
    if depth_limit is not None and depth >= depth_limit:
        return
    last = len(nodes) - 1
    for i, n in enumerate(nodes):
        connector = "└── " if i == last else "├── "
        kind = n.get("kind", "call")
        kind_tag = "[magenta]subagent[/]" if kind == "subagent" else "[green]call[/]"
        status = n.get("status") or "?"
        sid = n.get("session_id") or "(unresolved)"
        task = n.get("task") or ""
        _console.print(
            f"{prefix}{connector}{kind_tag} {task}  "
            f"[dim]{sid}[/]  [yellow]{status}[/]"
        )
        next_prefix = prefix + ("    " if i == last else "│   ")
        kids = n.get("children") or []
        _render_children(kids, next_prefix, depth + 1, depth_limit)


def render_messages_text(
    messages: Iterable[Message],
    *,
    include_meta: bool = False,
) -> None:
    """Print messages as role-headed prose (mirrors web TracePane).

    ``include_meta`` controls whether system/meta messages — already loaded
    upstream when the flag is set — appear in the rendered output. We accept
    the flag here for symmetry with the loader and as a second-stage filter
    for callers that hand us pre-loaded message lists.
    """
    for m in messages:
        if not include_meta and m.role == "system":
            continue
        head = _role_header(m)
        _console.print(head)
        body = _message_body_text(m)
        if body:
            for line in body.splitlines() or [""]:
                _console.print(f"  {line}")
        _console.print("")


def render_messages_markdown(messages: Iterable[Message]) -> None:
    for m in messages:
        head = _role_header(m, plain=True)
        print(f"### {head}")
        body = _message_body_text(m)
        if body:
            print()
            print(body)
        print()


def _role_header(m: Message, *, plain: bool = False) -> str:
    role = m.role
    ts = _fmt_ts(m.timestamp)
    tool = f" {m.tool_name}" if m.tool_name else ""
    inherited = " (inherited)" if m.is_inherited else ""
    base = f"{role}{tool} · {ts}{inherited}"
    if plain:
        return base
    color = {
        "user": "green",
        "assistant": "blue",
        "tool_use": "magenta",
        "tool_result": "yellow",
        "system": "dim",
    }.get(role, "white")
    return f"[bold {color}]{base}[/]"


def _message_body_text(m: Message) -> str:
    if m.role == "tool_use":
        parts = []
        if m.tool_input is not None:
            parts.append(f"input: {_short_json(m.tool_input)}")
        if m.spawn_kind and m.spawn_session_ids:
            parts.append(
                f"spawned {m.spawn_kind}: "
                f"{', '.join(s for s in m.spawn_session_ids if s)}"
            )
        return "\n".join(parts)
    if m.role == "tool_result":
        parts = []
        if m.tool_result_for:
            parts.append(f"for: {m.tool_result_for}")
        result_text = _stringify_result(m.tool_result)
        if result_text:
            parts.append(result_text)
        if m.is_error:
            parts.insert(0, "[error]")
        return "\n".join(parts)
    return m.text or ""


def _stringify_result(r: Any) -> str:
    if r is None:
        return ""
    if isinstance(r, str):
        return r
    if isinstance(r, list):
        out = []
        for block in r:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    return _short_json(r)


def _short_json(value: Any, limit: int = 200) -> str:
    import json as _json

    try:
        s = _json.dumps(value, default=str)
    except (TypeError, ValueError):
        s = str(value)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s
