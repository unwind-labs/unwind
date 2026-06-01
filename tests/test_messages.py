from pathlib import Path
import json

from unwind.callstack import CallstackIndex
from unwind.fork_detect import ForkDetector
from unwind.messages import annotate_spawns, read_messages
from unwind.spawns import SpawnResolver
from unwind.subagents import SubagentIndex


def _resolver(
    project_dir: Path,
    log_dir: Path,
    *,
    callstack: CallstackIndex | None = None,
) -> SpawnResolver:
    """Build a SpawnResolver for tests with the same wiring as
    registry.spawn_resolver_for_slug. ``log_dir`` is the callstack log
    dir; pass a non-existent path when callstack isn't relevant."""
    return SpawnResolver(
        callstack if callstack is not None else CallstackIndex(log_dir),
        ForkDetector(project_dir),
        SubagentIndex(project_dir),
        project_dir=project_dir,
    )


def _write(tmp: Path, lines: list[dict]) -> Path:
    p = tmp / "s.jsonl"
    with p.open("w") as fh:
        for rec in lines:
            fh.write(json.dumps(rec) + "\n")
    return p


def test_pairs_tool_use_with_tool_result(tmp_path: Path):
    lines = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:00.000Z",
            "message": {"role": "user", "content": "run"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "sonnet",
                "content": [
                    {"type": "text", "text": "sure"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                ],
            },
        },
    ]
    p = _write(tmp_path, lines)
    page = read_messages(p)
    roles = [m.role for m in page.messages]
    assert "user" in roles
    assert "assistant" in roles
    assert "tool_use" in roles
    assert "tool_result" in roles

    tool_use = [m for m in page.messages if m.role == "tool_use"][0]
    assert tool_use.tool_name == "Bash"
    assert tool_use.tool_input == {"cmd": "ls"}

    tool_result = [m for m in page.messages if m.role == "tool_result"][0]
    assert tool_result.tool_result_for == "t1"
    assert tool_result.tool_result == "ok"


def test_thinking_blocks_surface_as_thinking_messages(tmp_path: Path):
    """Assistant chain-of-thought must reach the UI: a ``thinking`` content
    block becomes a role=thinking Message carrying the reasoning text, rather
    than being silently dropped alongside text/tool_use blocks."""
    lines = [
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "sonnet",
                "content": [
                    {"type": "thinking", "thinking": "let me reason", "signature": "x"},
                    {"type": "text", "text": "the answer"},
                ],
            },
        },
    ]
    p = _write(tmp_path, lines)
    page = read_messages(p)
    roles = [m.role for m in page.messages]
    assert roles == ["thinking", "assistant"]

    thinking = [m for m in page.messages if m.role == "thinking"][0]
    assert thinking.text == "let me reason"


def test_empty_thinking_with_signature_surfaces_encrypted_placeholder(tmp_path: Path):
    """Claude Opus 4.7 routinely emits ``type: thinking`` blocks with an empty
    ``thinking`` field and a populated ``signature`` (the encrypted reasoning).
    Without a placeholder these render as "(empty)" in the trace, which reads
    like a bug. Surface a distinct placeholder so the user sees the model
    thought, but the text is unavailable."""
    lines = [
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "ENC=="},
                    {"type": "text", "text": "the answer"},
                ],
            },
        },
    ]
    p = _write(tmp_path, lines)
    page = read_messages(p)
    thinking = [m for m in page.messages if m.role == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].text == "[encrypted thinking]"


def test_whitespace_only_thinking_with_signature_surfaces_encrypted_placeholder(
    tmp_path: Path,
):
    """Whitespace-only ``thinking`` text is functionally empty — it would
    render as a blank bubble — so the placeholder logic must treat it the
    same as an empty string. Pins the ``.strip()`` check in
    ``_normalize_assistant`` so a regression to truthy-check-only behavior
    is caught."""
    lines = [
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "   \n  ", "signature": "ENC=="},
                ],
            },
        },
    ]
    p = _write(tmp_path, lines)
    page = read_messages(p)
    thinking = [m for m in page.messages if m.role == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].text == "[encrypted thinking]"


def test_redacted_thinking_surfaces_placeholder(tmp_path: Path):
    """``redacted_thinking`` has encrypted ``data`` and no readable text, so
    it surfaces a placeholder rather than an empty bubble."""
    lines = [
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "encrypted"},
                ],
            },
        },
    ]
    p = _write(tmp_path, lines)
    page = read_messages(p)
    thinking = [m for m in page.messages if m.role == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].text == "[redacted thinking]"


