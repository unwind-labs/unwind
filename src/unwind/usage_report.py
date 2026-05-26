"""Calendar-month token usage + USD cost rollup across every known project.

Reuses the existing per-session ``UsageEvent`` stream (one event per
assistant turn, tagged with the wire-format ``timestamp`` and ``model``)
and ``pricing.cost_usd``. Filters by an inclusive-exclusive UTC window
derived from a local-time calendar month, so a turn that fires at
2026-05-01T03:00Z (= Apr 30 20:00 PT) lands in April, not May.

Each ``UsageEvent`` lives in exactly one JSONL file — the session that
produced it — so summing across every JSONL counts each token exactly
once. Subagent sessions and callstack child sessions each contribute
their own events; no rollup-style double counting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Optional

from .jsonl import collect_uuids
from .pricing import cost_usd
from .projects import project_jsonl_listing
from .registry import (
    callstack_for_slug,
    canvas_tree_builder_for_slug,
    list_known_projects,
)

# A project whose ``source_path`` starts with one of these prefixes is
# treated as a throwaway integration-test scaffold rather than a real
# project — the Reports UI buckets them under a synthetic
# "Ephemeral test runs" row instead of crowding the top-N list.
EPHEMERAL_PATH_PREFIXES: tuple[str, ...] = ("/private/tmp/", "/tmp/")

# Claude Code's project slug is the source path with ``/`` replaced by
# ``-``. So ``/private/tmp/foo`` slugifies to ``-private-tmp-foo``. We
# match on slug too because :func:`registry.list_known_projects` returns
# synthesized ``~/.claude/projects/<slug>`` paths for projects we only
# discovered on disk (i.e. were never explicitly registered with their
# real source path) — the path-prefix check alone misses those.
EPHEMERAL_SLUG_PREFIXES: tuple[str, ...] = ("-private-tmp-", "-tmp-")

TOKEN_KEYS: tuple[str, ...] = ("cw", "cr", "r", "w")


def _zero_usage() -> dict[str, int]:
    return {k: 0 for k in TOKEN_KEYS}


def _zero_cost() -> dict[str, float]:
    return {k: 0.0 for k in TOKEN_KEYS}


@dataclass
class ProjectUsage:
    """One project's totals for the report window."""

    slug: str
    source_path: str
    session_count: int = 0
    usage: dict[str, int] = field(default_factory=_zero_usage)
    cost: dict[str, float] = field(default_factory=_zero_cost)

    @property
    def total_tokens(self) -> int:
        return sum(self.usage.values())

    @property
    def total_cost(self) -> float:
        return sum(self.cost.values())

    @property
    def is_ephemeral(self) -> bool:
        return self.source_path.startswith(EPHEMERAL_PATH_PREFIXES) or self.slug.startswith(
            EPHEMERAL_SLUG_PREFIXES
        )


@dataclass
class UsageReport:
    """Result of :func:`build_month_report`."""

    month: str  # "YYYY-MM"
    tz_name: str
    window_start_utc: datetime  # inclusive
    window_end_utc: datetime  # exclusive
    project_count: int
    session_count: int
    grand_usage: dict[str, int]
    grand_cost: dict[str, float]
    projects: list[ProjectUsage]  # sorted by total_cost desc

    @property
    def total_tokens(self) -> int:
        return sum(self.grand_usage.values())

    @property
    def total_cost(self) -> float:
        return sum(self.grand_cost.values())


def _local_tz() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None  # astimezone() always returns an aware datetime
    return tz


def _month_window_utc(
    month: str, tz: Optional[tzinfo]
) -> tuple[datetime, datetime, str]:
    """Translate a ``YYYY-MM`` label + timezone into a UTC half-open window
    ``[start, end)`` covering exactly that local calendar month.

    Returned ``tz_name`` is informational (UI footer / JSON metadata).
    """
    parts = month.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"month must be 'YYYY-MM' (got {month!r})")
    year, mon = int(parts[0]), int(parts[1])
    if not (1 <= mon <= 12):
        raise ValueError(f"month {mon} out of range in {month!r}")
    if tz is None:
        tz = _local_tz()
    start_local = datetime(year, mon, 1, 0, 0, 0, tzinfo=tz)
    if mon == 12:
        end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_local = datetime(year, mon + 1, 1, 0, 0, 0, tzinfo=tz)
    tz_name = tz.tzname(start_local) or str(tz)
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
        tz_name,
    )


