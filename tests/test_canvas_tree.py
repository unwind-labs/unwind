"""Tests for the canvas tree builder."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from unwind.callstack import CallstackIndex
from unwind.canvas_tree import (
    CanvasTreeBuilder,
    build_canvas_tree,
    collect_invocations,
    scan_session,
)


# --- helpers -------------------------------------------------------------


def _write_session(proj_dir: Path, sid: str, lines: list[dict]) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n"
    )


def _user(sid: str, ts: str, text: str = "hi", uuid: str = "u-1") -> dict:
    return {
        "uuid": uuid,
        "type": "user",
        "sessionId": sid,
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant(
    sid: str, ts: str, text: str = "ok", uuid: str = "a-1"
) -> dict:
    return {
        "uuid": uuid,
        "type": "assistant",
        "sessionId": sid,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _yield_message(sid: str, ts: str, question: str, uuid: str = "y-1") -> dict:
    """A Claude assistant message containing a yield envelope."""
    body = "```json\n" + json.dumps({"op": "yield", "question": question}) + "\n```"
    return _assistant(sid, ts, text=body, uuid=uuid)


def _write_report(
    log_dir: Path,
    invoke_id: str,
    parent_sid: str,
    started_at: str,
    ended_at: str,
    *,
    kind: str = "invoke",
    status: str = "complete",
    tasks: list[dict],
) -> None:
    d = log_dir / invoke_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": invoke_id,
                "kind": kind,
                "parent_session": parent_sid,
                "started_at": started_at,
                "ended_at": ended_at,
                "status": status,
                "tasks": tasks,
            }
        )
    )


# --- scan_session --------------------------------------------------------


def test_scan_session_captures_start_end_and_yields(tmp_path: Path):
    proj = tmp_path / "proj"
    sid = "s1"
    _write_session(
        proj,
        sid,
        [
            _user(sid, "2026-05-04T10:00:00Z"),
            _assistant(sid, "2026-05-04T10:00:05Z", text="thinking"),
            _yield_message(sid, "2026-05-04T10:00:10Z", "MFA code please"),
            _user(sid, "2026-05-04T10:01:00Z", text="000000", uuid="u-2"),
            _assistant(sid, "2026-05-04T10:01:05Z", text="done", uuid="a-2"),
        ],
    )
    scan = scan_session(proj / f"{sid}.jsonl")
    assert scan.session_id == sid
    assert scan.start_ts is not None
    assert scan.end_ts is not None
    assert len(scan.yields) == 1
    # Yield should be the assistant message at 10:00:10
    assert scan.yields[0].isoformat().startswith("2026-05-04T10:00:10")


def test_scan_session_handles_missing_file(tmp_path: Path):
    scan = scan_session(tmp_path / "nope.jsonl")
    assert scan.start_ts is None
    assert scan.end_ts is None
    assert scan.yields == []


# --- collect_invocations ------------------------------------------------


def test_collect_invocations_returns_one_entry_per_report(tmp_path: Path):
    log = tmp_path / "log"
    # Three reports recording the same parent → child edge.
    for i, ts in enumerate(
        ["2026-05-04T10:00:00+00:00", "2026-05-04T10:01:00+00:00", "2026-05-04T10:02:00+00:00"]
    ):
        _write_report(
            log,
            f"i{i}",
            parent_sid="MAIN",
            started_at=ts,
            ended_at=ts,
            kind="invoke" if i == 0 else "invoke_resume",
            tasks=[
                {
                    "task": "/task-x",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "CHILD",
                }
            ],
        )
    ci = CallstackIndex(log)
    invs = collect_invocations(ci)
    assert "CHILD" in invs
    assert len(invs["CHILD"]) == 3
    # Sorted by started_at.
    starts = [i.started_at.isoformat() for i in invs["CHILD"]]
    assert starts == sorted(starts)


def test_collect_invocations_walks_nested_children(tmp_path: Path):
    log = tmp_path / "log"
    _write_report(
        log,
        "i0",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:00+00:00",
        ended_at="2026-05-04T10:00:30+00:00",
        tasks=[
            {
                "task": "/parent",
                "status": "complete",
                "depth": 1,
                "session_id": "P",
                "children": [
                    {
                        "task": "/child",
                        "status": "complete",
                        "depth": 2,
                        "session_id": "C",
                    }
                ],
            }
        ],
    )
    ci = CallstackIndex(log)
    invs = collect_invocations(ci)
    assert {"P", "C"} <= set(invs.keys())
    # P was invoked by MAIN, C was invoked by P.
    assert invs["P"][0].caller_session_id == "MAIN"
    assert invs["C"][0].caller_session_id == "P"


# --- build_canvas_tree --------------------------------------------------


def test_lone_session_with_no_callstack_data_is_one_window(tmp_path: Path):
    proj = tmp_path / "proj"
    sid = "ROOT"
    _write_session(proj, sid, [_user(sid, "2026-05-04T10:00:00Z")])
    log = tmp_path / "log"
    log.mkdir()
    ci = CallstackIndex(log)
    root, all_w = build_canvas_tree(proj, sid, ci)
    assert root.session_id == sid
    assert root.kind == "root"
    assert root.children == []
    assert len(all_w) == 1


def test_root_calls_child_once_yields_two_node_tree(tmp_path: Path):
    proj = tmp_path / "proj"
    _write_session(
        proj, "MAIN", [_user("MAIN", "2026-05-04T10:00:00Z")]
    )
    _write_session(
        proj, "CHILD", [_user("CHILD", "2026-05-04T10:00:01Z")]
    )
    log = tmp_path / "log"
    _write_report(
        log,
        "i0",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:01+00:00",
        ended_at="2026-05-04T10:00:05+00:00",
        tasks=[
            {
                "task": "/task-x",
                "status": "complete",
                "depth": 1,
                "session_id": "CHILD",
            }
        ],
    )
    ci = CallstackIndex(log)
    root, all_w = build_canvas_tree(proj, "MAIN", ci)
    assert root.session_id == "MAIN"
    assert len(root.children) == 1
    child = root.children[0]
    assert child.session_id == "CHILD"
    assert child.parent_window_id == root.window_id
    assert child.kind == "call"
    assert len(all_w) == 2


def test_three_invocations_produce_three_child_windows(tmp_path: Path):
    """Parent invokes child three times. Child should get 3 windows."""
    proj = tmp_path / "proj"
    _write_session(proj, "MAIN", [_user("MAIN", "2026-05-04T10:00:00Z")])
    _write_session(proj, "CHILD", [_user("CHILD", "2026-05-04T10:00:01Z")])
    log = tmp_path / "log"
    timestamps = [
        ("2026-05-04T10:00:01+00:00", "2026-05-04T10:00:05+00:00", "invoke"),
        ("2026-05-04T10:00:10+00:00", "2026-05-04T10:00:15+00:00", "invoke_resume"),
        ("2026-05-04T10:00:20+00:00", "2026-05-04T10:00:25+00:00", "invoke_resume"),
    ]
    for i, (started, ended, kind) in enumerate(timestamps):
        _write_report(
            log,
            f"i{i}",
            parent_sid="MAIN",
            started_at=started,
            ended_at=ended,
            kind=kind,
            tasks=[
                {
                    "task": "/task-x",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "CHILD",
                }
            ],
        )
    ci = CallstackIndex(log)
    root, _all = build_canvas_tree(proj, "MAIN", ci)
    assert len(root.children) == 3
    # Each child is a distinct window of CHILD.
    sids = {c.session_id for c in root.children}
    assert sids == {"CHILD"}
    indices = sorted(c.window_index for c in root.children)
    assert indices == [0, 1, 2]
    # Kind: first is "call", rest are "resume".
    by_index = sorted(root.children, key=lambda c: c.window_index)
    assert by_index[0].kind == "call"
    assert by_index[1].kind == "resume"
    assert by_index[2].kind == "resume"
    # Window starts are in order.
    starts = [c.window_start for c in by_index]
    assert starts == sorted(starts)


def test_grandchild_chain_each_level_gets_own_windows(tmp_path: Path):
    """Three reports recording MAIN → P → C → G, all sessions identical
    across reports. Each of P, C, G should have 3 windows wired correctly
    into the tree (the customer_support `c4835a42 → e7a9597b → 8fc6bc55`
    regression case)."""
    proj = tmp_path / "proj"
    for sid in ("MAIN", "P", "C", "G"):
        _write_session(proj, sid, [_user(sid, "2026-05-04T10:00:00Z")])
    log = tmp_path / "log"
    times = [
        "2026-05-04T10:00:00+00:00",
        "2026-05-04T10:01:00+00:00",
        "2026-05-04T10:02:00+00:00",
    ]
    for i, ts in enumerate(times):
        _write_report(
            log,
            f"i{i}",
            parent_sid="MAIN",
            started_at=ts,
            ended_at=ts,
            kind="invoke" if i == 0 else "invoke_resume",
            tasks=[
                {
                    "task": "/p",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "P",
                    "children": [
                        {
                            "task": "/c",
                            "status": "complete",
                            "depth": 2,
                            "session_id": "C",
                            "children": [
                                {
                                    "task": "/g",
                                    "status": "complete",
                                    "depth": 3,
                                    "session_id": "G",
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    ci = CallstackIndex(log)
    root, all_w = build_canvas_tree(proj, "MAIN", ci)
    p_windows = [w for w in all_w if w.session_id == "P"]
    c_windows = [w for w in all_w if w.session_id == "C"]
    g_windows = [w for w in all_w if w.session_id == "G"]
    assert len(p_windows) == 3
    assert len(c_windows) == 3
    assert len(g_windows) == 3
    # Every C window has a P window parent; every G window has a C window
    # parent. The K-th instance of C should hang off the K-th P.
    for k in range(3):
        ck = next(w for w in c_windows if w.window_index == k)
        gk = next(w for w in g_windows if w.window_index == k)
        pk = next(w for w in p_windows if w.window_index == k)
        assert ck.parent_window_id == pk.window_id
        assert gk.parent_window_id == ck.window_id
        # Each P has exactly one C child; each C has exactly one G child.
        assert len(pk.children) == 1
        assert pk.children[0] is ck
        assert len(ck.children) == 1
        assert ck.children[0] is gk


def test_yielded_status_propagates(tmp_path: Path):
    """A child invocation that ended with status=yielded should have
    status=yield on its window."""
    proj = tmp_path / "proj"
    _write_session(proj, "MAIN", [_user("MAIN", "2026-05-04T10:00:00Z")])
    _write_session(proj, "C", [_user("C", "2026-05-04T10:00:01Z")])
    log = tmp_path / "log"
    _write_report(
        log,
        "i0",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:01+00:00",
        ended_at="2026-05-04T10:00:05+00:00",
        status="yielded",
        tasks=[
            {
                "task": "/task-x",
                "status": "yielded",
                "depth": 1,
                "session_id": "C",
            }
        ],
    )
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(proj, "MAIN", ci)
    assert root.children[0].status == "yield"


def test_only_the_last_window_can_be_yield(tmp_path: Path):
    """Only the FINAL window of a session can be "currently waiting".
    Earlier windows whose task yielded got resumed (that's how a later
    window exists), so they're past and show ``done`` even if the
    invocation's task status was ``yielded``."""
    proj = tmp_path / "proj"
    _write_session(proj, "MAIN", [_user("MAIN", "2026-05-04T10:00:00Z")])
    _write_session(proj, "C", [_user("C", "2026-05-04T10:00:01Z")])
    log = tmp_path / "log"
    # First call yielded. Second resume completed.
    _write_report(
        log,
        "i0",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:01+00:00",
        ended_at="2026-05-04T10:00:05+00:00",
        status="yielded",
        tasks=[
            {"task": "/x", "status": "yielded", "depth": 1, "session_id": "C"}
        ],
    )
    _write_report(
        log,
        "i1",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:10+00:00",
        ended_at="2026-05-04T10:00:15+00:00",
        kind="invoke_resume",
        status="complete",
        tasks=[
            {"task": "/x", "status": "complete", "depth": 1, "session_id": "C"}
        ],
    )
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(proj, "MAIN", ci)
    assert len(root.children) == 2
    # First window: yielded then resumed → done (the yield was answered).
    assert root.children[0].status == "done"
    # Second window: completed terminally → done.
    assert root.children[1].status == "done"


