"""Usage endpoints: monthly cross-project token + USD reports.

Backs the Reports view in the web UI. The heavy lifting lives in
:mod:`unwind.usage_report` (also used by ``unwind usage report`` CLI),
so this router is intentionally thin — translate query params, call
the rollup, project into a Pydantic response.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..usage_report import (
    EPHEMERAL_PATH_PREFIXES,
    EPHEMERAL_SLUG_PREFIXES,
    bucket_for_display,
    build_month_report,
)

router = APIRouter(tags=["usage"])


class _TokenUsage(BaseModel):
    cw: int
    cr: int
    r: int
    w: int


class _TokenCost(BaseModel):
    cw: float
    cr: float
    r: float
    w: float


class ProjectUsageRow(BaseModel):
    slug: str
    source_path: str
    session_count: int
    usage: _TokenUsage
    cost: _TokenCost
    total_tokens: int
    total_cost: float


class ProjectGroupRow(BaseModel):
    """A rolled-up bucket (ephemeral or tail). Same shape as a single
    project row plus ``project_count``, so the UI can render both with
    one component."""

    label: str
    project_count: int
    session_count: int
    usage: _TokenUsage
    cost: _TokenCost
    total_tokens: int
    total_cost: float


class UsageBuckets(BaseModel):
    top: list[ProjectUsageRow]
    ephemeral: Optional[ProjectGroupRow]
    other: Optional[ProjectGroupRow]


class UsageReportResponse(BaseModel):
    month: str
    tz_name: str
    window_start_utc: datetime
    window_end_utc: datetime
    project_count: int
    session_count: int
    grand_usage: _TokenUsage
    grand_cost: _TokenCost
    total_tokens: int
    total_cost: float
    ephemeral_path_prefixes: list[str]
    ephemeral_slug_prefixes: list[str]
    buckets: UsageBuckets
    # Full per-project list so the UI can re-bucket or page beyond
    # ``top`` without a second request. Same rows as ``buckets.top``
    # plus everything else.
    projects: list[ProjectUsageRow]


@router.get("/usage", response_model=UsageReportResponse)
def usage_report(
    month: Optional[str] = Query(
        None,
        description=(
            "Local calendar month as 'YYYY-MM'. Defaults to the current "
            "month in the server's local timezone."
        ),
        pattern=r"^\d{4}-\d{2}$",
    ),
    top: int = Query(
        20,
        ge=1,
        le=200,
        description="Number of real projects to list before bucketing the tail.",
    ),
) -> UsageReportResponse:
    """Token + USD cost rollup for one local calendar month, across every
    known project.

    See :func:`unwind.usage_report.build_month_report` for the rollup
    semantics. Grand totals sum *every* event in the window; the
    ``buckets`` field reshapes the per-project breakdown into top-N
    real + ephemeral roll-up + tail roll-up so the UI doesn't have to
    re-sort 300+ projects client-side.
    """
    if month is None:
        month = datetime.now().astimezone().strftime("%Y-%m")
    try:
        report = build_month_report(month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bucketed = bucket_for_display(report, top_n=top)

    def _p(p) -> ProjectUsageRow:
        return ProjectUsageRow(
            slug=p.slug,
            source_path=p.source_path,
            session_count=p.session_count,
            usage=_TokenUsage(**p.usage),
            cost=_TokenCost(**p.cost),
            total_tokens=p.total_tokens,
            total_cost=p.total_cost,
        )

    def _g(g) -> Optional[ProjectGroupRow]:
        if g is None:
            return None
        return ProjectGroupRow(
            label=g.label,
            project_count=g.project_count,
            session_count=g.session_count,
            usage=_TokenUsage(**g.usage),
            cost=_TokenCost(**g.cost),
            total_tokens=g.total_tokens,
            total_cost=g.total_cost,
        )

    return UsageReportResponse(
        month=report.month,
        tz_name=report.tz_name,
        window_start_utc=report.window_start_utc,
        window_end_utc=report.window_end_utc,
        project_count=report.project_count,
        session_count=report.session_count,
        grand_usage=_TokenUsage(**report.grand_usage),
        grand_cost=_TokenCost(**report.grand_cost),
        total_tokens=report.total_tokens,
        total_cost=report.total_cost,
        ephemeral_path_prefixes=list(EPHEMERAL_PATH_PREFIXES),
        ephemeral_slug_prefixes=list(EPHEMERAL_SLUG_PREFIXES),
        buckets=UsageBuckets(
            top=[_p(p) for p in bucketed.top],
            ephemeral=_g(bucketed.ephemeral),
            other=_g(bucketed.other),
        ),
        projects=[_p(p) for p in report.projects],
    )
