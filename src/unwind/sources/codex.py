"""Read local Codex rollouts without modifying the source logs.

The adapter emits the conversation blocks consumed by unwind's existing
message and usage scanners. Event mirrors are deliberately ignored: response
items are the transcript; cumulative token counters are the usage source.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .base import SessionFile, SessionSource

PREFIX = "codex-"
_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


@dataclass(frozen=True)
class CodexSession:
    sid: str
    path: Path
    cwd: str
    slug: str
    mtime: float
    size: int


_lock = threading.Lock()
_cache: dict[Path, tuple[float, tuple[CodexSession, ...]]] = {}
_metadata: dict[Path, tuple[int, int, dict]] = {}


def sessions(
    slug: str | None = None, *, fresh: bool = False
) -> tuple[CodexSession, ...]:
    """Discover active and archived sessions, deduplicated by session id.

    Only metadata is read during discovery. A short TTL amortizes directory
    walks across projects; changed files invalidate their metadata cache.
    """
    from ..projects import slug_for

    home = codex_home()
    with _lock:
        cached = _cache.get(home)
        if not fresh and cached and time.monotonic() - cached[0] < 1:
            entries = cached[1]
        else:
            found: dict[str, CodexSession] = {}
            paths_seen: set[Path] = set()
            for folder in ("archived_sessions", "sessions"):
                for path in sorted((home / folder).glob("**/*.jsonl")):
                    try:
                        st = path.stat()
                        paths_seen.add(path)
                        prior = _metadata.get(path)
                        if prior and prior[:2] == (st.st_mtime_ns, st.st_size):
                            meta = prior[2]
                        else:
                            meta = {}
                            with path.open(encoding="utf-8", errors="replace") as fh:
                                for _ in range(10):
                                    line = fh.readline()
                                    if not line:
                                        break
                                    try:
                                        rec = json.loads(line)
                                    except ValueError:
                                        continue
                                    if (
                                        isinstance(rec, dict)
                                        and rec.get("type") == "session_meta"
                                    ):
                                        meta = rec.get("payload") or {}
                                        break
                            if not isinstance(meta, dict):
                                meta = {}
                            _metadata[path] = (st.st_mtime_ns, st.st_size, meta)
                        sid, cwd = meta.get("id"), meta.get("cwd")
                        if not isinstance(sid, str) or not _UUID.fullmatch(sid):
                            continue
                        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                            continue
                        entry = CodexSession(
                            PREFIX + sid,
                            path,
                            cwd,
                            slug_for(cwd),
                            st.st_mtime,
                            st.st_size,
                        )
                        found[entry.sid] = entry
                    except OSError:
                        continue
            for path in list(_metadata):
                if path.is_relative_to(home) and path not in paths_seen:
                    _metadata.pop(path, None)
            entries = tuple(found.values())
            _cache[home] = (time.monotonic(), entries)
    return tuple(e for e in entries if slug is None or e.slug == slug)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b["text"]
        for b in content
        if isinstance(b, dict) and isinstance(b.get("text"), str)
    )


def normalize(records: Iterable[dict]) -> Iterator[dict]:
    """Normalize rollouts, preserving stable ids across file appends.

    Cached input is included in input_tokens; reasoning output is already in
    output_tokens. Neither should be added a second time. Repeated cumulative
    totals (including rate-limit-only events) contribute no usage.
    """
    sid = ""
    model = "codex-unknown"
    cwd = None
    totals: dict[str, int] = {}
    seen_items: set[str] = set()
    for n, rec in enumerate(records):
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = rec.get("type")
        base = {
            "timestamp": rec.get("timestamp"),
            "sessionId": sid,
            "uuid": f"codex-record-{n}",
            "provider": "codex",
            "cwd": cwd,
        }
        if kind == "session_meta":
            sid = PREFIX + str(payload.get("id", ""))
            cwd = payload.get("cwd")
            git = payload.get("git") or {}
            yield {
                **base,
                "type": "session-meta",
                "sessionId": sid,
                "cwd": cwd,
                "gitBranch": git.get("branch") if isinstance(git, dict) else None,
            }
        elif kind == "turn_context":
            model = payload.get("model") or model
        elif kind == "response_item":
            item_type = payload.get("type")
            item_id = payload.get("id")
            if item_id:
                if item_id in seen_items:
                    continue
                seen_items.add(item_id)
            base["uuid"] = item_id or base["uuid"]
            content = []
            role = "assistant"
            if item_type == "message":
                role = payload.get("role", "system")
                if role not in ("user", "assistant"):
                    role = "system"
                text = _text(payload.get("content"))
                # These are context injections, not the user's session title.
                if role == "user" and text.lstrip().startswith(
                    (
                        "# AGENTS.md instructions",
                        "<environment_context>",
                        "<permissions instructions>",
                    )
                ):
                    role = "system"
                content = [{"type": "text", "text": text}]
            elif item_type == "reasoning":
                text = _text(payload.get("summary"))
                if not text:
                    continue  # encrypted content isn't readable reasoning
                content = [{"type": "thinking", "thinking": text}]
            elif item_type in ("function_call", "custom_tool_call", "local_shell_call"):
                args = payload.get(
                    "arguments", payload.get("input", payload.get("action", {}))
                )
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                content = [
                    {
                        "type": "tool_use",
                        "id": payload.get("call_id") or item_id,
                        "name": payload.get("name", "shell"),
                        "input": args,
                    }
                ]
            elif item_type in (
                "function_call_output",
                "custom_tool_call_output",
                "local_shell_call_output",
            ):
                role = "user"
                output = payload.get("output", "")
                if not isinstance(output, (str, list)):
                    output = json.dumps(output, ensure_ascii=False)
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": payload.get("call_id"),
                        "content": output,
                    }
                ]
            else:
                continue
            yield {
                **base,
                "type": role,
                "content": _text(content),
                "message": {
                    "id": base["uuid"],
                    "role": role,
                    "model": model,
                    "content": content,
                },
            }
        elif kind in ("event_msg", "token_usage_record"):
            event = payload.get("type")
            if event in ("task_complete", "task_completed", "turn_aborted"):
                yield {**base, "type": "system", "subtype": "stop_hook_summary"}
                continue
            if kind == "token_usage_record":
                total = payload.get("thread_token_usage")
            elif event == "token_count":
                info = payload.get("info")
                total = (
                    info.get("total_token_usage") if isinstance(info, dict) else None
                )
            else:
                continue
            if not isinstance(total, dict):
                continue
            keys = (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
            )
            current = {
                k: max(0, total.get(k, 0)) if isinstance(total.get(k, 0), int) else 0
                for k in keys
            }
            # A lower cumulative total can be a stale mirror or a compacted
            # context baseline. Never count it as new usage.
            delta = {k: max(0, current[k] - totals.get(k, 0)) for k in keys}
            totals = {k: max(current[k], totals.get(k, 0)) for k in keys}
            cr = min(delta["cached_input_tokens"], delta["input_tokens"])
            cw = min(delta["cache_write_input_tokens"], delta["input_tokens"] - cr)
            if not any(delta.values()):
                continue
            yield {
                **base,
                "type": "usage",
                "message": {
                    "id": base["uuid"],
                    "model": model,
                    "usage": {
                        "input_tokens": delta["input_tokens"] - cr - cw,
                        "cache_read_input_tokens": cr,
                        "cache_creation_input_tokens": cw,
                        "output_tokens": delta["output_tokens"],
                    },
                },
            }


class CodexSource(SessionSource):
    id = "codex"
    label = "Codex / ChatGPT"

    def discover_projects(self):
        return tuple((e.slug, Path(e.cwd)) for e in sessions())

    def list_sessions(self, slug, project_dir, *, fresh=False):
        return tuple(
            SessionFile(e.sid, e.path, e.mtime, e.size, self.id, e.cwd)
            for e in sessions(slug, fresh=fresh)
        )

    def owns_session(self, session_id):
        return session_id.startswith(PREFIX)

    def matches_record(self, first):
        return first.get("type") == "session_meta"

    def normalize(self, records):
        yield from normalize(records)

    def estimate_cost(self, model, cw, cr, r, w):
        from .openai_pricing import cost_usd

        return cost_usd(model, cw, cr, r, w)
