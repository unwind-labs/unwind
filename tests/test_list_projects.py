"""Tests for the lightweight ``GET /api/projects`` listing.

The endpoint must not parse any JSONL bodies — for a developer with dozens
of projects, eagerly indexing each one previously caused tens of thousands
of file reads per request.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


@pytest.fixture
def client_at_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    import unwind.projects as projects_mod
    import unwind.registry as registry_mod
    import unwind.api.projects as api_projects_mod
    importlib.reload(projects_mod)
    importlib.reload(registry_mod)
    importlib.reload(api_projects_mod)

    app = FastAPI()
    app.include_router(api_projects_mod.router, prefix="/api")
    yield TestClient(app), home, projects_mod, registry_mod, api_projects_mod


def test_list_projects_does_not_parse_jsonls(client_at_home, monkeypatch):
    """The endpoint must rely only on ``os.scandir`` + at most a small
    head-of-file peek for source-path recovery — never the cached
    ``read_records`` parser. A 200-session project would otherwise pay the
    full parse cost per HTTP call."""
    client, home, _projects_mod, _registry_mod, _api = client_at_home

    # Two projects, each with several JSONL files. Project A has cwd in the
    # first record (the synthetic-slug case); B has none (folder is empty
    # of useful records but JSONLs exist).
    slug_a = "-Users-me-proj-a"
    slug_b = "-Users-me-proj-b"
    proj_a = home / ".claude" / "projects" / slug_a
    proj_b = home / ".claude" / "projects" / slug_b
    real_cwd = "/Users/me/proj-a"
    _write_jsonl(proj_a / "sess-1.jsonl", [
        {"type": "user", "cwd": real_cwd, "uuid": "u1",
         "timestamp": "2026-04-24T09:00:00.000Z",
         "message": {"role": "user", "content": "hi"}},
    ])
    _write_jsonl(proj_a / "sess-2.jsonl", [
        {"type": "user", "cwd": real_cwd, "uuid": "u2",
         "timestamp": "2026-04-24T10:00:00.000Z",
         "message": {"role": "user", "content": "yo"}},
    ])
    _write_jsonl(proj_b / "sess-3.jsonl", [
        {"type": "user", "uuid": "u3",
         "timestamp": "2026-04-24T11:00:00.000Z",
         "message": {"role": "user", "content": "no cwd"}},
    ])

    # Track full-file parses. The handler must NOT call ``read_records`` or
    # ``iter_lines`` for any project JSONL — only at most a head-of-file
    # ``open().readline()`` peek for synthetic-slug cwd recovery.
    import unwind.jsonl as jsonl_mod
    parse_calls: list[Path] = []
    real_iter = jsonl_mod.iter_lines
    real_read = jsonl_mod.read_records

    def tracking_iter(path):  # type: ignore[no-untyped-def]
        parse_calls.append(Path(path))
        yield from real_iter(path)

    def tracking_read(path):  # type: ignore[no-untyped-def]
        parse_calls.append(Path(path))
        return real_read(path)

    monkeypatch.setattr(jsonl_mod, "iter_lines", tracking_iter)
    monkeypatch.setattr(jsonl_mod, "read_records", tracking_read)

    resp = client.get("/api/projects")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert parse_calls == [], (
        f"list_projects parsed JSONLs: {parse_calls}. The endpoint must "
        "stay stat-only / head-of-file."
    )

    by_slug = {p["slug"]: p for p in payload}
    assert by_slug[slug_a]["session_count"] == 2
    assert by_slug[slug_b]["session_count"] == 1
    # Synthetic-slug projects recover real cwd via head-of-file peek.
    assert by_slug[slug_a]["source_path"] == real_cwd
    # Both projects have last_activity populated from mtime fallback.
    assert by_slug[slug_a]["last_activity"] is not None
    assert by_slug[slug_b]["last_activity"] is not None


def test_list_projects_sorts_by_last_activity_desc(client_at_home):
    client, home, _pm, _rm, _api = client_at_home
    older = home / ".claude" / "projects" / "-Users-me-older"
    newer = home / ".claude" / "projects" / "-Users-me-newer"
    _write_jsonl(older / "a.jsonl", [{"type": "user", "uuid": "a"}])
    _write_jsonl(newer / "b.jsonl", [{"type": "user", "uuid": "b"}])
    import os
    os.utime(older / "a.jsonl", (1_700_000_000, 1_700_000_000))
    os.utime(newer / "b.jsonl", (1_800_000_000, 1_800_000_000))

    payload = client.get("/api/projects").json()
    slugs = [p["slug"] for p in payload]
    assert slugs.index("-Users-me-newer") < slugs.index("-Users-me-older")


def test_list_projects_handles_empty_jsonl_dir(client_at_home):
    """A registered project with no JSONLs yet shows count 0, last_activity null."""
    client, home, projects_mod, registry_mod, _api = client_at_home
    real = home / "work" / "empty-proj"
    real.mkdir(parents=True)
    slug = projects_mod.slug_for(real)
    (home / ".claude" / "projects" / slug).mkdir(parents=True)
    registry_mod.register_default_project(str(real))

    payload = client.get("/api/projects").json()
    row = next(p for p in payload if p["slug"] == slug)
    assert row["session_count"] == 0
    assert row["last_activity"] is None
    # Real registered source survives unchanged.
    assert row["source_path"] == str(real)
