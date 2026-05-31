"""Canonical session/window status vocabulary.

One enum, one merge function, one boundary translator. Replaces three
divergent implementations that previously lived in ``callstack.py``,
``canvas_tree.py``, and ``messages.py`` with different priority orders
("yielded > running" in one, "live > yield" in another) and a boolean
collapse in the third.

The canonical priority is **live > yield > failed > done**. Rationale:
when work is still happening anywhere below an ancestor, the user wants
to see that the ancestor is live — a yielded sibling shouldn't outrank
an actively-running descendant. This resolves the prior disagreement
between ``aggregate_status_for_session`` (which preferred yielded) and
``_aggregate_subtree_status`` (which preferred live).

**Terminal-ancestor wall.** The priority above governs how a subtree's
statuses combine, but it does NOT mean a descendant's status always
escalates upward. A separate invariant gates the merge: when an
ancestor's OWN status is terminal (``failed`` or ``done``), its
descendants do not contribute to its aggregate — the ancestor's
terminal status is authoritative. A returned or failed invocation
cannot have a genuinely live descendant: the runtime gates a call's
return on its children returning first, so a child still marked
``running`` under a terminal parent is a stale ``report.yaml`` left
behind when the parent crashed (the runtime never wrote the child's
terminal envelope) — not live work. Without the wall, that stale
``running`` resurrects the parent to ``live`` and its CALL row pulses
forever / its session-list dot stays amber. ``yield`` is deliberately
NOT a wall: a yielded parent waiting on user input can legitimately sit
above still-running descendants, so escalation still applies there.

The wall is enforced in ``CallstackIndex.aggregate_status_for_session``
(the pure-report path), not inside :func:`merge`, which stays a pure
highest-priority-wins helper. ``canvas_tree._finalize_subtree`` does NOT
apply the wall and deliberately so: it gates each window's status on real
process liveness first, so a stale ``running`` report with no live process
is already downgraded to ``done`` and never escalates, while a ``live``
window there is genuinely live and SHOULD pulse its ancestors' rails.
"""
from __future__ import annotations

from typing import Iterable, Literal, Optional


Status = Literal["done", "yield", "live", "failed"]


# Priority order: live > yield > failed > done. See module docstring.
_PRIORITY: dict[str, int] = {"done": 0, "failed": 1, "yield": 2, "live": 3}


# Map raw status strings (callstack report.yaml + canonical) to canonical.
# Keys are case-folded at the call site.
_CALLSTACK_MAP: dict[str, Status] = {
    "complete": "done",
    "done": "done",
    "yielded": "yield",
    "yield": "yield",
    "running": "live",
    "in_progress": "live",
    "pending": "live",
    "live": "live",
    "failed": "failed",
    "error": "failed",
    # ``abandoned`` = the child process was killed mid-flight (e.g.
    # parent crashed before the child finished its turn — the runtime
    # writes this status with an "abandoned at shutdown (pid=…)" error).
    # The session never returned a result; treating it as ``done`` would
    # show a misleading green check on a clearly-broken run.
    "abandoned": "failed",
    # ``mixed`` = top-level report rollup where at least one child failed
    # while others completed. The overall invocation didn't fully
    # succeed, so canonicalise to ``failed`` rather than ``done``.
    "mixed": "failed",
}


def from_raw(raw: Optional[str]) -> Optional[Status]:
    """Translate a raw status string (from callstack ``report.yaml``,
    canvas-tree internals, or anywhere else) to canonical. Returns
    ``None`` for unknown / empty input so callers can distinguish
    "no signal" from "explicit done".
    """
    if not raw:
        return None
    return _CALLSTACK_MAP.get(raw.lower())


def merge(statuses: Iterable[Optional[Status]]) -> Status:
    """Return the highest-priority status across ``statuses``.

    ``None`` entries are skipped. Priority is live > yield > failed >
    done; an empty / all-None iterable returns ``"done"``.
    """
    best: Status = "done"
    best_p = _PRIORITY["done"]
    for s in statuses:
        if s is None:
            continue
        p = _PRIORITY.get(s, 0)
        if p > best_p:
            best, best_p = s, p
    return best


def is_done(s: Optional[Status]) -> Optional[bool]:
    """Map a canonical status to the spawn-row "done" flag.

    From the PARENT's perspective, a CALL row drops its in-progress
    dots the moment the child returns control — including ``yield``
    (the child is waiting for user input) and ``failed``. Only ``live``
    is genuinely in-flight. Returns ``None`` when the status itself is
    unknown so callers can fall back to a tool_result-arrival check.
    """
    if s is None:
        return None
    return s != "live"
