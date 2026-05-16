"""Per-request memoization for expensive per-slug helpers.

A handful of endpoints touch the same derived state more than once per
request (e.g. ``_active_session_for_project`` is computed once for
``list_sessions`` and again inside ``_compute_session_status`` if the
caller forgot to pass it through). A request-scoped memo collapses the
redundancy without changing the registry-level caching story — registry
caches survive across requests; this one dies with the request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Request


@dataclass
class RequestState:
    _memo: dict[tuple, Any] = field(default_factory=dict)

    def memoize(self, key: tuple, factory: Callable[[], Any]) -> Any:
        """Return the cached value for ``key``, computing via ``factory`` on miss.

        Keys should be hashable tuples that uniquely identify the derived
        value within one request — typically ``(name, slug, ...)``.
        """
        try:
            return self._memo[key]
        except KeyError:
            value = factory()
            self._memo[key] = value
            return value


def get_request_state(request: Request) -> RequestState:
    """FastAPI dependency. One ``RequestState`` per HTTP request."""
    rs = getattr(request.state, "unwind_request_state", None)
    if rs is None:
        rs = RequestState()
        request.state.unwind_request_state = rs
    return rs
