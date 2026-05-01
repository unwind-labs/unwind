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


def test_messages_surfaces_forks_when_no_callstack_log_dir(app_client):
    """deep-rewrite-style spawn: 10 forks share parent's head uuid, project
    has no ``.claude/callstack/log/`` — the messages endpoint should still
    return them as an extra_spawns card so the canvas can render the tree."""
    client, home, projects_mod, _ = app_client

    # Build a real project dir at <tmp>/work/proj WITHOUT a callstack log
    # subdirectory (the bug case).
    real_cwd = home.parent / "work" / "proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    parent = "11111111-aaaa-bbbb-cccc-222222222222"
    forks = [f"{i:08d}-dead-beef-cafe-{i:012d}" for i in range(10)]
    _hydrate_fork_family(home, slug, parent, forks)

    # Patch the parent's first record's cwd so the registry can recover the
    # real path from a slug-only entry.
    proj_dir = home / ".claude" / "projects" / slug
    parent_path = proj_dir / f"{parent}.jsonl"
    lines = parent_path.read_text().strip().split("\n")
    head_rec = json.loads(lines[0])
    head_rec["cwd"] = str(real_cwd)
    lines[0] = json.dumps(head_rec)
    # Forks must keep the same head record verbatim — copy the cwd over too.
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
    assert len(extra) == 1, f"expected 1 extra_spawn card, got {len(extra)}"
    card = extra[0]
    assert set(card["children"]) == set(forks)
    # Each fork should have a non-empty label (its divergent user text).
    assert all(t for t in card["tasks"]), card["tasks"]
