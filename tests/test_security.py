"""Origin policy used by HTTP CORS and the WebSocket handshake."""
from __future__ import annotations

from unwind.security import (
    allowed_origins,
    is_origin_allowed,
    is_same_origin,
)


def test_default_allow_list_has_dev_vite():
    origins = allowed_origins()
    assert "http://localhost:5173" in origins
    assert "http://127.0.0.1:5173" in origins


def test_env_override(monkeypatch):
    monkeypatch.setenv("UNWIND_ALLOWED_ORIGINS", "https://app.example , https://other.example")
    assert allowed_origins() == ["https://app.example", "https://other.example"]


def test_same_origin_matches():
    assert is_same_origin("http://127.0.0.1:8765", "127.0.0.1:8765", secure=False)


def test_same_origin_default_port():
    assert is_same_origin("http://example.com", "example.com", secure=False)


def test_same_origin_scheme_mismatch_rejected():
    # browser sent http origin but request claims wss
    assert not is_same_origin("http://127.0.0.1:8765", "127.0.0.1:8765", secure=True)


def test_origin_allow_missing_origin_is_permitted():
    # Non-browser clients omit Origin; allow so tests / curl / CLI work.
    assert is_origin_allowed(None, "127.0.0.1:8765", secure=False)


def test_origin_allow_same_origin():
    assert is_origin_allowed(
        "http://127.0.0.1:8765", "127.0.0.1:8765", secure=False
    )


def test_origin_allow_whitelisted_dev():
    assert is_origin_allowed(
        "http://localhost:5173", "127.0.0.1:8765", secure=False
    )


def test_origin_reject_unknown():
    assert not is_origin_allowed(
        "http://evil.example", "127.0.0.1:8765", secure=False
    )
