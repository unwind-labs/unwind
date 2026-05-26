"""Tests for the monthly cross-project token + USD usage report."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import unwind.usage_report as ur
from unwind.callstack import CallstackIndex
from unwind.canvas_tree import CanvasTreeBuilder
from unwind.usage_report import (
    EPHEMERAL_PATH_PREFIXES,
    EPHEMERAL_SLUG_PREFIXES,
    ProjectUsage,
    _month_window_utc,
    bucket_for_display,
    build_month_report,
)


# --- _month_window_utc -------------------------------------------------------


def test_month_window_is_half_open_and_uses_local_tz():
    """The window must be ``[first-of-month 00:00 local, first-of-next-month 00:00
    local)``. Both endpoints are converted to UTC; the start is inclusive,
    the end is exclusive. A timezone west of UTC pushes both endpoints
    later in UTC."""
    pst = timezone(timedelta(hours=-8))
    start_utc, end_utc, tz_name = _month_window_utc("2026-05", pst)
    assert start_utc == datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    assert end_utc == datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    assert tz_name  # informational; non-empty


def test_month_window_handles_december_year_rollover():
    utc = timezone.utc
    start_utc, end_utc, _ = _month_window_utc("2026-12", utc)
    assert start_utc == datetime(2026, 12, 1, tzinfo=utc)
    assert end_utc == datetime(2027, 1, 1, tzinfo=utc)


def test_month_window_rejects_garbage_month():
    """A bad ``--month`` value must fail loudly at the rollup boundary,
    not silently produce empty reports."""
    with pytest.raises(ValueError):
        _month_window_utc("not-a-month", timezone.utc)
    with pytest.raises(ValueError):
        _month_window_utc("2026-13", timezone.utc)
    with pytest.raises(ValueError):
        _month_window_utc("2026-5", timezone.utc)  # not zero-padded


# --- ephemeral detection -----------------------------------------------------


def test_is_ephemeral_matches_real_tmp_path():
    """A project explicitly registered with a /private/tmp/ source path
    must be flagged ephemeral."""
    p = ProjectUsage(slug="some-slug", source_path="/private/tmp/it-abc123")
    assert p.is_ephemeral


def test_is_ephemeral_matches_synthesized_tmp_slug():
    """The motivating bug: ``list_known_projects`` returns a synthesized
    ~/.claude/projects/<slug> path for projects we only discovered on
    disk (never explicitly registered). For those, the *slug* is the
    only signal of origin, so slug-prefix matching is required."""
    p = ProjectUsage(
        slug="-private-tmp-it-abc123",
        source_path="/Users/x/.claude/projects/-private-tmp-it-abc123",
    )
    assert p.is_ephemeral


def test_is_ephemeral_false_for_real_project():
    p = ProjectUsage(
        slug="-Users-amolk-work-real-project",
        source_path="/Users/amolk/work/real-project",
    )
    assert not p.is_ephemeral


def test_ephemeral_prefixes_are_publicly_documented():
    """The Reports UI needs to surface these to users (so they know what
    gets bucketed). Pinning the symbols protects against renames that
    would silently break the API."""
    assert EPHEMERAL_PATH_PREFIXES == ("/private/tmp/", "/tmp/")
    assert EPHEMERAL_SLUG_PREFIXES == ("-private-tmp-", "-tmp-")


# --- bucket_for_display ------------------------------------------------------


def _mk(slug: str, source_path: str, total_cost: float) -> ProjectUsage:
    p = ProjectUsage(slug=slug, source_path=source_path, session_count=1)
    # Distribute the cost across the four buckets so totals are non-trivial.
    p.cost["cw"] = total_cost
    p.usage["cw"] = int(total_cost * 1_000_000)
    return p


def test_bucket_excludes_ephemerals_from_top_n():
    """Ephemeral projects must never compete for top-N slots even if
    they outspend real projects — otherwise the headline breakdown gets
    drowned in tmp scaffolds."""
    projects = [
        _mk("-private-tmp-it-1", "/private/tmp/it-1", 1000.0),  # ephemeral big spender
        _mk("real-a", "/Users/x/work/a", 100.0),
        _mk("real-b", "/Users/x/work/b", 50.0),
    ]
    report = ur.UsageReport(
        month="2026-05",
        tz_name="UTC",
        window_start_utc=datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 6, 1, tzinfo=timezone.utc),
        project_count=3,
        session_count=3,
        grand_usage={"cw": 1_150_000_000, "cr": 0, "r": 0, "w": 0},
        grand_cost={"cw": 1150.0, "cr": 0.0, "r": 0.0, "w": 0.0},
        projects=projects,
    )
    b = bucket_for_display(report, top_n=10)
    assert [p.slug for p in b.top] == ["real-a", "real-b"]
    assert b.ephemeral is not None
    assert b.ephemeral.project_count == 1
    assert b.ephemeral.total_cost == 1000.0
    assert b.other is None  # nothing left over


def test_bucket_tail_appears_when_real_projects_exceed_top_n():
    projects = [
        _mk(f"real-{i}", f"/Users/x/work/{i}", 100 - i) for i in range(5)
    ]  # 100, 99, 98, 97, 96 — already sorted
    report = ur.UsageReport(
        month="2026-05",
        tz_name="UTC",
        window_start_utc=datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 6, 1, tzinfo=timezone.utc),
        project_count=5,
        session_count=5,
        grand_usage={"cw": 0, "cr": 0, "r": 0, "w": 0},
        grand_cost={"cw": 490.0, "cr": 0.0, "r": 0.0, "w": 0.0},
        projects=projects,
    )
    b = bucket_for_display(report, top_n=2)
    assert len(b.top) == 2
    assert b.other is not None
    assert b.other.project_count == 3
    assert b.other.total_cost == 98 + 97 + 96  # 291
    assert b.ephemeral is None


def test_bucket_empty_report_produces_no_groups():
    report = ur.UsageReport(
        month="2026-05",
        tz_name="UTC",
        window_start_utc=datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 6, 1, tzinfo=timezone.utc),
        project_count=0,
        session_count=0,
        grand_usage={"cw": 0, "cr": 0, "r": 0, "w": 0},
        grand_cost={"cw": 0.0, "cr": 0.0, "r": 0.0, "w": 0.0},
        projects=[],
    )
    b = bucket_for_display(report, top_n=10)
    assert b.top == []
    assert b.ephemeral is None
    assert b.other is None


# --- build_month_report end-to-end ------------------------------------------


def _write_session(
    proj_dir: Path,
    sid: str,
    events: list[tuple[str, dict]],
    *,
    uuid_prefix: str | None = None,
) -> None:
    """Synthesize a JSONL with one assistant ``usage`` block per event.

    Each ``(timestamp, usage)`` becomes one assistant turn, which the
    scanner will surface as one :class:`UsageEvent`. That's the unit
    ``build_month_report`` filters on.

    ``uuid_prefix`` lets fork tests share uuids between parent and
    fork (``--fork-session`` copies the parent's transcript including
    each record's uuid). Defaults to ``sid``-namespaced uuids so
    independent sessions never collide.
    """
    proj_dir.mkdir(parents=True, exist_ok=True)
    prefix = uuid_prefix if uuid_prefix is not None else f"{sid}-a"
    lines = []
    for i, (ts, usage) in enumerate(events):
        lines.append(
            json.dumps(
                {
                    "uuid": f"{prefix}-{i}",
                    "type": "assistant",
                    "sessionId": sid,
                    "timestamp": ts,
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet-4",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": usage,
                    },
                }
            )
        )
    (proj_dir / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")


def _stub_registry(
    monkeypatch: pytest.MonkeyPatch,
    projects: dict[str, Path],
    callstack_log_dirs: dict[str, Path] | None = None,
) -> None:
    """Redirect ``usage_report``'s registry hooks at an in-memory project
    set. Avoids the HOME-env / module-reload dance the other tests use,
    so this test stays hermetic without leaking into the registry cache.

    ``callstack_log_dirs`` lets a test wire a real callstack log dir
    per slug (the fork-inheritance filter needs the parent_chain).
    Slugs without an entry get an empty (no-logs) CallstackIndex so
    ``inherited_uuids_for`` reports nothing.
    """
    monkeypatch.setattr(
        ur, "list_known_projects", lambda: sorted(projects.items())
    )
    monkeypatch.setattr(
        ur,
        "canvas_tree_builder_for_slug",
        lambda slug: CanvasTreeBuilder(projects[slug]),
    )
    cs_dirs = callstack_log_dirs or {}
    monkeypatch.setattr(
        ur,
        "callstack_for_slug",
        lambda slug: CallstackIndex(
            cs_dirs.get(slug, projects[slug] / ".no-callstack")
        ),
    )


def test_build_month_report_filters_events_by_local_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An event whose UTC timestamp falls inside the local-month window
    counts; an event one second before the start or at/after the end
    does not. The window is half-open on the *local* clock, so the UTC
    boundaries shift with the test's timezone."""
    proj = tmp_path / "proj-real"
    sid = "s1"
    _write_session(
        proj,
        sid,
        [
            # In window: 2026-05-15 noon UTC, clearly inside May regardless of TZ.
            ("2026-05-15T12:00:00Z", {"input_tokens": 10, "output_tokens": 20,
                                       "cache_creation_input_tokens": 100,
                                       "cache_read_input_tokens": 1000}),
            # Outside window: 2026-04-30 noon UTC, clearly April everywhere.
            ("2026-04-30T12:00:00Z", {"input_tokens": 1, "output_tokens": 2,
                                       "cache_creation_input_tokens": 3,
                                       "cache_read_input_tokens": 4}),
            # Outside window: 2026-06-15 noon UTC, clearly June.
            ("2026-06-15T12:00:00Z", {"input_tokens": 5, "output_tokens": 5,
                                       "cache_creation_input_tokens": 5,
                                       "cache_read_input_tokens": 5}),
        ],
    )
    _stub_registry(monkeypatch, {"slug-real": proj})

    report = build_month_report("2026-05", tz=timezone.utc)
    assert report.session_count == 1
    assert report.project_count == 1
    # Only the May event's tokens land in the totals.
    assert report.grand_usage == {"cw": 100, "cr": 1000, "r": 10, "w": 20}
    # And cost is non-zero (pricing module priced it) — exact value
    # depends on the model rate table, so just check the structure.
    assert report.grand_cost["cw"] > 0
    assert report.grand_cost["cr"] > 0


def test_build_month_report_no_double_counting_across_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each ``UsageEvent`` lives in exactly one JSONL — the session that
    produced it. Summing across projects must equal the per-project
    sum; subagent / call-child sessions show up under their own slug,
    never inherited from the caller."""
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    _write_session(proj_a, "s-a", [
        ("2026-05-10T00:00:00Z", {"input_tokens": 7, "output_tokens": 0,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0}),
    ])
    _write_session(proj_b, "s-b", [
        ("2026-05-11T00:00:00Z", {"input_tokens": 13, "output_tokens": 0,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0}),
    ])
    _stub_registry(monkeypatch, {"slug-a": proj_a, "slug-b": proj_b})

    report = build_month_report("2026-05", tz=timezone.utc)
    per_project = {p.slug: p.usage["r"] for p in report.projects}
    assert per_project == {"slug-a": 7, "slug-b": 13}
    assert report.grand_usage["r"] == 20  # exactly once each


def test_build_month_report_excludes_records_inherited_from_fork_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``claude --fork-session`` mirrors the parent's JSONL into the
    child, including every assistant ``message.usage`` block (same
    uuid). Without filtering, the parent's prefix tokens get counted
    once in the parent and again in every fork — N forks of one parent
    inflate by N+1. Verify each token is counted exactly once.
    """
    proj = tmp_path / "proj"
    # Parent has two assistant turns with usage.
    _write_session(
        proj,
        "PARENT",
        [
            ("2026-05-10T00:00:00Z", {
                "input_tokens": 1, "output_tokens": 2,
                "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50,
            }),
            ("2026-05-10T00:00:01Z", {
                "input_tokens": 3, "output_tokens": 4,
                "cache_creation_input_tokens": 200, "cache_read_input_tokens": 80,
            }),
        ],
        uuid_prefix="p",
    )
    # Fork inherits the parent's two turns verbatim (same uuids), then
    # adds one new post-fork turn. Splitting the JSONL by hand instead
    # of via ``_write_session`` so the inherited records keep their
    # ``p-…`` uuids and the new one has a distinct ``f-…`` uuid.
    fork_records = [
        {
            "uuid": "p-0",
            "type": "assistant",
            "sessionId": "PARENT",
            "timestamp": "2026-05-10T00:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 1, "output_tokens": 2,
                    "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50,
                },
            },
        },
        {
            "uuid": "p-1",
            "type": "assistant",
            "sessionId": "PARENT",
            "timestamp": "2026-05-10T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 3, "output_tokens": 4,
                    "cache_creation_input_tokens": 200, "cache_read_input_tokens": 80,
                },
            },
        },
        {
            "uuid": "f-0",
            "type": "assistant",
            "sessionId": "FORK",
            "timestamp": "2026-05-10T00:00:10Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 7, "output_tokens": 9,
                    "cache_creation_input_tokens": 11, "cache_read_input_tokens": 13,
                },
            },
        },
    ]
    (proj / "FORK.jsonl").write_text("\n".join(json.dumps(r) for r in fork_records) + "\n")
    # A callstack report tying FORK to PARENT — that's how
    # ``inherited_uuids_for`` discovers the parent chain.
    log = tmp_path / "log"
    invoke_dir = log / "i0"
    invoke_dir.mkdir(parents=True, exist_ok=True)
    (invoke_dir / "report.yaml").write_text(
        yaml.safe_dump({
            "invoke_id": "i0",
            "kind": "call",
            "parent_session": "PARENT",
            "started_at": "2026-05-10T00:00:05+00:00",
            "ended_at": "2026-05-10T00:00:20+00:00",
            "status": "complete",
            "tasks": [{
                "task": "/task-x",
                "status": "complete",
                "depth": 1,
                "session_id": "FORK",
            }],
        })
    )

    _stub_registry(monkeypatch, {"slug": proj}, callstack_log_dirs={"slug": log})

    report = build_month_report("2026-05", tz=timezone.utc)
    # Parent's two turns + fork's one new turn, each counted once.
    # Inherited (p-0, p-1) inside FORK.jsonl must NOT add to totals.
    assert report.grand_usage == {
        "cw": 100 + 200 + 11,
        "cr": 50 + 80 + 13,
        "r": 1 + 3 + 7,
        "w": 2 + 4 + 9,
    }
    # Both sessions still register as "had an event" — the fork
    # contributes its new turn, the parent contributes both turns.
    assert report.session_count == 2


def test_build_month_report_drops_projects_with_no_in_window_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A project with sessions but no May activity must not appear at
    all — otherwise the breakdown shows empty rows and the project
    count is misleading."""
    proj_active = tmp_path / "active"
    proj_quiet = tmp_path / "quiet"
    _write_session(proj_active, "s1", [
        ("2026-05-10T00:00:00Z", {"input_tokens": 1, "output_tokens": 1,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0}),
    ])
    _write_session(proj_quiet, "s2", [
        ("2026-03-10T00:00:00Z", {"input_tokens": 99, "output_tokens": 99,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0}),
    ])
    _stub_registry(monkeypatch, {"slug-active": proj_active, "slug-quiet": proj_quiet})

    report = build_month_report("2026-05", tz=timezone.utc)
    assert [p.slug for p in report.projects] == ["slug-active"]
    assert report.project_count == 1