def test_last_window_with_yielded_status_shows_yield(tmp_path: Path):
    """The final window of a child whose task is currently ``yielded``
    (no later resume yet) shows ``yield``."""
    proj = tmp_path / "proj"
    _write_session(proj, "MAIN", [_user("MAIN", "2026-05-04T10:00:00Z")])
    _write_session(proj, "C", [_user("C", "2026-05-04T10:00:01Z")])
    log = tmp_path / "log"
    _write_report(
        log,
        "i0",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:01+00:00",
        ended_at="2026-05-04T10:00:05+00:00",
        status="yielded",
        tasks=[
            {"task": "/x", "status": "yielded", "depth": 1, "session_id": "C"}
        ],
    )
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(proj, "MAIN", ci)
    assert len(root.children) == 1
    assert root.children[0].status == "yield"


def test_yield_in_live_root_sets_root_status_to_yield(tmp_path: Path):
    """An ALIVE root session that ends with a yield envelope shows
    ``yield``. (When the process has exited or activity is stale, the
    same content is treated as ``done`` — see the ``stale`` test
    below — since virtually every historical session sits at a
    ``stop_hook_summary`` and we'd otherwise drown the canvas in
    amber.)"""
    proj = tmp_path / "proj"
    sid = "MAIN"
    _write_session(
        proj,
        sid,
        [
            _user(sid, "2026-05-04T10:00:00Z"),
            _assistant(sid, "2026-05-04T10:00:05Z"),
            _yield_message(sid, "2026-05-04T10:00:10Z", "Approve?"),
        ],
    )
    log = tmp_path / "log"
    log.mkdir()
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(
        proj, sid, ci, is_live_session=lambda _sid: True
    )
    assert root.status == "yield"


