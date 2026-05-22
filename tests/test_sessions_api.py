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


def test_messages_extras_emit_one_card_per_callstack_invocation(app_client):
    """When a parent calls the same child via callstack three times (three
    ``report.yaml`` files), the messages endpoint must surface three
    ``extra_spawns`` cards — one per invocation, each with its own
    ``invoke_id`` and ``started_at`` — so the canvas can render a
    distinct child node per invocation under each parent window.
    """
    import yaml

    client, home, projects_mod, _ = app_client

    real_cwd = home.parent / "work" / "callstack-proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    main_sid = "11111111-aaaa-bbbb-cccc-111111111111"
    parent_sid = "22222222-aaaa-bbbb-cccc-222222222222"
    child_sid = "33333333-aaaa-bbbb-cccc-333333333333"

    proj_dir = home / ".claude" / "projects" / slug
    base = {
        "uuid": "u-1",
        "type": "user",
        "timestamp": "2026-05-03T19:00:00.000Z",
        "message": {"role": "user", "content": "hi"},
        "cwd": str(real_cwd),
    }
    # parent_sid's JSONL has NO references to child_sid — the spawn
    # happened via the callstack Skill, not an MCP tool_use, so it lives
    # only in the report.yaml files.
    _write_session(proj_dir, main_sid, [{**base, "sessionId": main_sid}])
    _write_session(
        proj_dir, parent_sid, [{**base, "sessionId": parent_sid, "uuid": "u-2"}]
    )
    _write_session(
        proj_dir, child_sid, [{**base, "sessionId": child_sid, "uuid": "u-3"}]
    )

    # Three reports recording parent → child, at three different times.
    log_root = real_cwd / ".claude" / "callstack" / "log"
    timestamps = [
        "2026-05-03T19:55:42+00:00",
        "2026-05-03T21:09:00+00:00",
        "2026-05-03T21:09:40+00:00",
    ]
    for i, ts in enumerate(timestamps):
        invoke_id = f"20260503T19554{i}-r{i}"
        d = log_root / invoke_id
        d.mkdir(parents=True)
        (d / "report.yaml").write_text(
            yaml.safe_dump(
                {
                    "invoke_id": invoke_id,
                    "parent_session": main_sid,
                    "status": "complete",
                    "started_at": ts,
                    "ended_at": ts,
                    "tasks": [
                        {
                            "task": "/verify-mfa",
                            "status": "complete",
                            "depth": 1,
                            "session_id": parent_sid,
                            "children": [
                                {
                                    "task": "/check-code-expiry",
                                    "status": "complete",
                                    "depth": 2,
                                    "session_id": child_sid,
                                }
                            ],
                        }
                    ],
                }
            )
        )

    resp = client.get(f"/api/projects/{slug}/sessions/{parent_sid}/messages")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    extras = body.get("extra_spawns", [])
    assert len(extras) == 3, f"expected 3 extra_spawns, got {len(extras)}: {extras}"
    # Each card has exactly one child = the same session_id.
    assert all(card["children"] == [child_sid] for card in extras)
    # Each card has its own invoke_id and started_at.
    invoke_ids = {card["invoke_id"] for card in extras}
    started = {card["started_at"] for card in extras}
    assert len(invoke_ids) == 3, invoke_ids
    assert len(started) == 3, started


