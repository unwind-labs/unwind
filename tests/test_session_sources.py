"""Source adapters exercised through discovery, conversations, canvas and reports."""

import importlib
import json
from datetime import timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from unwind.messages import read_messages
from unwind.projects import ProjectPaths, slug_for
from unwind.session_scan import scan_session
from unwind.sources.codex import sessions

SID = "019c31d7-12a8-7a03-bac3-adba05ecff1d"
CLAUDE_SID = "119c31d7-12a8-7a03-bac3-adba05ecff1d"
TS = "2026-05-15T12:00:00Z"


def record(kind, payload, ts=TS):
    return {"type": kind, "payload": payload, "timestamp": ts}


def item(kind, **kwargs):
    return record("response_item", {"type": kind, **kwargs})


def tokens(inp, cached, out):
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "output_tokens": out,
        "reasoning_output_tokens": out // 2,
        "total_tokens": inp + out,
    }


def count(total, ts=TS):
    return record(
        "event_msg",
        {
            "type": "token_count",
            "info": {"total_token_usage": total, "last_token_usage": total},
        },
        ts,
    )


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("UNWIND_DEFAULT_PATH", raising=False)
    monkeypatch.delenv("UNWIND_DEFAULT_SLUG", raising=False)
    import unwind.usage_report as usage_report

    monkeypatch.setattr(usage_report, "_local_tz", lambda: timezone.utc)
    project = tmp_path / "project"
    project.mkdir()
    import unwind.registry as registry

    importlib.reload(registry)
    path = home / "sessions/2026/05/15" / f"rollout-{SID}.jsonl"
    rows = [
        record(
            "session_meta", {"id": SID, "cwd": str(project), "git": {"branch": "main"}}
        ),
        record("turn_context", {"model": "gpt-test"}),
        item(
            "message",
            role="user",
            content=[
                {"type": "input_text", "text": "# AGENTS.md instructions\nContext"}
            ],
        ),
        item(
            "message",
            id="user-1",
            role="user",
            content=[{"type": "input_text", "text": "Fix the tests"}],
        ),
        record("event_msg", {"type": "user_message", "message": "Fix the tests"}),
        item(
            "reasoning",
            summary=[{"type": "summary_text", "text": "Checking the tests"}],
        ),
        item(
            "function_call",
            call_id="call-1",
            name="exec_command",
            arguments='{"cmd":"pytest"}',
        ),
        item("function_call_output", call_id="call-1", output="3 passed"),
        item(
            "custom_tool_call",
            call_id="call-2",
            name="apply_patch",
            input="*** Begin Patch",
        ),
        item("custom_tool_call_output", call_id="call-2", output="Success"),
        item(
            "message",
            id="answer",
            role="assistant",
            content=[{"type": "output_text", "text": "Fixed."}],
        ),
        count(tokens(100, 40, 20)),
        count(tokens(100, 40, 20)),  # repeated snapshot, not another request
        count(tokens(150, 60, 30)),
        record("event_msg", {"type": "task_complete"}),
    ]
    write(path, rows)
    return project, path, rows


def client():
    from unwind.api import projects, sessions_api, usage

    app = FastAPI()
    for router in (projects.router, sessions_api.router, usage.router):
        app.include_router(router, prefix="/api")
    return TestClient(app)


def test_discovery_and_summary_without_claude_directory(workspace):
    project, path, _ = workspace
    from unwind.registry import index_for_slug, list_known_projects

    assert (slug_for(project), project) in list_known_projects()
    index = index_for_slug(slug_for(project))
    summary = index.list_sessions()[0]
    assert summary.session_id == "codex-" + SID
    assert summary.title == "Fix the tests"
    assert summary.provider == "codex"
    assert summary.git_branch == "main"
    assert index.jsonl_path_for(summary.session_id) == path
    assert index.get_session(summary.session_id) == summary


def test_normalizes_tools_reasoning_and_ignores_event_mirrors(workspace):
    _, path, _ = workspace
    page = read_messages(path)
    assert [m.text for m in page.messages if m.role == "user"] == ["Fix the tests"]
    assert [m.text for m in page.messages if m.role == "thinking"] == [
        "Checking the tests"
    ]
    tools = [m for m in page.messages if m.role == "tool_use"]
    assert [(m.tool_name, m.tool_input) for m in tools] == [
        ("exec_command", {"cmd": "pytest"}),
        ("apply_patch", "*** Begin Patch"),
    ]
    assert [m.tool_result_for for m in page.messages if m.role == "tool_result"] == [
        "call-1",
        "call-2",
    ]
    assert all(m.session_id == "codex-" + SID for m in page.messages)
    assert any(
        "AGENTS" in (m.text or "")
        for m in read_messages(path, include_meta=True).messages
    )


