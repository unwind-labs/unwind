"""Tests for the centralized ``project_jsonl_listing`` helper.

Multiple consumers (``SessionIndex.list_sessions``, ``_project_jsonl_signature``,
``last_activity_for``, ``compute_invoke_index_for_project``,
``ForkDetector._refresh``, and the watcher) used to each glob the project
directory independently. The helper collapses repeat scans within a request
into a single ``os.scandir`` pass via a short TTL cache.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_listing_caches_repeat_calls(tmp_path: Path):
    """Back-to-back calls within the TTL share one ``os.scandir`` pass."""
    from unwind.projects import invalidate_jsonl_listing, project_jsonl_listing

    _write_jsonl(tmp_path / "a.jsonl", [{"u": "x"}])
    _write_jsonl(tmp_path / "b.jsonl", [{"u": "y"}])
    invalidate_jsonl_listing(tmp_path)

    scan_calls = {"n": 0}
    real_scandir = os.scandir

    def counting(path):  # noqa: ANN001
        if Path(path) == tmp_path:
            scan_calls["n"] += 1
        return real_scandir(path)

    import unwind.projects as projects_mod

    original = projects_mod.os.scandir
    projects_mod.os.scandir = counting  # type: ignore[assignment]
    try:
        first = project_jsonl_listing(tmp_path)
        second = project_jsonl_listing(tmp_path)
        third = project_jsonl_listing(tmp_path)
    finally:
        projects_mod.os.scandir = original  # type: ignore[assignment]

    assert scan_calls["n"] == 1
    assert first == second == third
    assert {e.sid for e in first} == {"a", "b"}


def test_listing_fresh_bypasses_cache(tmp_path: Path):
    """``fresh=True`` callers (HTTP ETag) must see in-place file changes
    that don't bump the directory mtime."""
    from unwind.projects import invalidate_jsonl_listing, project_jsonl_listing

    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [{"u": "x"}])
    invalidate_jsonl_listing(tmp_path)

    first = project_jsonl_listing(tmp_path)
    assert len(first) == 1
    initial_size = first[0].size

    with p.open("a") as fh:
        fh.write(json.dumps({"u": "y"}) + "\n")
    future = first[0].mtime + 1
    os.utime(p, (future, future))

    cached = project_jsonl_listing(tmp_path)  # within TTL, dir mtime unchanged
    assert cached[0].size == initial_size

    refreshed = project_jsonl_listing(tmp_path, fresh=True)
    assert refreshed[0].size > initial_size
    assert refreshed[0].mtime == future


@pytest.fixture
def sessions_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    import unwind.projects as projects_mod
    import unwind.registry as registry_mod
    import unwind.api.projects as api_projects_mod
    import unwind.api.sessions_api as api_sessions_mod

    importlib.reload(projects_mod)
    importlib.reload(registry_mod)
    importlib.reload(api_projects_mod)
    importlib.reload(api_sessions_mod)

    app = FastAPI()
    app.include_router(api_sessions_mod.router, prefix="/api")

    yield TestClient(app), home, projects_mod, registry_mod


def test_list_sessions_endpoint_shares_one_scan(
    sessions_client, monkeypatch: pytest.MonkeyPatch
):
    """The /sessions endpoint must scan the project_dir at most twice per
    request: once via the cached listing (shared by SessionIndex.list_sessions,
    ForkDetector, and any spawn-resolver helpers) and once for the fresh
    signature path. Anything more is a regression to the pre-T15 state where
    five consumers each globbed independently."""
    client, home, projects_mod, _ = sessions_client

    slug = "-Users-me-proj-a"
    real_cwd = "/Users/me/proj-a"
    proj = home / ".claude" / "projects" / slug
    for i in range(3):
        _write_jsonl(
            proj / f"sess-{i}.jsonl",
            [{"type": "user", "cwd": real_cwd, "uuid": f"u{i}",
              "timestamp": f"2026-04-24T09:0{i}:00.000Z",
              "message": {"role": "user", "content": f"m{i}"}}],
        )

    # Warm: the very first call will trigger one-time discovery side effects
    # (auto-register-default, synthetic-slug upgrade peek). We measure the
    # second call, which is the steady-state shape.
    client.get(f"/api/projects/{slug}/sessions")

    scan_calls: list[Path] = []
    real_scandir = os.scandir

    def counting(path):  # noqa: ANN001
        if Path(path) == proj:
            scan_calls.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(projects_mod.os, "scandir", counting)
    # Bust the TTL cache so the first measured call has to scan at least once.
    projects_mod.invalidate_jsonl_listing(proj)

    r = client.get(f"/api/projects/{slug}/sessions")
    assert r.status_code == 200

    # Today we expect: 1 fresh signature scan (ETag-style path via
    # project_state_signature) + 1 cached listing scan shared by everyone
    # else. Cap at 3 to absorb any small future churn but flag a true
    # regression to the old ~5-consumer pattern.
    assert len(scan_calls) <= 3, (
        f"/sessions made {len(scan_calls)} scans of {proj}; "
        "expected <=3 (one cached + one fresh-signature)"
    )
