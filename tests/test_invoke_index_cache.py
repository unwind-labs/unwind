"""invoke_index caching: shared scan across requests, invalidates on mtime change."""
from __future__ import annotations

import json
from pathlib import Path

import unwind.registry as registry
from unwind import spawns


def _write_session_with_callstack(proj_dir: Path, sid: str, invoke_id: str) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    rec_assistant = {
        "uuid": "u1",
        "type": "assistant",
        "sessionId": sid,
        "timestamp": "2026-05-13T00:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "tu1",
                "name": "mcp__plugin_callstack_call__call",
                "input": {},
            }],
        },
    }
    rec_user = {
        "uuid": "u2",
        "type": "user",
        "sessionId": sid,
        "timestamp": "2026-05-13T00:00:01.000Z",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tu1",
                "content": [{"type": "text", "text": f'{{"invoke_id": "{invoke_id}"}}'}],
            }],
        },
    }
    (proj_dir / f"{sid}.jsonl").write_text(
        json.dumps(rec_assistant) + "\n" + json.dumps(rec_user) + "\n"
    )


def test_invoke_index_cached_across_calls(tmp_path, monkeypatch):
    """Second call hits the cache and does not rescan JSONLs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    slug = "-test-proj-1"
    proj_dir = tmp_path / ".claude" / "projects" / slug
    _write_session_with_callstack(proj_dir, "11111111-1111-1111-1111-111111111111", "INV-A")

    calls: list[Path] = []
    real = spawns.compute_invoke_index_for_project

    def spy(p: Path) -> dict[str, list[str]]:
        calls.append(p)
        return real(p)

    monkeypatch.setattr(registry, "compute_invoke_index_for_project", spy, raising=False)
    # Reload-safe: registry imports the symbol inside invoke_index_for_slug
    # via local import; patch the spawns attr instead.
    monkeypatch.setattr(spawns, "compute_invoke_index_for_project", spy)

    registry.forget_slug(slug)

    first = registry.invoke_index_for_slug(slug, proj_dir)
    second = registry.invoke_index_for_slug(slug, proj_dir)

    assert first == second == {"INV-A": ["11111111-1111-1111-1111-111111111111"]}
    assert len(calls) == 1, "second call must hit the cache"


def test_invoke_index_invalidates_when_jsonl_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    slug = "-test-proj-2"
    proj_dir = tmp_path / ".claude" / "projects" / slug
    _write_session_with_callstack(proj_dir, "22222222-2222-2222-2222-222222222222", "INV-A")
    registry.forget_slug(slug)

    first = registry.invoke_index_for_slug(slug, proj_dir)
    assert first == {"INV-A": ["22222222-2222-2222-2222-222222222222"]}

    # Add a second session with a new invoke_id.
    _write_session_with_callstack(proj_dir, "33333333-3333-3333-3333-333333333333", "INV-B")
    # bump mtime / size so signature differs (writes already do this).
    second = registry.invoke_index_for_slug(slug, proj_dir)
    assert second == {
        "INV-A": ["22222222-2222-2222-2222-222222222222"],
        "INV-B": ["33333333-3333-3333-3333-333333333333"],
    }


def test_resolver_uses_injected_index_no_rescan(tmp_path, monkeypatch):
    """SpawnResolver constructed with invoke_index=... must NOT rescan."""
    monkeypatch.setenv("HOME", str(tmp_path))
    slug = "-test-proj-3"
    proj_dir = tmp_path / ".claude" / "projects" / slug
    _write_session_with_callstack(proj_dir, "44444444-4444-4444-4444-444444444444", "INV-Z")
    registry.forget_slug(slug)

    calls: list[Path] = []
    real = spawns.compute_invoke_index_for_project

    def spy(p: Path) -> dict[str, list[str]]:
        calls.append(p)
        return real(p)

    monkeypatch.setattr(spawns, "compute_invoke_index_for_project", spy)

    resolver = registry.spawn_resolver_for_slug(slug)
    # spawn_resolver_for_slug already populated the registry cache.
    pre_calls = len(calls)
    # Force the resolver to consult the invoke index.
    resolver._invoke_id_to_parent_session()
    resolver._invoke_id_to_parent_session()
    assert len(calls) == pre_calls, "resolver must reuse injected index"