def test_include_meta_toggles_attachments(tmp_path: Path):
    lines = [
        {
            "type": "attachment",
            "uuid": "att1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:00.000Z",
            "attachment": {"hookName": "hook", "content": "hidden by default"},
        },
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {"role": "user", "content": "visible"},
        },
    ]
    p = _write(tmp_path, lines)
    no_meta = read_messages(p)
    assert [m.role for m in no_meta.messages] == ["user"]

    with_meta = read_messages(p, include_meta=True)
    roles = [m.role for m in with_meta.messages]
    assert roles == ["system", "user"]


def test_attachment_subtype_surfaces_as_raw_type(tmp_path: Path):
    """The attachment's specific subtype (skill_listing, hook_success, …)
    is exposed via ``raw_type`` so the UI can dispatch a per-type renderer.
    skill_listing's body is the listing itself, with no ``[name]`` prefix."""
    lines = [
        {
            "type": "attachment",
            "uuid": "att1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:00.000Z",
            "attachment": {
                "type": "skill_listing",
                "content": "- alpha: do a\n- beta: do b",
                "skillCount": 2,
            },
        },
        {
            "type": "attachment",
            "uuid": "att2",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "attachment": {
                "type": "deferred_tools_delta",
                "addedNames": ["CronCreate", "CronList"],
            },
        },
    ]
    p = _write(tmp_path, lines)
    msgs = read_messages(p, include_meta=True).messages
    by_type = {m.raw_type: m for m in msgs}
    assert "skill_listing" in by_type
    assert by_type["skill_listing"].text == "- alpha: do a\n- beta: do b"
    assert "deferred_tools_delta" in by_type
    assert by_type["deferred_tools_delta"].text == "added: CronCreate, CronList"


def test_in_flight_agent_matches_pending_subagent(tmp_path: Path):
    """While an Agent tool_use is pending (no tool_result yet), unwind should
    still link it to the on-disk subagent trace so the UI can show the live
    subagent. Two sibling Agent calls with different descriptions get matched
    to the right traces, even though neither has a tool_result yet."""
    session_id = "sess-123"
    project_dir = tmp_path / "project"
    sub_dir = project_dir / session_id / "subagents"
    sub_dir.mkdir(parents=True)

    # Two pending subagents written by Claude Code.
    for agent_id, desc in (("aaaa1111", "Branch audit"), ("bbbb2222", "Code review")):
        (sub_dir / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": desc})
        )
        (sub_dir / f"agent-{agent_id}.jsonl").write_text("")

    # Parent session JSONL with two Agent tool_uses, no tool_results yet.
    parent_jsonl = project_dir / f"{session_id}.jsonl"
    parent_jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "uuid": "a1",
            "sessionId": session_id,
            "timestamp": "2026-04-24T08:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Agent",
                     "input": {"description": "Branch audit", "prompt": "..."}},
                    {"type": "tool_use", "id": "t2", "name": "Agent",
                     "input": {"description": "Code review", "prompt": "..."}},
                ],
            },
        }) + "\n"
    )

    page = read_messages(parent_jsonl)
    resolver = _resolver(project_dir, project_dir / "nope-callstack")
    annotate_spawns(
        page.messages, current_session_id=session_id, spawn_resolver=resolver
    )

    tool_uses = [m for m in page.messages if m.role == "tool_use"]
    assert len(tool_uses) == 2

    by_id = {m.tool_use_id: m for m in tool_uses}
    assert by_id["t1"].spawn_kind == "subagent"
    assert by_id["t1"].spawn_session_ids == ["agent-aaaa1111"]
    assert by_id["t2"].spawn_kind == "subagent"
    assert by_id["t2"].spawn_session_ids == ["agent-bbbb2222"]


