"""Per-session JSONL scan: the one-pass parser feeding every downstream consumer.

Every consumer that asks "what's the latest envelope state of this session?"
or "what divergence label did the runtime write for this fork?" or "how
many tokens did this session burn?" reads from a single mtime-cached
:class:`SessionScan` produced by :func:`scan_session`. The canvas tree
turns those scans into windows; the spawn resolver reads
``last_envelope_kind`` to infer fork status; the fork detector reads
``queue_op_starting_task`` for divergence labels.

``CanvasTreeBuilder`` is just a project-scoped cache over
``scan_session`` — repeated requests for the same session in the same
process reuse the parsed result until the JSONL's mtime/size changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

from ._cache import PathCache
from .jsonl import (
    RETURN_RE as _RETURN_RE,
    YIELD_RE as _YIELD_RE,
    _text_blocks,
    extract_assistant_text as _extract_assistant_text,
    is_tool_result_record as _is_tool_result_record,
    iter_lines,
    parse_ts as _parse_ts,
)


_STARTING_TASK_RE = re.compile(
    r"##\s*Starting\s+Task[^\n]*\n+\s*(\S[^\n]*)", re.IGNORECASE
)
# How many of the JSONL's leading user messages SessionScan keeps for
# divergence-text fallback. Enough to find a non-inherited prompt
# without bloating the per-scan memory footprint.
_USER_PREFIX_CAP = 10


class UsageEvent(NamedTuple):
    """One assistant turn's ``message.usage`` counters + the model that
    produced them. Fields mirror Anthropic's wire format:
    ``cw=cache_creation_input_tokens``, ``cr=cache_read_input_tokens``,
    ``r=input_tokens``, ``w=output_tokens``.

    ``uuid`` is the originating assistant record's uuid. Used to detect
    events copied into a fork's JSONL from its parent (``claude
    --fork-session`` mirrors the parent transcript verbatim, including
    each assistant turn's usage block — so without this we'd count the
    parent's tokens once per fork).
    """

    ts: Optional[datetime]
    model: Optional[str]
    cw: int
    cr: int
    r: int
    w: int
    uuid: Optional[str] = None


@dataclass
class SessionScan:
    """Lightweight per-session summary used to build canvas windows."""

    session_id: str
    path: Path
    mtime: float = 0.0
    size: int = 0
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None
    yields: list[datetime] = field(default_factory=list)
    # True if the session's most recent meaningful event is Claude
    # finishing a turn (``system/stop_hook_summary``) with no user
    # reply since — i.e. Claude is currently waiting for input. This
    # is the "interactive yield" signal that callstack-yield envelopes
    # don't catch (and that ``away_summary`` recaps incorrectly
    # implied).
    at_user_prompt: bool = False
    # True iff the LAST callstack envelope seen in an assistant message
    # was a ``{"op":"return"}``. Used to override a stale callstack
    # ``report.yaml`` status of ``running`` for a child whose JSONL
    # shows it already returned (the runtime sometimes fails to update
    # the report). Earlier returns followed by a later yield/run flip
    # this back to False — the LAST envelope is the terminal state.
    has_returned: bool = False
    # Persistent terminal-envelope tracking (NEVER reset on intervening
    # events). ``last_envelope_kind`` is the kind of the LAST callstack
    # envelope ever seen ("return" | "yield" | None), and
    # ``last_envelope_ts`` is its timestamp. Used by ``SpawnResolver`` to
    # infer fork-spawn status without re-walking the JSONL — a yield
    # followed by a resume followed by a return surfaces as ``complete``
    # at the latest envelope, because that's the terminal state.
    last_envelope_kind: Optional[str] = None
    last_envelope_ts: Optional[datetime] = None
    # For fork-detected sessions: the assigned task label captured from
    # the FIRST ``queue-operation`` record whose content matches
    # ``## Starting Task ... /task-X``. This is the callstack runtime's
    # primary divergence signal — when present, ForkDetector returns it
    # verbatim as the spawn label.
    queue_op_starting_task: Optional[str] = None
    # Fallback divergence source: the first few ``user``-record (uuid,
    # text) pairs in the JSONL. ForkDetector filters these against the
    # family root's uuid set to find the first message that ISN'T
    # inherited from the parent (i.e. the divergent prompt). Capped so
    # that long sessions don't bloat the cache.
    first_user_texts: list[tuple[str, str]] = field(default_factory=list)
    # Per-assistant-message token usage events. ``model`` is the
    # assistant message's ``message.model`` string, kept per-event so
    # cost can be priced at the rate of whichever model that specific
    # turn ran against (and so the attribution pass doesn't need a
    # separate cost array shadowing this one).
    usage_events: list[UsageEvent] = field(default_factory=list)


def scan_session(path: Path) -> SessionScan:
    """Walk a session's JSONL once; collect start, end, yield timestamps,
    and the at-user-prompt state (Claude finished its turn, awaiting reply).
    """
    try:
        st = path.stat()
    except OSError:
        return SessionScan(session_id=path.stem, path=path)
    scan = SessionScan(
        session_id=path.stem,
        path=path,
        mtime=st.st_mtime,
        size=st.st_size,
    )
    # Two derived flags driven by the same stream:
    #   at_user_prompt: last meaningful event = yield envelope OR stop_hook
    #   has_returned:   last assistant envelope = return
    # Both reset together on any "real" event (assistant turn without an
    # envelope, real user reply). Tool_result user records and unrelated
    # system subtypes don't count as events — they leave state alone.
    at_user_prompt = False
    has_returned = False
    # A single assistant turn is written to the JSONL as several records —
    # one per content block (thinking / text / each tool_use) — and every
    # one of those records repeats the SAME ``message.usage``. Count each
    # turn once, keyed on the stable assistant message id (``requestId`` as
    # a fallback), or a turn with N tool calls gets its tokens summed N+1
    # times over.
    seen_turn_ids: set[str] = set()
    for rec in iter_lines(path):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            if scan.start_ts is None:
                scan.start_ts = ts
            scan.end_ts = ts
        rtype = rec.get("type")
        if rtype == "assistant":
            msg = rec.get("message")
            if isinstance(msg, dict):
                u = msg.get("usage")
                if isinstance(u, dict):
                    cw = int(u.get("cache_creation_input_tokens") or 0)
                    cr = int(u.get("cache_read_input_tokens") or 0)
                    r_in = int(u.get("input_tokens") or 0)
                    w_out = int(u.get("output_tokens") or 0)
                    turn_id = msg.get("id") or rec.get("requestId")
                    already_seen = (
                        isinstance(turn_id, str) and turn_id in seen_turn_ids
                    )
                    if (cw or cr or r_in or w_out) and not already_seen:
                        if isinstance(turn_id, str):
                            seen_turn_ids.add(turn_id)
                        m = msg.get("model")
                        model = m if isinstance(m, str) else None
                        rec_uuid = rec.get("uuid")
                        scan.usage_events.append(
                            UsageEvent(
                                ts, model, cw, cr, r_in, w_out,
                                rec_uuid if isinstance(rec_uuid, str) else None,
                            )
                        )
            text = _extract_assistant_text(rec)
            if text and _YIELD_RE.search(text):
                if ts is not None:
                    scan.yields.append(ts)
                at_user_prompt, has_returned = True, False
                scan.last_envelope_kind = "yield"
                scan.last_envelope_ts = ts
            elif text and _RETURN_RE.search(text):
                at_user_prompt, has_returned = False, True
                scan.last_envelope_kind = "return"
                scan.last_envelope_ts = ts
            else:
                at_user_prompt, has_returned = False, False
        elif rtype == "user" and not _is_tool_result_record(rec):
            # Tool results leave state alone (mid-turn tool processing);
            # real user replies reset both flags.
            at_user_prompt, has_returned = False, False
            u = rec.get("uuid")
            if (
                isinstance(u, str)
                and len(scan.first_user_texts) < _USER_PREFIX_CAP
            ):
                scan.first_user_texts.append(
                    (u, _text_blocks(rec.get("message"), " ") or "")
                )
        elif rtype == "queue-operation":
            if scan.queue_op_starting_task is None:
                content = rec.get("content")
                if isinstance(content, str):
                    m = _STARTING_TASK_RE.search(content)
                    if m:
                        scan.queue_op_starting_task = m.group(1).strip()
        elif rtype == "system" and rec.get("subtype") == "stop_hook_summary":
            # End-of-turn marker. Sets at_user_prompt but doesn't touch
            # has_returned — a stop hook after a return envelope mustn't
            # un-flag the return.
            at_user_prompt = True
    scan.at_user_prompt = at_user_prompt
    scan.has_returned = has_returned
    return scan


class CanvasTreeBuilder:
    """Per-project scan cache. Reuses scans across canvas requests.

    Re-scans a session only when its JSONL's mtime/size changes.
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self._cache = PathCache(scan_session)

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    def get_scan(self, session_id: str) -> SessionScan:
        path = self._project_dir / f"{session_id}.jsonl"
        return self._cache.get(path)