def test_stale_root_with_yield_envelope_is_done_not_yield(tmp_path: Path):
    """Same yield-envelope content, but the session is stale (process
    not running, no recent activity). Status downgrades to ``done``
    so historical sessions don't all light up amber."""
    proj = tmp_path / "proj"
    sid = "MAIN"
    _write_session(
        proj,
        sid,
        [
            _user(sid, "2026-05-04T10:00:00Z"),
            _assistant(sid, "2026-05-04T10:00:05Z"),
            _yield_message(sid, "2026-05-04T10:00:10Z", "Approve?"),
        ],
    )
    log = tmp_path / "log"
    log.mkdir()
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(proj, sid, ci, is_live_session=lambda _sid: False)
    assert root.status == "done"


# --- builder caching ----------------------------------------------------


def test_builder_caches_scans_until_mtime_changes(tmp_path: Path):
    proj = tmp_path / "proj"
    sid = "S"
    _write_session(proj, sid, [_user(sid, "2026-05-04T10:00:00Z")])
    builder = CanvasTreeBuilder(proj)
    s1 = builder.get_scan(sid)
    s2 = builder.get_scan(sid)
    assert s1 is s2  # exact same object → cache hit

    # Append more — mtime/size change → re-scan returns a new object.
    import time as _time

    _time.sleep(0.01)
    path = proj / f"{sid}.jsonl"
    with path.open("a") as fh:
        fh.write(
            json.dumps(_user(sid, "2026-05-04T10:01:00Z", uuid="u-2")) + "\n"
        )
    s3 = builder.get_scan(sid)
    assert s3 is not s1
