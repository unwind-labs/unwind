"""OpenAPI surface is off by default."""
from __future__ import annotations

from fastapi.testclient import TestClient

from unwind.server import create_app


def test_docs_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNWIND_DOCS", raising=False)
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/docs").status_code in (404, 405)
        assert c.get("/api/openapi.json").status_code == 404


def test_docs_on_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UNWIND_DOCS", "1")
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/docs").status_code == 200
        assert c.get("/api/openapi.json").status_code == 200
