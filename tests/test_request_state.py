"""Per-request memoization (T24) collapses redundant per-slug lookups.

The smoking gun before this change: ``_active_session_for_project`` (which
runs ``psutil.process_iter`` indirectly via ``project_activity`` and walks
the session list) used to be called once per row inside
``_compute_session_status`` when the caller forgot to pass ``active_session_id``.
With ``RequestState``, it fires at most once per request per slug.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from unwind import registry as _reg
from unwind.api import sessions_api as _sa
from unwind.server import create_app


def _setup_multi_session(tmp_path: Path, monkeypatch, slug: str) -> tuple[str, list[str], Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    _reg.forget_slug(slug)
    proj_dir = tmp_path / ".claude" / "projects" / slug
    proj_dir.mkdir(parents=True)
    sids: list[str] = []
    for i in range(5):
        sid = f"{i:08d}-2222-3333-4444-555555555555"
        rec = {
            "uuid": f"u{i}",
            "type": "user",
            "sessionId": sid,
            "timestamp": f"2026-05-13T00:00:0{i}.000Z",
            "message": {"role": "user", "content": "hi"},
        }
        (proj_dir / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")
        sids.append(sid)
    return slug, sids, proj_dir


def test_active_session_lookup_runs_once_per_request(tmp_path, monkeypatch):
    slug, sids, _ = _setup_multi_session(tmp_path, monkeypatch, "-rs-active-once")

    calls: list[tuple] = []
    real_active = _sa._active_session_for_project

    def counting_active(index, project_path):
        calls.append((id(index), project_path))
        return real_active(index, project_path)

    monkeypatch.setattr(_sa, "_active_session_for_project", counting_active)

    app = create_app()
    with TestClient(app) as c:
        r = c.get(f"/api/projects/{slug}/sessions")
        assert r.status_code == 200, r.text
        assert len(r.json()) == len(sids)

    # Without RequestState memoization, every non-fork row's
    # _compute_session_status would call _active_session_for_project
    # again (5 rows -> 5+ extra calls). With memoization, exactly one
    # call per request — the one in list_sessions itself.
    assert len(calls) == 1, f"expected 1 active-session lookup, got {len(calls)}"


def test_request_state_isolated_per_request(tmp_path, monkeypatch):
    """A second request must trigger a fresh lookup (no cross-request leak)."""
    slug, _sids, _ = _setup_multi_session(tmp_path, monkeypatch, "-rs-isolated")

    calls: list[tuple] = []
    real_active = _sa._active_session_for_project

    def counting_active(index, project_path):
        calls.append((id(index), project_path))
        return real_active(index, project_path)

    monkeypatch.setattr(_sa, "_active_session_for_project", counting_active)

    app = create_app()
    with TestClient(app) as c:
        c.get(f"/api/projects/{slug}/sessions").raise_for_status()
        c.get(f"/api/projects/{slug}/sessions").raise_for_status()

    assert len(calls) == 2, f"expected 1 lookup per request, got {len(calls)}"


def test_get_session_uses_request_state(tmp_path, monkeypatch):
    """``GET /sessions/{id}`` previously skipped the active-session hoist and
    would recompute inside ``_compute_session_status``. RequestState still
    keeps it at one call per request."""
    slug, sids, _ = _setup_multi_session(tmp_path, monkeypatch, "-rs-get-session")

    calls: list[tuple] = []
    real_active = _sa._active_session_for_project

    def counting_active(index, project_path):
        calls.append((id(index), project_path))
        return real_active(index, project_path)

    monkeypatch.setattr(_sa, "_active_session_for_project", counting_active)

    app = create_app()
    with TestClient(app) as c:
        r = c.get(f"/api/projects/{slug}/sessions/{sids[0]}")
        assert r.status_code == 200, r.text

    assert len(calls) == 1
