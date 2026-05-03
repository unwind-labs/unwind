"""End-to-end coverage for the messages endpoint's spawn surfacing.

The deep-rewrite skill case: the parent session spawns several
``claude --fork-session`` subprocesses but the project has no
``.claude/callstack/log/`` directory (the call_trace lives elsewhere).
Without the fallback the canvas only renders the parent and silently
drops 10 child sessions that the fork detector clearly classifies.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _write_session(proj_dir: Path, sid: str, lines: list[dict]) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n"
    )


def _hydrate_fork_family(home: Path, slug: str, parent_sid: str, fork_sids: list[str]) -> None:
    """Write a parent + N forks all sharing the parent's head uuid.

    Each fork carries the parent's first record verbatim (mimicking what
    ``claude --fork-session`` produces), then a divergent user message that
    serves as the fork's label.
    """
    proj_dir = home / ".claude" / "projects" / slug
    head = {
        "uuid": "head-shared",
        "type": "user",
        "sessionId": parent_sid,
        "timestamp": "2026-04-24T09:00:00.000Z",
        "message": {"role": "user", "content": "shared prefix"},
    }
    _write_session(proj_dir, parent_sid, [
        head,
        {
            "uuid": "p-2",
            "type": "assistant",
            "sessionId": parent_sid,
            "timestamp": "2026-04-24T09:00:01.000Z",
            "message": {"role": "assistant", "content": "ok"},
        },
    ])

    # Fork birth times must be > parent's so the family-root selection works.
    for i, fsid in enumerate(fork_sids):
        path = proj_dir / f"{fsid}.jsonl"
        _write_session(proj_dir, fsid, [
            head,  # inherited
            {
                "uuid": f"fork-own-{i}",
                "type": "user",
                "sessionId": fsid,
                "timestamp": "2026-04-24T10:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"reviewer {i}"}],
                },
            },
        ])
        # Bump mtime / atime to ensure ordering (parent oldest).
        os.utime(path, (1700000100 + i, 1700000100 + i))
    parent_path = proj_dir / f"{parent_sid}.jsonl"
    os.utime(parent_path, (1700000000, 1700000000))


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Spin up the FastAPI app rooted at a fake $HOME so the project lives
    under our temp tree, then return a TestClient with registry state reset."""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    # Reload modules so they pick up the patched HOME at import time.
    import importlib
    import unwind.projects as projects_mod
    import unwind.registry as registry_mod
    import unwind.api.projects as api_projects_mod
    import unwind.api.sessions_api as api_sessions_mod
    importlib.reload(projects_mod)
    importlib.reload(registry_mod)
    importlib.reload(api_projects_mod)
    importlib.reload(api_sessions_mod)

    # Minimal app — just the sessions router under /api, no watcher startup.
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(api_sessions_mod.router, prefix="/api")

    client = TestClient(app)
    yield client, home, projects_mod, registry_mod
    monkeypatch.undo()


def test_messages_does_not_surface_unmarked_forks(app_client):
    """deep-rewrite-style spawn: 10 sessions share the parent's head uuid,
    project has no ``.claude/callstack/log/`` and none of the children
    carry the callstack fork prologue.

    Under the marker-only fork policy these are NOT classified as forks —
    they show up as their own top-level sessions instead of being grouped
    under the parent. This is an intentional trade-off to avoid false
    positives where independent runs happen to begin with the same first
    user message (or where ``claude --resume`` clones a parent's head)."""
    client, home, projects_mod, _ = app_client

    real_cwd = home.parent / "work" / "proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    parent = "11111111-aaaa-bbbb-cccc-222222222222"
    forks = [f"{i:08d}-dead-beef-cafe-{i:012d}" for i in range(10)]
    _hydrate_fork_family(home, slug, parent, forks)

    proj_dir = home / ".claude" / "projects" / slug
    parent_path = proj_dir / f"{parent}.jsonl"
    lines = parent_path.read_text().strip().split("\n")
    head_rec = json.loads(lines[0])
    head_rec["cwd"] = str(real_cwd)
    lines[0] = json.dumps(head_rec)
    parent_path.write_text("\n".join(lines) + "\n")
    for fsid in forks:
        fp = proj_dir / f"{fsid}.jsonl"
        fl = fp.read_text().strip().split("\n")
        fl[0] = lines[0]
        fp.write_text("\n".join(fl) + "\n")

    resp = client.get(f"/api/projects/{slug}/sessions/{parent}/messages")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    extra = body.get("extra_spawns", [])
    assert extra == [], f"expected no extra_spawn cards, got {extra}"


def test_main_session_with_completed_forks_uses_process_detection(
    app_client, monkeypatch: pytest.MonkeyPatch
):
    """A main session whose entire fork chain has completed should NOT be
    marked done if a claude process is still running for the project. This
    is the bug the user hit: callstack's terminal status overrode the live
    process detection for the user's top-level Claude Code session."""
    import yaml

    client, home, projects_mod, _ = app_client

    # Project setup: one main session, one fork session, one callstack log.
    real_cwd = home.parent / "work" / "live-proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    main_sid = "11111111-aaaa-bbbb-cccc-111111111111"
    fork_sid = "22222222-aaaa-bbbb-cccc-222222222222"

    # Write minimal session JSONLs (single user record each).
    proj_dir = home / ".claude" / "projects" / slug
    base = {
        "uuid": "u-1",
        "type": "user",
        "timestamp": "2026-04-24T09:00:00.000Z",
        "message": {"role": "user", "content": "hi"},
        "cwd": str(real_cwd),
    }
    _write_session(proj_dir, main_sid, [{**base, "sessionId": main_sid}])
    _write_session(
        proj_dir, fork_sid, [{**base, "sessionId": fork_sid, "uuid": "u-2"}]
    )

    # Callstack report: MAIN spawned FORK, FORK is complete, report itself
    # is complete. ``aggregate_status_for_session(MAIN)`` will return
    # "complete" → the fix must override that with process detection.
    cs_log = real_cwd / ".claude" / "callstack" / "log" / "20260101T000000-x"
    cs_log.mkdir(parents=True)
    (cs_log / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "20260101T000000-x",
                "parent_session": main_sid,
                "status": "complete",
                "tasks": [
                    {
                        "task": "/some-task",
                        "status": "complete",
                        "depth": 1,
                        "session_id": fork_sid,
                    }
                ],
            }
        )
    )

    # Mock process detection to claim a claude process is running for the
    # project. Without the fix, the main session would still show "done".
    import unwind.api.sessions_api as api_sessions_mod

    def fake_session_status(project_path, last_epoch):
        del project_path, last_epoch
        return "live"

    monkeypatch.setattr(
        api_sessions_mod, "session_status", fake_session_status
    )

    # ``include_forks=true`` so the fork (a callstack task) is also returned
    # — we want to assert its status independently from the main session.
    resp = client.get(f"/api/projects/{slug}/sessions?include_forks=true")
    assert resp.status_code == 200, resp.text
    rows = {r["session_id"]: r for r in resp.json()}

    # Main session: callstack says complete, but it's NOT a callstack task,
    # and process detection says live → should be live.
    assert rows[main_sid]["status"] == "live", rows[main_sid]
    # Fork: callstack says complete AND it IS a callstack task → done,
    # regardless of live process state.
    assert rows[fork_sid]["status"] == "done", rows[fork_sid]
