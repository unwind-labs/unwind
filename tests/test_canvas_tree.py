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
from unwind.fork_detect import ForkDetector
from unwind.spawns import SpawnResolver
from unwind.subagents import SubagentIndex


def _resolver(
    project_dir: Path,
    callstack: CallstackIndex,
) -> SpawnResolver:
    """Build a SpawnResolver wired the same way registry does."""
    builder = CanvasTreeBuilder(project_dir)
    return SpawnResolver(
        callstack,
        ForkDetector(project_dir, session_scanner=builder.get_scan),
        SubagentIndex(project_dir),
        project_dir=project_dir,
        session_scanner=builder.get_scan,
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
    sid: str,
    ts: str,
    text: str = "ok",
    uuid: str = "a-1",
    usage: dict | None = None,
) -> dict:
    msg: dict = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }
    if usage is not None:
        msg["usage"] = usage
    return {
        "uuid": uuid,
        "type": "assistant",
        "sessionId": sid,
        "timestamp": ts,
        "message": msg,
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


def test_scan_session_extracts_usage_events(tmp_path: Path):
    """Each assistant message with a ``message.usage`` block becomes one
    ``(ts, cw, cr, r, w)`` tuple. Messages without ``usage`` (or with
    all-zero counters) are skipped so leaf nodes don't pick up phantom
    rows."""
    proj = tmp_path / "proj"
    sid = "s1"
    _write_session(
        proj,
        sid,
        [
            _user(sid, "2026-05-04T10:00:00Z"),
            _assistant(
                sid,
                "2026-05-04T10:00:05Z",
                text="t1",
                uuid="a-1",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 1000,
                },
            ),
            # No usage block — should not appear in events.
            _assistant(sid, "2026-05-04T10:00:06Z", text="t2", uuid="a-2"),
            _assistant(
                sid,
                "2026-05-04T10:00:07Z",
                text="t3",
                uuid="a-3",
                usage={
                    "input_tokens": 5,
                    "output_tokens": 7,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            ),
        ],
    )
    scan = scan_session(proj / f"{sid}.jsonl")
    assert len(scan.usage_events) == 2
    # (ts, model, cw, cr, r, w) ordering
    _ts1, _m1, cw1, cr1, r1, w1 = scan.usage_events[0]
    assert (cw1, cr1, r1, w1) == (100, 1000, 10, 20)
    _ts2, _m2, cw2, cr2, r2, w2 = scan.usage_events[1]
    assert (cw2, cr2, r2, w2) == (0, 0, 5, 7)


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
    invs = collect_invocations(_resolver(tmp_path / "proj" if (tmp_path / "proj").exists() else tmp_path, ci))
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
    invs = collect_invocations(_resolver(tmp_path / "proj" if (tmp_path / "proj").exists() else tmp_path, ci))
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
    root, all_w = build_canvas_tree(proj, sid, spawn_resolver=_resolver(proj, ci))
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
    root, all_w = build_canvas_tree(proj, "MAIN", spawn_resolver=_resolver(proj, ci))
    assert root.session_id == "MAIN"
    assert len(root.children) == 1
    child = root.children[0]
    assert child.session_id == "CHILD"
    assert child.parent_window_id == root.window_id
    assert child.kind == "call"
    assert len(all_w) == 2


def test_usage_self_and_subtree_aggregate_post_order(tmp_path: Path):
    """End-to-end: parent and child each have usage events; the parent's
    ``self_usage`` reflects only its own tokens and ``subtree_usage``
    reflects parent + child. Leaf's subtree equals its self.
    """
    proj = tmp_path / "proj"
    _write_session(
        proj,
        "MAIN",
        [
            _user("MAIN", "2026-05-04T10:00:00Z"),
            _assistant(
                "MAIN",
                "2026-05-04T10:00:02Z",
                text="m1",
                uuid="m-a-1",
                usage={
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 4,
                },
            ),
        ],
    )
    _write_session(
        proj,
        "CHILD",
        [
            _user("CHILD", "2026-05-04T10:00:01Z"),
            _assistant(
                "CHILD",
                "2026-05-04T10:00:03Z",
                text="c1",
                uuid="c-a-1",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40,
                },
            ),
        ],
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
    root, _all = build_canvas_tree(proj, "MAIN", spawn_resolver=_resolver(proj, ci))
    child = root.children[0]
    # Leaf: self == subtree.
    assert child.self_usage == {"cw": 30, "cr": 40, "r": 10, "w": 20}
    assert child.subtree_usage == child.self_usage
    # Parent: self counts only its own tokens; subtree adds the child's.
    assert root.self_usage == {"cw": 3, "cr": 4, "r": 1, "w": 2}
    assert root.subtree_usage == {"cw": 33, "cr": 44, "r": 11, "w": 22}


def test_usage_cost_aggregates_per_model_rates(tmp_path: Path):
    """Cost is computed per-record using the assistant message's ``model``
    field, then aggregated subtree-style like the token counters. A
    sonnet turn and an opus turn in the same window add at their
    respective rates (sonnet input $3/M, opus input $15/M).
    """
    proj = tmp_path / "proj"
    sid = "ROOT"
    # Two assistant turns, same input_tokens count, different models.
    # Expected cost.r = 1,000,000 * $3/M (sonnet) + 1,000,000 * $15/M (opus)
    #                 = $3 + $15 = $18
    def _assist_with_model(uuid: str, ts: str, model: str) -> dict:
        rec = _assistant(
            sid,
            ts,
            uuid=uuid,
            usage={
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )
        rec["message"]["model"] = model
        return rec

    _write_session(
        proj,
        sid,
        [
            _user(sid, "2026-05-04T10:00:00Z"),
            _assist_with_model("a-1", "2026-05-04T10:00:01Z", "claude-sonnet-4-6"),
            _assist_with_model("a-2", "2026-05-04T10:00:02Z", "claude-opus-4-7"),
        ],
    )
    log = tmp_path / "log"
    log.mkdir()
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(proj, sid, spawn_resolver=_resolver(proj, ci))
    # Sonnet input @ $3/M for 1M tokens = $3.00
    # Opus input @ $15/M for 1M tokens = $15.00
    # Total input cost = $18.00. Other categories are zero.
    assert abs(root.self_cost["r"] - 18.0) < 1e-9
    assert root.self_cost["cw"] == 0
    assert root.self_cost["cr"] == 0
    assert root.self_cost["w"] == 0
    # Leaf: subtree == self.
    assert root.subtree_cost == root.self_cost


def test_window_node_to_dict_includes_usage_fields(tmp_path: Path):
    """Serializer exposes ``self_usage`` and ``subtree_usage`` so the
    frontend can render the footer without an extra round-trip."""
    proj = tmp_path / "proj"
    sid = "ROOT"
    _write_session(
        proj,
        sid,
        [
            _user(sid, "2026-05-04T10:00:00Z"),
            _assistant(
                sid,
                "2026-05-04T10:00:01Z",
                usage={
                    "input_tokens": 7,
                    "output_tokens": 11,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            ),
        ],
    )
    log = tmp_path / "log"
    log.mkdir()
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(proj, sid, spawn_resolver=_resolver(proj, ci))
    d = root.to_dict()
    assert d["self_usage"] == {"cw": 0, "cr": 0, "r": 7, "w": 11}
    assert d["subtree_usage"] == {"cw": 0, "cr": 0, "r": 7, "w": 11}


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
    root, _all = build_canvas_tree(proj, "MAIN", spawn_resolver=_resolver(proj, ci))
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
    root, all_w = build_canvas_tree(proj, "MAIN", spawn_resolver=_resolver(proj, ci))
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
    root, _ = build_canvas_tree(proj, "MAIN", spawn_resolver=_resolver(proj, ci))
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
    root, _ = build_canvas_tree(proj, "MAIN", spawn_resolver=_resolver(proj, ci))
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
    root, _ = build_canvas_tree(proj, "MAIN", spawn_resolver=_resolver(proj, ci))
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
        proj, sid, spawn_resolver=_resolver(proj, ci), is_live_session=lambda _sid: True
    )
    assert root.status == "yield"


def test_subtree_status_propagates_live_descendant_to_done_root(tmp_path: Path):
    """A finished (``done``) root whose descendant is still ``live`` must
    expose ``subtree_status="live"`` so the canvas can pulse the
    ancestor's rail. ``status`` stays ``done`` — that's the self signal
    used by the in-card terminator row."""
    proj = tmp_path / "proj"
    _write_session(proj, "MAIN", [_user("MAIN", "2026-05-04T10:00:00Z")])
    _write_session(proj, "CHILD", [_user("CHILD", "2026-05-04T10:00:01Z")])
    log = tmp_path / "log"
    _write_report(
        log,
        "i0",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:01+00:00",
        ended_at="2026-05-04T10:00:05+00:00",
        tasks=[{
            "task": "/task-x",
            "status": "running",  # child still in-flight
            "depth": 1,
            "session_id": "CHILD",
        }],
    )
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(
        proj, "MAIN", spawn_resolver=_resolver(proj, ci), is_live_session=lambda sid: sid == "CHILD"
    )
    assert root.status == "done"
    assert root.subtree_status == "live"
    assert root.children[0].status == "live"
    assert root.children[0].subtree_status == "live"


def test_subtree_status_yield_beats_done_but_loses_to_live(tmp_path: Path):
    """Priority is ``live`` > ``yield`` > ``done``. With one yielded
    child and one live grandchild, the root must surface ``live``."""
    proj = tmp_path / "proj"
    _write_session(proj, "MAIN", [_user("MAIN", "2026-05-04T10:00:00Z")])
    _write_session(proj, "Y", [_user("Y", "2026-05-04T10:00:01Z")])
    _write_session(proj, "L", [_user("L", "2026-05-04T10:00:02Z")])
    log = tmp_path / "log"
    _write_report(
        log,
        "i0",
        parent_sid="MAIN",
        started_at="2026-05-04T10:00:01+00:00",
        ended_at="2026-05-04T10:00:05+00:00",
        tasks=[
            {"task": "/y", "status": "yielded", "depth": 1, "session_id": "Y"},
            {"task": "/l", "status": "running", "depth": 1, "session_id": "L"},
        ],
    )
    ci = CallstackIndex(log)
    root, _ = build_canvas_tree(
        proj, "MAIN", spawn_resolver=_resolver(proj, ci), is_live_session=lambda sid: sid in {"Y", "L"}
    )
    assert root.status == "done"
    assert root.subtree_status == "live"


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
    root, _ = build_canvas_tree(proj, sid, spawn_resolver=_resolver(proj, ci), is_live_session=lambda _sid: False)
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
