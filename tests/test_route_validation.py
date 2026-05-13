"""Path params (slug, session_id) are rejected when malformed."""
from __future__ import annotations

from fastapi.testclient import TestClient

from unwind.server import create_app


def test_slug_with_dots_rejected(tmp_path, monkeypatch):
    """Slugs containing '.' don't match [A-Za-z0-9-]+ so the validator
    rejects path-traversal attempts that survive Starlette's URL normalisation."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/projects/...secret/sessions")
        assert r.status_code == 422


def test_slug_with_special_chars_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        # Spaces, $, etc. — anything outside [A-Za-z0-9-]
        r = c.get("/api/projects/foo$bar/sessions")
        assert r.status_code == 422


def test_session_id_not_uuid_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/projects/valid-slug/sessions/not-a-uuid/messages")
        assert r.status_code == 422


def test_session_id_uuid_accepted(tmp_path, monkeypatch):
    """A valid UUID passes validation (404/500 from the handler is fine — we
    only care that we got past the validator)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        r = c.get(
            "/api/projects/valid-slug/sessions/"
            "12345678-1234-1234-1234-123456789abc/messages"
        )
        assert r.status_code != 422


def test_valid_slug_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        # Real slug shape from Claude Code's slug_for(): -Users-amolk-work-foo
        r = c.get("/api/projects/-Users-amolk-work-foo/sessions")
        # Validator passed; the handler may return [] for an empty project dir.
        assert r.status_code in (200, 404)