def test_usage_is_disjoint_and_cumulative(workspace):
    _, path, _ = workspace
    scan = scan_session(path)
    assert sum(e.r for e in scan.usage_events) == 90
    assert sum(e.cr for e in scan.usage_events) == 60
    assert sum(e.w for e in scan.usage_events) == 30
    assert all(e.provider == "codex" for e in scan.usage_events)
    assert scan.at_user_prompt


def test_modern_usage_records_and_legacy_mirrors_count_once(workspace):
    _, path, rows = workspace
    u = tokens(100, 40, 20)
    rows = rows[:11] + [
        record("token_usage_record", {"thread_token_usage": u, "usage": u}),
        count(u),
    ]
    write(path, rows)
    scan = scan_session(path)
    assert len(scan.usage_events) == 1
    assert scan.usage_events[0].r == 60
    assert not scan.at_user_prompt


def test_appended_messages_have_stable_ids_and_refresh_usage(workspace):
    project, path, _ = workspace
    from unwind.registry import index_for_slug, canvas_tree_builder_for_slug

    index = index_for_slug(slug_for(project))
    before = read_messages(path)
    index.list_sessions()
    with path.open("a") as fh:
        fh.write(
            json.dumps(
                item(
                    "message",
                    role="user",
                    content=[{"type": "input_text", "text": "Again"}],
                )
            )
            + "\n"
        )
        fh.write(json.dumps(count(tokens(200, 80, 40))) + "\n")
        fh.write('{"type":')  # an in-progress final line
    after = read_messages(path)
    assert [m.uuid for m in after.messages[: len(before.messages)]] == [
        m.uuid for m in before.messages
    ]
    assert after.messages[-1].text == "Again"
    assert index.get_session("codex-" + SID).message_count > 0
    scan = canvas_tree_builder_for_slug(slug_for(project)).get_scan("codex-" + SID)
    assert sum(e.r + e.cr + e.w for e in scan.usage_events) == 240


def test_archives_deduplicate_and_deleted_sessions_disappear(workspace):
    project, path, rows = workspace
    from unwind.sources.codex import codex_home

    archive = codex_home() / "archived_sessions" / path.name
    write(archive, rows)
    assert len(sessions(slug_for(project), fresh=True)) == 1
    path.unlink()
    assert sessions(slug_for(project), fresh=True)[0].path == archive
    archive.unlink()
    assert sessions(slug_for(project), fresh=True) == ()


def test_http_project_messages_canvas_and_scope(workspace):
    project, _, _ = workspace
    c = client()
    slug = slug_for(project)
    assert c.get("/api/projects").json()[0]["session_count"] == 1
    rows = c.get(f"/api/projects/{slug}/sessions").json()
    assert rows[0]["provider"] == "codex"
    endpoint = f"/api/projects/{slug}/sessions/codex-{SID}"
    assert c.get(endpoint).json()["provider"] == "codex"
    assert c.get(endpoint + "/messages").json()["messages"]
    canvas = c.get(endpoint + "/canvas")
    assert canvas.status_code == 200, canvas.text
    root = canvas.json()["root"]
    assert root["provider"] == "codex"
    assert root["subtree_usage"] == {"cw": 0, "cr": 60, "r": 90, "w": 30}
    assert root["unpriced_tokens"] == 180
    assert (
        c.get(
            f"/api/projects/another-project/sessions/codex-{SID}/messages"
        ).status_code
        == 404
    )
    assert (
        c.get(
            endpoint + "/canvas", headers={"If-None-Match": canvas.headers["etag"]}
        ).status_code
        == 304
    )


