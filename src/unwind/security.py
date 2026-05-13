"""Shared HTTP/WS origin policy.

CORS already gates cross-origin XHR/fetch in :mod:`unwind.server`, but the
WebSocket handshake bypasses CORS. We reuse this allow-list for both surfaces
so policy lives in one place.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


DEFAULT_DEV_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def allowed_origins() -> list[str]:
    """Explicit cross-origin allow-list. Same-origin is always permitted."""
    extra = os.environ.get("UNWIND_ALLOWED_ORIGINS", "").strip()
    if not extra:
        return list(DEFAULT_DEV_ORIGINS)
    return [o.strip() for o in extra.split(",") if o.strip()]


def _normalise(origin: str) -> tuple[str, str, int] | None:
    parts = urlsplit(origin)
    if not parts.scheme or not parts.hostname:
        return None
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (parts.scheme, parts.hostname.lower(), port)


def is_same_origin(origin: str, host_header: str | None, *, secure: bool) -> bool:
    """True if ``origin`` matches the request's own host:port."""
    if not origin or not host_header:
        return False
    o = _normalise(origin)
    if o is None:
        return False
    # host_header is "host" or "host:port"; no scheme.
    h_scheme = "https" if secure else "http"
    h = _normalise(f"{h_scheme}://{host_header}")
    if h is None:
        return False
    return o == h


def is_origin_allowed(origin: str | None, host_header: str | None, *, secure: bool) -> bool:
    """Allow if Origin is missing (non-browser client), same-origin, or whitelisted."""
    if not origin:
        # Non-browser clients (curl, python-websockets in tests) omit Origin.
        # Browsers always send it, so absence cannot be forged from a browser.
        return True
    if is_same_origin(origin, host_header, secure=secure):
        return True
    return origin in allowed_origins()


def require_trusted_origin(request: Request) -> None:
    """FastAPI dependency: 403 when a state-changing request is cross-origin.

    Browsers always send the Origin header on POST/PUT/DELETE, so this is a
    reliable CSRF / cross-origin gate for endpoints that mutate server state.
    """
    headers = request.headers
    origin = headers.get("origin")
    host = headers.get("host")
    secure = request.url.scheme == "https"
    if not is_origin_allowed(origin, host, secure=secure):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
