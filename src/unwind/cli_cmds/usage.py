"""``unwind usage`` verbs: cross-project token + USD reports."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import typer

from ..usage_report import (
    EPHEMERAL_PATH_PREFIXES,
    EPHEMERAL_SLUG_PREFIXES,
    BucketedReport,
    bucket_for_display,
    build_month_report,
)
from . import _common, _render

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _serialize_for_json(b: BucketedReport) -> dict:
    """Project the bucketed report into a flat, UI-ready JSON payload."""
    r = b.report

    def _p(p):
        return {
            "slug": p.slug,
            "source_path": p.source_path,
            "session_count": p.session_count,
            "usage": p.usage,
            "cost": p.cost,
            "total_tokens": p.total_tokens,
            "total_cost": p.total_cost,
        }

    def _g(g):
        if g is None:
            return None
        return {
            "label": g.label,
            "project_count": g.project_count,
            "session_count": g.session_count,
            "usage": g.usage,
            "cost": g.cost,
            "total_tokens": g.total_tokens,
            "total_cost": g.total_cost,
        }

    return {
        "month": r.month,
        "tz_name": r.tz_name,
        "window_start_utc": r.window_start_utc,
        "window_end_utc": r.window_end_utc,
        "project_count": r.project_count,
        "session_count": r.session_count,
        "grand_usage": r.grand_usage,
        "grand_cost": r.grand_cost,
        "total_tokens": r.total_tokens,
        "total_cost": r.total_cost,
        "ephemeral_path_prefixes": list(EPHEMERAL_PATH_PREFIXES),
        "ephemeral_slug_prefixes": list(EPHEMERAL_SLUG_PREFIXES),
        "buckets": {
            "top": [_p(p) for p in b.top],
            "ephemeral": _g(b.ephemeral),
            "other": _g(b.other),
        },
        # Full list too, so an API caller can re-bucket if it wants.
        "projects": [_p(p) for p in r.projects],
    }


@app.command("report")
def report(
    month: Optional[str] = typer.Option(
        None,
        "--month",
        help="Local calendar month as 'YYYY-MM'. Defaults to the current month.",
    ),
    top: int = typer.Option(
        20, "--top", min=1, help="Number of real projects to list before bucketing the tail."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show token usage + USD cost across every known project, for one
    local calendar month.

    The grand total sums every event in the window (including ephemeral
    test runs and the long tail). The per-project breakdown shows the
    top-N real projects, then rolls ephemeral test runs and the
    remaining real projects into two synthetic rows so the table stays
    readable when you have hundreds of projects.
    """
    if month is None:
        month = datetime.now().astimezone().strftime("%Y-%m")
    try:
        rep = build_month_report(month)
    except ValueError as exc:
        raise _common.usage_error(str(exc))
    bucketed = bucket_for_display(rep, top_n=top)
    if json_out:
        _common.echo_json(_serialize_for_json(bucketed))
        return
    _render.render_usage_report(bucketed)
