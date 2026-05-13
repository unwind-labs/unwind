"""Shared HTTP/WS origin policy.

CORS already gates cross-origin XHR/fetch in :mod:`unwind.server`, but the
WebSocket handshake bypasses CORS. We reuse this allow-list for both surfaces
so policy lives in one place.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit


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