def build_month_report(
    month: str, *, tz: Optional[tzinfo] = None
) -> UsageReport:
    """Build a token + USD cost report for the local calendar month
    ``month`` (``"YYYY-MM"``).

    Walks every known/discoverable project; for each, walks every JSONL,
    reuses the cached ``SessionScan.usage_events``, and sums those whose
    UTC timestamp falls inside the local month window. ``pricing.cost_usd``
    prices each event with its recording model — same per-model rates as
    the canvas.

    Projects with zero in-window events are omitted from
    :attr:`UsageReport.projects`.
    """
    start_utc, end_utc, tz_name = _month_window_utc(month, tz)

    rows: list[ProjectUsage] = []
    for slug, source_path in list_known_projects():
        builder = canvas_tree_builder_for_slug(slug)
        ci = callstack_for_slug(slug)
        # Per-session "uuids inherited from callstack ancestors". Lazy:
        # only forks (sessions with a non-empty parent chain) need it,
        # and the underlying ``collect_uuids`` is mtime-cached, so
        # repeated lookups for sibling forks of the same parent share
        # work. Without this filter, every fork double-counts the
        # parent's prefix tokens (``--fork-session`` mirrors the parent
        # transcript verbatim into the child JSONL).
        inherited_cache: dict[str, set[str]] = {}

        def _inherited(sid: str) -> set[str]:
            cached = inherited_cache.get(sid)
            if cached is not None:
                return cached
            chain = ci.parent_chain(sid) if ci.has_logs else []
            out: set[str] = set()
            for ancestor_id in chain:
                anc_path = builder.project_dir / f"{ancestor_id}.jsonl"
                if anc_path.is_file():
                    out |= collect_uuids(anc_path)
            inherited_cache[sid] = out
            return out

        row = ProjectUsage(slug=slug, source_path=str(source_path))
        for entry in project_jsonl_listing(builder.project_dir):
            scan = builder.get_scan(entry.sid)
            inherited = _inherited(entry.sid)
            session_had_event = False
            for ev in scan.usage_events:
                if ev.ts is None or ev.ts < start_utc or ev.ts >= end_utc:
                    continue
                if ev.uuid is not None and ev.uuid in inherited:
                    continue
                session_had_event = True
                row.usage["cw"] += ev.cw
                row.usage["cr"] += ev.cr
                row.usage["r"] += ev.r
                row.usage["w"] += ev.w
                c = cost_usd(ev.model, ev.cw, ev.cr, ev.r, ev.w)
                for k in TOKEN_KEYS:
                    row.cost[k] += c[k]
            if session_had_event:
                row.session_count += 1
        if row.session_count:
            rows.append(row)

    rows.sort(key=lambda r: r.total_cost, reverse=True)
    grand_usage = {k: sum(r.usage[k] for r in rows) for k in TOKEN_KEYS}
    grand_cost = {k: sum(r.cost[k] for r in rows) for k in TOKEN_KEYS}

    return UsageReport(
        month=month,
        tz_name=tz_name,
        window_start_utc=start_utc,
        window_end_utc=end_utc,
        project_count=len(rows),
        session_count=sum(r.session_count for r in rows),
        grand_usage=grand_usage,
        grand_cost=grand_cost,
        projects=rows,
    )


@dataclass
class ProjectGroup:
    """A bucket of projects rolled into one row in the per-project breakdown."""

    label: str
    project_count: int
    session_count: int
    usage: dict[str, int]
    cost: dict[str, float]

    @property
    def total_tokens(self) -> int:
        return sum(self.usage.values())

    @property
    def total_cost(self) -> float:
        return sum(self.cost.values())


@dataclass
class BucketedReport:
    """A :class:`UsageReport` reshaped for display: top-N real projects + a
    rolled-up ephemeral bucket + a rolled-up tail bucket. The grand totals
    on the underlying :class:`UsageReport` still sum *everything* — the
    buckets are only for the per-project breakdown.
    """

    report: UsageReport
    top: list[ProjectUsage]
    ephemeral: Optional[ProjectGroup]
    other: Optional[ProjectGroup]


def bucket_for_display(report: UsageReport, *, top_n: int = 20) -> BucketedReport:
    """Split ``report.projects`` into top-N real projects + ephemeral roll-up
    + tail roll-up. Ephemerals are removed from the top-N race entirely so a
    long-tail of tmp scaffolds can't crowd out real work.
    """
    real = [p for p in report.projects if not p.is_ephemeral]
    ephemerals = [p for p in report.projects if p.is_ephemeral]
    top = real[:top_n]
    tail = real[top_n:]

    def _group(label: str, ps: list[ProjectUsage]) -> Optional[ProjectGroup]:
        if not ps:
            return None
        u = _zero_usage()
        c = _zero_cost()
        for p in ps:
            for k in TOKEN_KEYS:
                u[k] += p.usage[k]
                c[k] += p.cost[k]
        return ProjectGroup(
            label=label,
            project_count=len(ps),
            session_count=sum(p.session_count for p in ps),
            usage=u,
            cost=c,
        )

    return BucketedReport(
        report=report,
        top=top,
        ephemeral=_group(f"Ephemeral test runs ({len(ephemerals)} projects)", ephemerals),
        other=_group(f"Other ({len(tail)} projects)", tail),
    )
