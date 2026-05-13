"""WebSocket handshake enforces Origin policy."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from unwind.server import create_app


def test_ws_rejects_unknown_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNWIND_ALLOWED_ORIGINS", raising=False)
    app = create_app()
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/api/ws?project=p",
                headers={"origin": "http://evil.example"},
            ):
                pass
        assert exc.value.code == 1008


def test_ws_accepts_no_origin(tmp_path, monkeypatch):
    """CLI / curl / python-websockets clients don't send Origin."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects" / "p").mkdir(parents=True)
    app = create_app()
    with TestClient(app) as c:
        with c.websocket_connect("/api/ws?project=p") as ws:
            msg = ws.receive_json()
            assert msg.get("type") == "ready"


def test_ws_accepts_dev_vite_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects" / "p").mkdir(parents=True)
    app = create_app()
    with TestClient(app) as c:
        with c.websocket_connect(
            "/api/ws?project=p",
            headers={"origin": "http://localhost:5173"},
        ) as ws:
            msg = ws.receive_json()
            assert msg.get("type") == "ready"
