"""End-to-end coverage for ``GET /api/usage``.

The router is a thin Pydantic projection over
:func:`unwind.usage_report.build_month_report`. These tests pin the
HTTP-visible contract so the React Reports view can rely on the shape
and on the error model.
"""
from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unwind.api.usage as api_usage
import unwind.usage_report as ur
from unwind.canvas_tree import CanvasTreeBuilder


def _write_session(proj_dir: Path, sid: str, events: list[tuple[str, dict]]) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (ts, usage) in enumerate(events):
        lines.append(
            json.dumps(
                {
                    "uuid": f"a-{i}",
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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Mount only the usage router and route registry lookups at a
    synthetic two-project workspace under ``tmp_path``.

    Project ``slug-real`` has one May event. Project
    ``-private-tmp-it-x`` has one May event too — so the test can
    verify both the totals and the ephemeral bucketing in one request.
    """
    real = tmp_path / "real"
    eph = tmp_path / "eph"
    _write_session(real, "s-real", [
        ("2026-05-15T12:00:00Z", {"input_tokens": 10, "output_tokens": 20,
                                   "cache_creation_input_tokens": 100,
                                   "cache_read_input_tokens": 1000}),
    ])
    _write_session(eph, "s-eph", [
        ("2026-05-15T12:00:00Z", {"input_tokens": 1, "output_tokens": 1,
                                   "cache_creation_input_tokens": 1,
                                   "cache_read_input_tokens": 1}),
    ])
    # Decouple the registered ``source_path`` (which drives ephemeral
    # classification) from the on-disk scan dir (which holds the JSONLs).
    # ``tmp_path`` itself can land under ``/tmp`` — an EPHEMERAL_PATH_PREFIX —
    # when ``TMPDIR`` is rooted there (e.g. sandboxed CI), which would
    # misclassify the "real" project as ephemeral and empty the top bucket.
    # Pin source_paths explicitly: one plainly real, one plainly ephemeral.
    source_paths = {
        "slug-real": "/Users/dev/projects/real",
        "-private-tmp-it-x": "/private/tmp/it-x",
    }
    builder_dirs = {"slug-real": real, "-private-tmp-it-x": eph}
    monkeypatch.setattr(
        ur, "list_known_projects", lambda: sorted(source_paths.items())
    )
    monkeypatch.setattr(
        ur, "canvas_tree_builder_for_slug",
        lambda slug: CanvasTreeBuilder(builder_dirs[slug]),
    )

    # Force the report's local-TZ defaulting onto UTC so the test is
    # hermetic against the CI box's clock.
    def _utc_build(month, *, tz=None):
        # ``tz`` arg required so signature matches the real function; the
        # value is intentionally ignored — this override pins the report
        # to UTC so the test doesn't depend on the CI box's local clock.
        _ = tz
        from unwind.usage_report import build_month_report as real_build
        return real_build(month, tz=timezone.utc)
    monkeypatch.setattr(api_usage, "build_month_report", _utc_build)

    app = FastAPI()
    app.include_router(api_usage.router, prefix="/api")
    return TestClient(app)


def test_usage_endpoint_returns_full_contract(client: TestClient):
    r = client.get("/api/usage?month=2026-05")
    assert r.status_code == 200
    body = r.json()
    # Top-level scalars the Reports header card consumes.
    assert body["month"] == "2026-05"
    assert body["project_count"] == 2
    assert body["session_count"] == 2
    assert body["total_tokens"] == 100 + 1000 + 10 + 20 + 1 + 1 + 1 + 1
    assert body["grand_usage"] == {
        "cw": 101, "cr": 1001, "r": 11, "w": 21,
    }
    # Buckets — ephemeral is its own row, never in top.
    top_slugs = [p["slug"] for p in body["buckets"]["top"]]
    assert top_slugs == ["slug-real"]
    assert body["buckets"]["ephemeral"]["project_count"] == 1
    assert body["buckets"]["other"] is None
    # Full projects list contains both.
    assert sorted(p["slug"] for p in body["projects"]) == [
        "-private-tmp-it-x",
        "slug-real",
    ]
    # The UI needs the prefix lists to explain bucketing to users.
    assert "/private/tmp/" in body["ephemeral_path_prefixes"]
    assert "-private-tmp-" in body["ephemeral_slug_prefixes"]


def test_usage_endpoint_rejects_malformed_month(client: TestClient):
    """Pydantic ``pattern`` validation catches bad input at the HTTP
    boundary — the rollup never runs with a partial date string."""
    r = client.get("/api/usage?month=2026-5")
    assert r.status_code == 422
    r = client.get("/api/usage?month=garbage")
    assert r.status_code == 422


def test_usage_endpoint_rejects_out_of_range_month(client: TestClient):
    """The pattern passes ``2026-13`` (two digits) — server-side
    ``ValueError`` from ``build_month_report`` must surface as a 400,
    not a 500."""
    r = client.get("/api/usage?month=2026-13")
    assert r.status_code == 400


def test_usage_endpoint_defaults_month_to_current_local(client: TestClient):
    """Omitting ``?month`` must succeed (the endpoint defaults to the
    current local month). The Reports view's initial load relies on
    this — it doesn't know what month to ask for until the user picks
    one."""
    r = client.get("/api/usage")
    assert r.status_code == 200
    body = r.json()
    # Format check: YYYY-MM, current year & month.
    assert len(body["month"]) == 7
    assert body["month"][4] == "-"