def test_active_main_session_with_completed_forks_is_live(
    app_client, monkeypatch: pytest.MonkeyPatch
):
    """An ACTIVELY-USED main session whose entire fork chain has completed
    should still show ``live`` — the callstack chain finishing doesn't
    mean the user's main session is done. The bug this guards against:
    callstack's aggregate "complete" status overriding the live signal.

    Liveness requires both a live claude process AND THIS session's
    own JSONL touched in the last 5 minutes. The test mocks the
    process check (since pytest doesn't have a real claude running)
    and gives the main session a fresh JSONL timestamp."""
    import time as _time
    import yaml

    import unwind.api.sessions_api as api_sessions_mod
    from unwind.processes import ProjectActivity

    def fake_project_activity(_path: str) -> ProjectActivity:
        return ProjectActivity(
            claude_running=True, pid_count=1, sampled_at=_time.time()
        )

    monkeypatch.setattr(
        api_sessions_mod, "project_activity", fake_project_activity
    )

    client, home, projects_mod, _ = app_client

    real_cwd = home.parent / "work" / "live-proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    main_sid = "11111111-aaaa-bbbb-cccc-111111111111"
    fork_sid = "22222222-aaaa-bbbb-cccc-222222222222"

    proj_dir = home / ".claude" / "projects" / slug
    fresh_iso = (
        _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(_time.time())) + ".000Z"
    )
    _write_session(
        proj_dir,
        main_sid,
        [
            {
                "uuid": "u-1",
                "type": "user",
                "timestamp": fresh_iso,
                "message": {"role": "user", "content": "hi"},
                "cwd": str(real_cwd),
                "sessionId": main_sid,
            }
        ],
    )
    _write_session(
        proj_dir,
        fork_sid,
        [
            {
                "uuid": "u-2",
                "type": "user",
                "timestamp": "2026-04-24T09:00:00.000Z",
                "message": {"role": "user", "content": "hi"},
                "cwd": str(real_cwd),
                "sessionId": fork_sid,
            }
        ],
    )

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

    resp = client.get(f"/api/projects/{slug}/sessions?include_forks=true")
    assert resp.status_code == 200, resp.text
    rows = {r["session_id"]: r for r in resp.json()}

    assert rows[main_sid]["status"] == "live", rows[main_sid]
    assert rows[fork_sid]["status"] == "done", rows[fork_sid]


def test_canvas_running_fork_child_window_is_live(app_client):
    """Regression: the canvas endpoint's ``is_live`` must recognize a
    callstack-fork child whose aggregate status is still ``running`` —
    otherwise the parent's CALL row (driven by the callstack aggregate)
    shows ``in_progress`` while the child window collapses to ``done``,
    producing the divergent status the user reported."""
    import yaml

    client, home, projects_mod, _ = app_client

    real_cwd = home.parent / "work" / "running-fork-proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    main_sid = "11111111-cccc-dddd-eeee-111111111111"
    child_sid = "22222222-cccc-dddd-eeee-222222222222"

    proj_dir = home / ".claude" / "projects" / slug
    base = {
        "timestamp": "2026-05-03T19:00:00.000Z",
        "message": {"role": "user", "content": "hi"},
        "cwd": str(real_cwd),
    }
    _write_session(
        proj_dir,
        main_sid,
        [{**base, "uuid": "u-1", "type": "user", "sessionId": main_sid}],
    )
    _write_session(
        proj_dir,
        child_sid,
        [{**base, "uuid": "u-2", "type": "user", "sessionId": child_sid}],
    )

    cs_log = real_cwd / ".claude" / "callstack" / "log" / "20260503T190000-x"
    cs_log.mkdir(parents=True)
    (cs_log / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "20260503T190000-x",
                "parent_session": main_sid,
                "status": "running",
                "tasks": [
                    {
                        "task": "Execute Phase 3",
                        "status": "running",
                        "depth": 1,
                        "session_id": child_sid,
                    }
                ],
            }
        )
    )

    resp = client.get(f"/api/projects/{slug}/sessions/{main_sid}/canvas")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    child_windows = [w for w in body["all_windows"] if w["session_id"] == child_sid]
    assert child_windows, f"no window for child {child_sid} in {body}"
    # Final window of the still-running fork child must be live, not done.
    assert child_windows[-1]["status"] == "live", child_windows[-1]


