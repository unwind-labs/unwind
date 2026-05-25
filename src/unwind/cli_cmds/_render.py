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


def render_usage_breakdown(root: Any, _all_windows: list[Any]) -> None:
    """Render token-usage and USD-cost per window of a session tree.

    Mirrors the layout of the canvas card's footer (categories as rows,
    scopes as columns): for each window we print one block with category
    rows (Cache Write / Cache Read / Read / Write) and scope columns
    (Self tokens / Subtree tokens / USD cost). The root's grand-total
    line appears at the bottom, matching the canvas. """
    # Order: BFS from root by start time — same column order as the canvas.
    ordered: list[Any] = []
    seen: set[str] = set()
    def visit(node: Any) -> None:
        if node.window_id in seen:
            return
        seen.add(node.window_id)
        ordered.append(node)
        for c in sorted(node.children, key=lambda x: x.window_start or datetime.min):
            visit(c)
    visit(root)

    categories = [
        ("cw", "Cache Write"),
        ("cr", "Cache Read"),
        ("r", "Read"),
        ("w", "Write"),
    ]

    for w in ordered:
        label = w.label or w.session_id[:12]
        kind = w.kind
        status = w.status
        header = (
            f"[bold cyan]{label}[/]  "
            f"[dim]{w.window_id}[/]  "
            f"[magenta]{kind}[/]  [yellow]{status}[/]"
        )
        _console.print(header)
        t = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
        t.add_column("", style="dim")
        t.add_column("Self", justify="right")
        t.add_column("Subtree", justify="right")
        t.add_column("USD (subtree)", justify="right", style="green")
        for key, lbl in categories:
            t.add_row(
                lbl,
                _fmt_tokens(w.self_usage.get(key, 0)),
                _fmt_tokens(w.subtree_usage.get(key, 0)),
                _fmt_usd(w.subtree_cost.get(key, 0.0)),
            )
        _console.print(t)
        _console.print("")

    cost_total = sum(root.subtree_cost.values())
    tok_total = sum(root.subtree_usage.values())
    _console.print(
        f"[bold]totals[/]: {_fmt_tokens(tok_total)} tokens · "
        f"[green]{_fmt_usd(cost_total)}[/] across {len(ordered)} window(s)"
    )


def _fmt_tokens(n: int) -> str:
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


def _fmt_usd(n: float) -> str:
    if not n:
        return "$0.00"
    if n < 0.01:
        return "<$0.01"
    return f"${n:,.2f}"


def _friendly_project_name(slug: str, source_path: str) -> str:
    """Best-effort short display name for a project.

    If ``source_path`` looks like a real filesystem path (i.e. not just
    ``~/.claude/projects/<slug>`` synthesized for a never-registered
    project), use its last two segments. Otherwise reverse-translate the
    slug back to a path by replacing leading ``-`` with ``/`` and use
    its last two segments. This is lossy (slugs aren't reversible if a
    path component itself contained ``-``) but good enough for a
    display label.
    """
    if "/.claude/projects/" not in source_path:
        path = source_path.rstrip("/")
    else:
        # Synthesized — recover something readable from the slug.
        path = "/" + slug.lstrip("-").replace("-", "/")
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2] + "/" + parts[-1]
    return parts[-1] if parts else slug


def render_usage_report(bucketed: Any) -> None:
    """Render a :class:`unwind.usage_report.BucketedReport` as a Rich table.

    Format mirrors :func:`render_session_usage` columns (CW / CR / In /
    Out for both tokens and USD) so the Reports view feels continuous
    with the per-session canvas footer. Uses a fixed wide console width
    so Rich doesn't crush the numeric columns down to single-character
    rows on narrow terminals.
    """
    report = bucketed.report
    title = (
        f"Usage — {report.month} ({report.tz_name}) · "
        f"{report.session_count} sessions across {report.project_count} projects"
    )
    table = Table(title=title, show_lines=False, expand=False)
    table.add_column("project", overflow="fold", min_width=24, max_width=40)
    table.add_column("sess", justify="right", no_wrap=True)
    table.add_column("CW", justify="right", no_wrap=True)
    table.add_column("CR", justify="right", no_wrap=True)
    table.add_column("In", justify="right", no_wrap=True)
    table.add_column("Out", justify="right", no_wrap=True)
    table.add_column("$CW", justify="right", no_wrap=True)
    table.add_column("$CR", justify="right", no_wrap=True)
    table.add_column("$In", justify="right", no_wrap=True)
    table.add_column("$Out", justify="right", no_wrap=True)
    table.add_column("$Total", justify="right", style="bold", no_wrap=True)

    def _row(name: str, sess: int, u: dict, c: dict, total: float, style: str = ""):
        table.add_row(
            f"[{style}]{name}[/]" if style else name,
            str(sess),
            _fmt_tokens(u.get("cw", 0)),
            _fmt_tokens(u.get("cr", 0)),
            _fmt_tokens(u.get("r", 0)),
            _fmt_tokens(u.get("w", 0)),
            _fmt_usd(c.get("cw", 0.0)),
            _fmt_usd(c.get("cr", 0.0)),
            _fmt_usd(c.get("r", 0.0)),
            _fmt_usd(c.get("w", 0.0)),
            _fmt_usd(total),
        )

    for p in bucketed.top:
        _row(
            _friendly_project_name(p.slug, p.source_path),
            p.session_count,
            p.usage,
            p.cost,
            p.total_cost,
        )

    if bucketed.ephemeral is not None:
        g = bucketed.ephemeral
        _row(g.label, g.session_count, g.usage, g.cost, g.total_cost, style="dim")
    if bucketed.other is not None:
        g = bucketed.other
        _row(g.label, g.session_count, g.usage, g.cost, g.total_cost, style="dim")

    # Grand total row — always sums everything in the report, including
    # ephemerals and the tail, so the headline number matches.
    _row(
        "TOTAL",
        report.session_count,
        report.grand_usage,
        report.grand_cost,
        report.total_cost,
        style="bold green",
    )
    # Force a wide console so the 11 columns don't collapse on a narrow
    # terminal. Rich's default auto-detect uses 80 cols when stdout
    # isn't a TTY (e.g. piped to a file), which crushes the table.
    Console(width=180).print(table)


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
    base = f"{role}{tool} · {ts}"
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
    from ..jsonl import stringify_tool_result

    text = stringify_tool_result(r)
    if text:
        return text
    # CLI variant adds a pretty JSON fallback for unrecognised shapes.
    if r is None or isinstance(r, (str, list)):
        return text
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
