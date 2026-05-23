"""Shared HTTP/WS origin policy.

CORS already gates cross-origin XHR/fetch in :mod:`unwind.server`, but the
WebSocket handshake bypasses CORS. We reuse this allow-list for both surfaces
so policy lives in one place.
"""
from __future__ import annotations

import hmac
import re
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import HTTPException, Path, Request

from .settings import get_settings


# Claude Code's slug rule: any char outside [A-Za-z0-9-] is replaced with '-'.
# We anchor to reject path traversal segments like "..".
SLUG_PATTERN = r"^[A-Za-z0-9-]+$"
# Standard UUID v1–v5 shape for session JSONL filenames, OR the synthetic
# ``agent-<hex>`` id used to address an in-session subagent trace (see
# unwind.subagents.SUBAGENT_PREFIX).
SESSION_ID_PATTERN = (
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|agent-[0-9a-fA-F]+)$"
)

_SLUG_RE = re.compile(SLUG_PATTERN)
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)


SlugPath = Annotated[str, Path(pattern=SLUG_PATTERN, max_length=512)]
SessionIdPath = Annotated[str, Path(pattern=SESSION_ID_PATTERN)]


def is_valid_slug(s: str) -> bool:
    return bool(_SLUG_RE.match(s))


def is_valid_session_id(s: str) -> bool:
    return bool(_SESSION_ID_RE.match(s))


def allowed_origins() -> list[str]:
    """Explicit cross-origin allow-list. Same-origin is always permitted."""
    return list(get_settings().allowed_origins)


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


def is_origin_allowed(
    origin: str | None,
    host_header: str | None,
    *,
    secure: bool,
    allow_missing_origin: bool = True,
) -> bool:
    """Allow if same-origin or whitelisted.

    ``allow_missing_origin``: when True (default), a missing Origin header is
    permitted — fine for read-only GETs and WS where non-browser clients
    legitimately omit it. State-changing endpoints pass False so any local
    non-browser process that bypasses the Origin-based CSRF guard is rejected.
    """
    if not origin:
        return allow_missing_origin
    if is_same_origin(origin, host_header, secure=secure):
        return True
    return origin in allowed_origins()


def require_trusted_origin(request: Request) -> None:
    """FastAPI dependency: 403 when a state-changing request is cross-origin.

    Browsers always send the Origin header on POST/PUT/DELETE, so this is a
    reliable CSRF / cross-origin gate for endpoints that mutate server state.
    Missing Origin is treated as untrusted: a local non-browser process could
    otherwise bypass this guard.
    """
    headers = request.headers
    origin = headers.get("origin")
    host = headers.get("host")
    secure = request.url.scheme == "https"
    if not is_origin_allowed(
        origin, host, secure=secure, allow_missing_origin=False
    ):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


def auth_token() -> str | None:
    """Configured bearer token, or None when auth is disabled (loopback default)."""
    return get_settings().auth_token


def is_token_valid(presented: str | None) -> bool:
    """Constant-time compare ``presented`` against the configured token."""
    expected = auth_token()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def extract_bearer(authorization: str | None) -> str | None:
    """Parse an ``Authorization: Bearer <token>`` header value."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