def test_canvas_returned_fork_child_window_is_done_despite_stale_report(app_client):
    """Regression: callstack ``report.yaml`` sometimes records a child
    task as ``running`` even after the child emitted a
    ``{"op":"return"}`` envelope (runtime fails to update the report).

    The canvas's ``is_live`` must consult the child's JSONL for a
    trailing RETURN envelope and downgrade to ``done`` — otherwise the
    parent's CALL row appears stuck ``in_progress`` indefinitely.
    """
    import yaml

    client, home, projects_mod, _ = app_client

    real_cwd = home.parent / "work" / "returned-fork-proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    main_sid = "33333333-cccc-dddd-eeee-333333333333"
    child_sid = "44444444-cccc-dddd-eeee-444444444444"

    proj_dir = home / ".claude" / "projects" / slug
    _write_session(
        proj_dir,
        main_sid,
        [
            {
                "uuid": "u-1",
                "type": "user",
                "sessionId": main_sid,
                "timestamp": "2026-05-03T19:00:00.000Z",
                "message": {"role": "user", "content": "hi"},
                "cwd": str(real_cwd),
            }
        ],
    )
    # Child JSONL whose last assistant message contains a RETURN envelope.
    _write_session(
        proj_dir,
        child_sid,
        [
            {
                "uuid": "u-2",
                "type": "user",
                "sessionId": child_sid,
                "timestamp": "2026-05-03T19:00:00.000Z",
                "message": {"role": "user", "content": "go"},
                "cwd": str(real_cwd),
            },
            {
                "uuid": "a-1",
                "type": "assistant",
                "sessionId": child_sid,
                "timestamp": "2026-05-03T19:00:05.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": '```json\n{"op": "return", "result": "ok"}\n```',
                        }
                    ],
                },
            },
        ],
    )

    # report.yaml lies: it still says the child is running.
    cs_log = real_cwd / ".claude" / "callstack" / "log" / "20260503T190000-y"
    cs_log.mkdir(parents=True)
    (cs_log / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "20260503T190000-y",
                "parent_session": main_sid,
                "status": "mixed",
                "tasks": [
                    {
                        "task": "Execute Phase 3",
                        "status": "running",
                        "depth": 1,
                        "session_id": child_sid,
                    }
                ],
            }
        )
    )

    resp = client.get(f"/api/projects/{slug}/sessions/{main_sid}/canvas")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    child_windows = [w for w in body["all_windows"] if w["session_id"] == child_sid]
    assert child_windows, f"no window for child {child_sid} in {body}"
    # The RETURN envelope in the JSONL overrides the stale ``running``
    # status in report.yaml.
    assert child_windows[-1]["status"] == "done", child_windows[-1]


def test_messages_since_uuid_returns_only_delta(app_client):
    """``GET /messages?since_uuid=<u>`` returns messages that landed after
    the record with that uuid. ``file_offset`` and ``last_uuid`` continue
    to reflect the FULL file so the client can keep tailing."""
    client, home, projects_mod, _ = app_client

    real_cwd = home.parent / "work" / "delta-proj"
    real_cwd.mkdir(parents=True)
    slug = projects_mod.slug_for(real_cwd)

    sid = "33333333-aaaa-bbbb-cccc-333333333333"
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(
        proj_dir,
        sid,
        [
            {
                "uuid": "u-1",
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-04-24T09:00:00.000Z",
                "message": {"role": "user", "content": "hello"},
            },
            {
                "uuid": "u-2",
                "type": "assistant",
                "sessionId": sid,
                "timestamp": "2026-04-24T09:00:01.000Z",
                "message": {"role": "assistant", "content": "hi"},
            },
            {
                "uuid": "u-3",
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-04-24T09:00:02.000Z",
                "message": {"role": "user", "content": "bye"},
            },
        ],
    )

    full = client.get(f"/api/projects/{slug}/sessions/{sid}/messages")
    assert full.status_code == 200, full.text
    full_body = full.json()
    assert [m["uuid"] for m in full_body["messages"]] == ["u-1", "u-2", "u-3"]
    assert full_body["last_uuid"] == "u-3"
    full_offset = full_body["file_offset"]

    delta = client.get(
        f"/api/projects/{slug}/sessions/{sid}/messages?since_uuid=u-2"
    )
    assert delta.status_code == 200, delta.text
    delta_body = delta.json()
    # Only the third record's normalized messages — u-2 itself is dropped.
    assert [m["uuid"] for m in delta_body["messages"]] == ["u-3"]
    # Cursor fields still describe the full file so the client can keep
    # tailing on the next refetch.
    assert delta_body["last_uuid"] == "u-3"
    assert delta_body["file_offset"] == full_offset

    # Unknown since_uuid falls back to the full payload (client may have
    # dropped its cache or the file was rotated).
    miss = client.get(
        f"/api/projects/{slug}/sessions/{sid}/messages?since_uuid=does-not-exist"
    )
    assert miss.status_code == 200
    assert [m["uuid"] for m in miss.json()["messages"]] == ["u-1", "u-2", "u-3"]

    # since_uuid pointing at the last record returns no messages.
    tail = client.get(
        f"/api/projects/{slug}/sessions/{sid}/messages?since_uuid=u-3"
    )
    assert tail.status_code == 200
    assert tail.json()["messages"] == []