def test_mixed_report_filters_and_month_boundaries(workspace):
    project, path, _ = workspace
    claude = ProjectPaths.for_path(project).project_dir / f"{CLAUDE_SID}.jsonl"
    write(
        claude,
        [
            {
                "type": "assistant",
                "timestamp": TS,
                "uuid": "claude-response",
                "message": {
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ],
    )
    with path.open("a") as fh:
        fh.write(json.dumps(count(tokens(200, 80, 40), "2026-06-01T00:00:00Z")) + "\n")
    from unwind.usage_report import build_month_report

    all_report = build_month_report("2026-05", tz=timezone.utc)
    codex = build_month_report("2026-05", tz=timezone.utc, provider="codex")
    claude_report = build_month_report("2026-05", tz=timezone.utc, provider="claude")
    assert all_report.project_count == 1
    assert all_report.session_count == 2
    assert (
        all_report.total_tokens
        == codex.total_tokens + claude_report.total_tokens
        == 195
    )
    assert all_report.total_cost == claude_report.total_cost > 0
    assert codex.projects[0].unpriced_tokens == 180
    assert (
        build_month_report("2026-06", tz=timezone.utc, provider="codex").total_tokens
        == 60
    )
    c = client()
    assert (
        c.get("/api/usage?month=2026-05&provider=codex").json()["unpriced_tokens"]
        == 180
    )
    assert c.get("/api/usage?provider=unknown").status_code == 400


@pytest.mark.parametrize("include_unknown", [False, True])
def test_codex_priced_usage_matches_canvas_report_and_cli(workspace, include_unknown):
    project, path, rows = workspace
    rows[1]["payload"]["model"] = "gpt-6.1-sol"
    first = {**tokens(100, 40, 20), "cache_write_input_tokens": 10}
    second = {**tokens(150, 60, 30), "cache_write_input_tokens": 10}
    rows = rows[:11] + [
        count(first),
        count(first),
        record("turn_context", {"model": "gpt-5.3-codex"}),
        count(second),
    ]
    if include_unknown:
        rows += [
            record("turn_context", {"model": "unknown-model"}),
            count({**tokens(200, 80, 40), "cache_write_input_tokens": 10}),
        ]
    write(path, rows)
    c = client()
    root = c.get(
        f"/api/projects/{slug_for(project)}/sessions/codex-{SID}/canvas"
    ).json()["root"]
    report = c.get("/api/usage?month=2026-05&provider=codex").json()
    # Request 1: 50 uncached, 40 cached, 10 cache writes, 20 output.
    # Request 2: 30 uncached, 20 cached, 10 output; a different model.
    expected = {"cw": 0.000025, "cr": 0.0000075, "r": 0.0001525, "w": 0.00034}
    assert root["subtree_cost"] == pytest.approx(expected)
    assert report["grand_cost"] == pytest.approx(expected)
    assert report["total_cost"] == pytest.approx(0.000525)
    assert (
        report["unpriced_tokens"]
        == root["unpriced_tokens"]
        == (60 if include_unknown else 0)
    )

    from unwind.cli import app

    result = CliRunner().invoke(
        app, ["usage", "report", "--month", "2026-05", "--provider", "codex", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["total_cost"] == pytest.approx(
        report["total_cost"]
    )


def test_codex_cli_filter_and_slug_resolution(workspace):
    project, _, _ = workspace
    from unwind.cli import app

    result = CliRunner().invoke(
        app,
        [
            "session",
            "list",
            "--project",
            slug_for(project),
            "--harness",
            "codex",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["provider"] == "codex"


def test_new_source_only_needs_adapter_registration(workspace, monkeypatch):
    project, _, _ = workspace
    import unwind.sources as sources
    from unwind.sources.base import SessionFile, SessionSource

    path = project / "example.txt"
    path.write_text("Hello from a new source")
    sid = "example-" + SID
    slug = slug_for(project)

    class ExampleSource(SessionSource):
        id = "example"
        label = "Example"

        def discover_projects(self):
            return [(slug, project)]

        def list_sessions(self, slug_arg, project_dir, *, fresh=False):
            st = path.stat()
            return (
                [SessionFile(sid, path, st.st_mtime, st.st_size, self.id)]
                if slug_arg == slug
                else []
            )

        def owns_session(self, session_id):
            return session_id.startswith("example-")

        def matches_path(self, candidate):
            return candidate == path

        def read_records(self, path):
            yield from self.normalize([{"example": path.read_text()}])

        def matches_record(self, first):
            return False

        def normalize(self, records):
            for rec in records:
                yield {
                    "type": "user",
                    "sessionId": sid,
                    "uuid": "example-user",
                    "timestamp": TS,
                    "message": {"content": rec["example"]},
                }
                yield {
                    "type": "usage",
                    "uuid": "example-usage",
                    "timestamp": TS,
                    "message": {"usage": {"input_tokens": 7}},
                }

    monkeypatch.setattr(sources, "SOURCES", (ExampleSource(), *sources.SOURCES))
    c = client()
    assert {"id": "example", "label": "Example"} in c.get("/api/sources").json()
    rows = c.get(f"/api/projects/{slug}/sessions").json()
    assert any(s["provider"] == "example" for s in rows)
    endpoint = f"/api/projects/{slug}/sessions/{sid}"
    assert (
        c.get(endpoint + "/messages").json()["messages"][0]["text"]
        == "Hello from a new source"
    )
    assert c.get(endpoint + "/canvas").json()["root"]["self_usage"]["r"] == 7
    assert (
        c.get("/api/usage?month=2026-05&provider=example").json()["total_tokens"] == 7
    )