def test_sibling_callstack_invokes_dont_phantom_each_others_children(tmp_path: Path):
    """When a session makes multiple ``invoke_parallel`` calls that all merge
    into the same callstack report (current callstack behavior — nested invokes
    write into the outermost invocation's report), each tool_use should only
    surface its OWN requested children. Without per-tool_use claiming, the
    leftover-task logic phantom-renders sibling tool_uses' children as extra
    rows, producing the 11-row jumble we observed in the deep-rewrite trace.
    """
    session_id = "sess-fork"
    log_dir = tmp_path / "callstack-log"
    invoke_dir = log_dir / "20260101T000000-aaaa"
    invoke_dir.mkdir(parents=True)

    # Report has the parent fork's task containing all 9 sibling children
    # (5 specialists + 3 meta + 1 re-author) — what callstack would write
    # once Fix 2 lands. The bug is sensitive to all of them sharing one report.
    report = {
        "invoke_id": "20260101T000000-aaaa",
        "parent_session": "root-sess",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:10:00+00:00",
        "status": "complete",
        "tasks": [
            {
                "id": "fork0001",
                "task": "fork driver",
                "status": "complete",
                "depth": 1,
                "session_id": session_id,
                "children": [
                    *[
                        {"id": f"sp{i}", "task": f"specialist {i}",
                         "status": "complete", "depth": 2,
                         "session_id": f"sp-sess-{i}"}
                        for i in range(5)
                    ],
                    *[
                        {"id": f"ma{i}", "task": f"meta-assessor {i}",
                         "status": "complete", "depth": 2,
                         "session_id": f"ma-sess-{i}"}
                        for i in range(3)
                    ],
                    {"id": "ra0", "task": "re-author",
                     "status": "complete", "depth": 2,
                     "session_id": "ra-sess"},
                ],
            }
        ],
    }
    import yaml as _yaml
    (invoke_dir / "report.yaml").write_text(_yaml.safe_dump(report))

    # Three sibling callstack tool_uses, all with tool_results pointing at
    # the same invoke_id (matching the deep-rewrite scenario).
    def _result(invoke_id: str) -> str:
        return '{"invoke_id": "' + invoke_id + '"}'

    parent = tmp_path / "parent.jsonl"
    parent.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "uuid": "a1", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:01.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu-spec",
              "name": "mcp__plugin_callstack_call__invoke_parallel",
              "input": {"tasks": [f"specialist {i}" for i in range(5)]}},
         ]}},
        {"type": "user", "uuid": "u1", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:02.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu-spec",
              "content": _result("20260101T000000-aaaa")},
         ]}},
        {"type": "assistant", "uuid": "a2", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:03.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu-meta",
              "name": "mcp__plugin_callstack_call__invoke_parallel",
              "input": {"tasks": [f"meta-assessor {i}" for i in range(3)]}},
         ]}},
        {"type": "user", "uuid": "u2", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:04.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu-meta",
              "content": _result("20260101T000000-aaaa")},
         ]}},
        {"type": "assistant", "uuid": "a3", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:05.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu-ra",
              "name": "mcp__plugin_callstack_call__invoke",
              "input": {"task": "re-author"}},
         ]}},
        {"type": "user", "uuid": "u3", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:06.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu-ra",
              "content": _result("20260101T000000-aaaa")},
         ]}},
    ]) + "\n")

    page = read_messages(parent)
    ci = CallstackIndex(log_dir)
    resolver = _resolver(tmp_path, log_dir, callstack=ci)
    annotate_spawns(
        page.messages,
        slug_callstack=ci,
        current_session_id=session_id,
        spawn_resolver=resolver,
    )

    by_id = {m.tool_use_id: m for m in page.messages if m.role == "tool_use"}

    # Each tool_use claims ONLY its requested children — no leftover phantom
    # rows from sibling tool_uses' tasks.
    assert by_id["tu-spec"].spawn_session_ids == [f"sp-sess-{i}" for i in range(5)]
    assert by_id["tu-meta"].spawn_session_ids == [f"ma-sess-{i}" for i in range(3)]
    assert by_id["tu-ra"].spawn_session_ids == ["ra-sess"]

    # Total rows: 5 + 3 + 1 = 9, not the 11 (or 17) the buggy version emitted.
    total = sum(len(m.spawn_session_ids) for m in by_id.values())
    assert total == 9


def test_in_flight_callstack_anchors_via_shared_report(tmp_path: Path):
    """When the specialists tool_use has completed but the meta-assessor
    tool_use is still in-flight (no tool_result yet), the in-flight branch
    must fall back to the SAME merged report so name-matching can still
    resolve meta-assessor session_ids. Without this, the meta tool_use
    emits empty placeholders, the actual session_ids leak into the
    extras_spawns path, and the UI renders a duplicate set of cards."""
    session_id = "sess-inflight"
    log_dir = tmp_path / "callstack-log"
    invoke_dir = log_dir / "20260101T000000-aaaa"
    invoke_dir.mkdir(parents=True)

    # Merged report (Fix 2 callstack output): all children present, with
    # meta-assessors marked running.
    report = {
        "invoke_id": "20260101T000000-aaaa",
        "parent_session": "root-sess",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": None,
        "status": "mixed",
        "tasks": [
            {
                "id": "fork0001",
                "task": "fork driver",
                "status": "running",
                "depth": 1,
                "session_id": session_id,
                "children": [
                    *[
                        {"id": f"sp{i}", "task": f"specialist {i}",
                         "status": "complete", "depth": 2,
                         "session_id": f"sp-sess-{i}"}
                        for i in range(5)
                    ],
                    *[
                        {"id": f"ma{i}", "task": f"meta-assessor {i}",
                         "status": "running", "depth": 2,
                         "session_id": f"ma-sess-{i}"}
                        for i in range(3)
                    ],
                ],
            }
        ],
    }
    import yaml as _yaml
    (invoke_dir / "report.yaml").write_text(_yaml.safe_dump(report))

    parent = tmp_path / "parent.jsonl"
    parent.write_text("\n".join(json.dumps(r) for r in [
        # Specialists: tool_use + tool_result (completed).
        {"type": "assistant", "uuid": "a1", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:01.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu-spec",
              "name": "mcp__plugin_callstack_call__invoke_parallel",
              "input": {"tasks": [f"specialist {i}" for i in range(5)]}},
         ]}},
        {"type": "user", "uuid": "u1", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:02.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu-spec",
              "content": '{"invoke_id": "20260101T000000-aaaa"}'},
         ]}},
        # Meta-assessors: tool_use only, NO tool_result (in flight).
        {"type": "assistant", "uuid": "a2", "sessionId": session_id,
         "timestamp": "2026-01-01T00:00:03.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu-meta",
              "name": "mcp__plugin_callstack_call__invoke_parallel",
              "input": {"tasks": [f"meta-assessor {i}" for i in range(3)]}},
         ]}},
    ]) + "\n")

    page = read_messages(parent)
    ci = CallstackIndex(log_dir)
    resolver = _resolver(tmp_path, log_dir, callstack=ci)
    annotate_spawns(
        page.messages,
        slug_callstack=ci,
        current_session_id=session_id,
        spawn_resolver=resolver,
    )

    by_id = {m.tool_use_id: m for m in page.messages if m.role == "tool_use"}

    # Both tool_uses anchor to their actual children — even though meta is
    # in-flight and shares a report with the already-claimed specialists.
    assert by_id["tu-spec"].spawn_session_ids == [f"sp-sess-{i}" for i in range(5)]
    assert by_id["tu-meta"].spawn_session_ids == [f"ma-sess-{i}" for i in range(3)]


def test_partial_completion_marks_done_per_child(tmp_path: Path):
    """When 4 of 5 ``invoke_parallel`` children have completed but the 5th
    is still running, the parent tool_use has no tool_result yet — but the
    4 completed children should already show as done in the caller card.
    Drives the spawn_status field from the callstack report's per-task status.
    """
    session_id = "sess-partial"
    log_dir = tmp_path / "callstack-log"
    invoke_dir = log_dir / "20260101T000000-bbbb"
    invoke_dir.mkdir(parents=True)

    # 4 of 5 specialists complete, one still running.
    report = {
        "invoke_id": "20260101T000000-bbbb",
        "parent_session": "root-sess",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": None,
        "status": "mixed",
        "tasks": [
            {
                "id": "fork0001",
                "task": "fork driver",
                "status": "running",
                "depth": 1,
                "session_id": session_id,
                "children": [
                    {"id": "sp0", "task": "specialist 0", "status": "complete",
                     "depth": 2, "session_id": "sp-sess-0"},
                    {"id": "sp1", "task": "specialist 1", "status": "complete",
                     "depth": 2, "session_id": "sp-sess-1"},
                    {"id": "sp2", "task": "specialist 2", "status": "complete",
                     "depth": 2, "session_id": "sp-sess-2"},
                    {"id": "sp3", "task": "specialist 3", "status": "complete",
                     "depth": 2, "session_id": "sp-sess-3"},
                    {"id": "sp4", "task": "specialist 4", "status": "running",
                     "depth": 2, "session_id": "sp-sess-4"},
                ],
            }
        ],
    }
    import yaml as _yaml
    (invoke_dir / "report.yaml").write_text(_yaml.safe_dump(report))

    parent = tmp_path / "parent.jsonl"
    # In-flight: tool_use only, no tool_result yet (the parent invoke_parallel
    # hasn't returned because specialist 4 is still running).
    parent.write_text(json.dumps({
        "type": "assistant", "uuid": "a1", "sessionId": session_id,
        "timestamp": "2026-01-01T00:00:01.000Z",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu-spec",
             "name": "mcp__plugin_callstack_call__invoke_parallel",
             "input": {"tasks": [f"specialist {i}" for i in range(5)]}},
        ]},
    }) + "\n")

    page = read_messages(parent)
    ci = CallstackIndex(log_dir)
    resolver = _resolver(tmp_path, log_dir, callstack=ci)
    annotate_spawns(
        page.messages,
        slug_callstack=ci,
        current_session_id=session_id,
        spawn_resolver=resolver,
    )

    spec = next(m for m in page.messages if m.role == "tool_use")
    assert spec.spawn_session_ids == [f"sp-sess-{i}" for i in range(5)]
    # First 4 individually marked done despite parent tool_result being absent.
    # ``running`` canonicalises to ``live``; ``complete`` to ``done``.
    assert spec.spawn_status == ["done", "done", "done", "done", "live"]
