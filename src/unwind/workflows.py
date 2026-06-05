"""Per-session workflow runs.

When the assistant uses the ``Workflow`` tool, Claude Code runs a JS
orchestration script in the background that spawns many subagents across
phases. Each run is persisted alongside the launching session:

    ~/.claude/projects/<slug>/<session>/workflows/wf_<runId>.json          (rollup)
    ~/.claude/projects/<slug>/<session>/workflows/scripts/<name>-wf_<runId>.js
    ~/.claude/projects/<slug>/<session>/subagents/workflows/wf_<runId>/agent-<agentId>.jsonl
    ~/.claude/projects/<slug>/<session>/subagents/workflows/wf_<runId>/journal.jsonl

The ``wf_<runId>.json`` rollup is the rich source — phases plus one
``workflow_agent`` entry per agent (label, phase, model, state, tokens,
duration). It is written only when the run COMPLETES; a still-running run
has the transcript dir + journal but no rollup, so we synthesise a degraded
view from the transcript filenames and the journal's ``started``/``result``
events.

We surface each run as a synthetic grouping subtree in the session's call
tree (assembled in :mod:`unwind.spawns`): a run node → one phase node per
phase → one agent leaf per agent. The agent leaves reuse the ``agent-<id>``
subagent-transcript machinery (their JSONLs are real Claude logs), so they
drill into their conversation and price their tokens for free.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ._cache import PathCache


@dataclass(frozen=True)
class WorkflowAgent:
    agent_id: str
    label: str
    phase_index: int
    phase_title: str
    model: str
    state: str  # raw rollup state: ``done`` | ``running`` | ``queued`` | ``error`` …
    tokens: int
    tool_calls: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]

    @property
    def synthetic_session_id(self) -> str:
        """``agent-<id>`` — the same address the subagent machinery uses, so
        the canvas scanner and messages endpoint resolve this agent's
        transcript without a new code path."""
        return f"agent-{self.agent_id}"


@dataclass(frozen=True)
class WorkflowPhase:
    index: int
    title: str


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str            # ``wf_<hex>`` — used verbatim as the run node's synthetic session id
    name: str
    status: str            # raw rollup status: ``completed`` | ``running`` | ``failed`` …
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    total_tokens: int
    phases: tuple[WorkflowPhase, ...]
    agents: tuple[WorkflowAgent, ...]
    # True when synthesised from a still-running run (no rollup yet): phases
    # and per-agent metadata are unknown, so labels/tokens are approximate.
    partial: bool = False
    result_preview: str = ""
    log_lines: tuple[str, ...] = field(default_factory=tuple)

    def phase_session_id(self, phase_index: int) -> str:
        return f"{self.run_id}::p{phase_index}"


class WorkflowIndex:
    """Caches per-session workflow runs keyed by the launching session_id.

    Mirrors :class:`unwind.subagents.SubagentIndex`: a per-file
    ``(mtime, size)`` cache for rollup parsing plus a per-session listing
    cache keyed by the relevant directories' mtimes.
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self._lock = threading.Lock()
        self._file_cache: PathCache = PathCache(self._build_run_from_rollup)
        self._cache: dict[str, tuple[tuple, list[WorkflowRun]]] = {}
        self._parent_sids_cache: Optional[tuple[float, frozenset[str]]] = None

    # --- discovery --------------------------------------------------------

    def parent_sids(self) -> set[str]:
        """Every session_id that launched at least one workflow.

        A launching session has a ``<sid>/workflows/`` dir (the script is
        written there at launch, before any rollup lands). One mtime-cached
        ``os.scandir`` of the project dir.
        """
        if not self._project_dir.is_dir():
            return set()
        try:
            dir_mtime = self._project_dir.stat().st_mtime
        except OSError:
            return set()

        with self._lock:
            cached = self._parent_sids_cache
            if cached is not None and cached[0] == dir_mtime:
                return set(cached[1])

        out: set[str] = set()
        try:
            with os.scandir(self._project_dir) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if os.path.isdir(os.path.join(entry.path, "workflows")):
                        out.add(entry.name)
        except OSError:
            return set()

        with self._lock:
            self._parent_sids_cache = (dir_mtime, frozenset(out))
        return out

    def list_for_session(self, session_id: str) -> list[WorkflowRun]:
        """Workflow runs launched by ``session_id``.

        Completed runs come from ``workflows/wf_*.json`` rollups; a run with
        a transcript dir but no rollup yet is synthesised as a degraded
        (``partial``) run.
        """
        rollup_dir = self._project_dir / session_id / "workflows"
        transcript_dir = self._project_dir / session_id / "subagents" / "workflows"
        sig = (_dir_mtime(rollup_dir), _dir_mtime(transcript_dir))
        if sig == (None, None):
            return []

        with self._lock:
            cached = self._cache.get(session_id)
            if cached is not None and cached[0] == sig:
                return list(cached[1])

        runs: list[WorkflowRun] = []
        seen: set[str] = set()
        if rollup_dir.is_dir():
            for rollup in sorted(rollup_dir.glob("wf_*.json")):
                run = self._file_cache.get(rollup)
                if run is not None:
                    runs.append(run)
                    seen.add(run.run_id)
        if transcript_dir.is_dir():
            for run_dir in sorted(transcript_dir.glob("wf_*")):
                if not run_dir.is_dir():
                    continue
                run_id = run_dir.name
                if run_id in seen:
                    continue
                run = self._build_running_run(session_id, run_id, run_dir)
                if run is not None:
                    runs.append(run)
                    seen.add(run_id)

        runs.sort(key=lambda r: r.started_at or _EPOCH)
        with self._lock:
            self._cache[session_id] = (sig, runs)
        return list(runs)

    def resolve_run(self, run_id: str) -> Optional[WorkflowRun]:
        """Find a run by ``wf_<id>`` across every launching session.

        ``run_id`` may carry a ``::p<n>`` phase suffix (the phase node's
        synthetic id) — it's stripped so phase nodes resolve to their run.
        """
        base = run_id.split("::", 1)[0]
        if not self._project_dir.is_dir():
            return None
        for sid in self.parent_sids():
            for run in self.list_for_session(sid):
                if run.run_id == base:
                    return run
        return None

    # --- internals --------------------------------------------------------

    def _build_run_from_rollup(self, path: Path) -> Optional[WorkflowRun]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        run_id = str(data.get("runId") or path.stem)
        name = str(data.get("workflowName") or "workflow")
        status = str(data.get("status") or "")
        started_at = _epoch_ms(data.get("startTime"))
        ended_at = _add_ms(started_at, data.get("durationMs"))
        total_tokens = _as_int(data.get("totalTokens"))

        phases: list[WorkflowPhase] = []
        agents: list[WorkflowAgent] = []
        for entry in data.get("workflowProgress") or []:
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")
            if etype == "workflow_phase":
                phases.append(
                    WorkflowPhase(
                        index=_as_int(entry.get("index")),
                        title=str(entry.get("title") or ""),
                    )
                )
            elif etype == "workflow_agent":
                a_start = _epoch_ms(entry.get("startedAt"))
                agents.append(
                    WorkflowAgent(
                        agent_id=str(entry.get("agentId") or ""),
                        label=str(entry.get("label") or ""),
                        phase_index=_as_int(entry.get("phaseIndex")),
                        phase_title=str(entry.get("phaseTitle") or ""),
                        model=str(entry.get("model") or ""),
                        state=str(entry.get("state") or ""),
                        tokens=_as_int(entry.get("tokens")),
                        tool_calls=_as_int(entry.get("toolCalls")),
                        started_at=a_start,
                        ended_at=_add_ms(a_start, entry.get("durationMs")),
                    )
                )

        agents = [a for a in agents if a.agent_id]
        if not phases:
            phases = _phases_from_agents(agents)

        return WorkflowRun(
            run_id=run_id,
            name=name,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            total_tokens=total_tokens,
            phases=tuple(sorted(phases, key=lambda p: p.index)),
            agents=tuple(agents),
            partial=False,
            result_preview=_result_preview(data.get("result")),
            log_lines=tuple(str(x) for x in (data.get("logs") or []) if isinstance(x, str)),
        )

    def _build_running_run(
        self, session_id: str, run_id: str, run_dir: Path
    ) -> Optional[WorkflowRun]:
        """Degraded view of a run with a transcript dir but no rollup yet.

        Agents come from the ``agent-*.jsonl`` filenames; their done/running
        split comes from the journal (``result`` = done, ``started`` only =
        running). Phases and per-agent labels are unknown, so we collapse to
        a single unnamed phase and label agents by id prefix.
        """
        done_ids: set[str] = set()
        journal = run_dir / "journal.jsonl"
        if journal.is_file():
            try:
                with journal.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") == "result":
                            aid = rec.get("agentId")
                            if isinstance(aid, str):
                                done_ids.add(aid)
            except OSError:
                pass

        agents: list[WorkflowAgent] = []
        for jsonl in sorted(run_dir.glob("agent-*.jsonl")):
            agent_id = jsonl.stem.removeprefix("agent-")
            if not agent_id:
                continue
            agents.append(
                WorkflowAgent(
                    agent_id=agent_id,
                    label=agent_id[:8],
                    phase_index=1,
                    phase_title="",
                    model="",
                    state="done" if agent_id in done_ids else "running",
                    tokens=0,
                    tool_calls=0,
                    started_at=_file_birth(jsonl),
                    ended_at=None,
                )
            )
        if not agents:
            return None

        starts = [a.started_at for a in agents if a.started_at]
        return WorkflowRun(
            run_id=run_id,
            name=_infer_name(self._project_dir / session_id / "workflows" / "scripts", run_id),
            status="running",
            started_at=min(starts) if starts else None,
            ended_at=None,
            total_tokens=0,
            phases=(WorkflowPhase(index=1, title=""),),
            agents=tuple(agents),
            partial=True,
        )


# --- helpers --------------------------------------------------------------


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_int(v: object) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def _epoch_ms(v: object) -> Optional[datetime]:
    if isinstance(v, (int, float)) and v > 0:
        try:
            return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    return None


def _add_ms(start: Optional[datetime], dur: object) -> Optional[datetime]:
    if start is None or not isinstance(dur, (int, float)) or dur < 0:
        return None
    return start + timedelta(milliseconds=dur)


def _dir_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _file_birth(path: Path) -> Optional[datetime]:
    try:
        st = path.stat()
    except OSError:
        return None
    bt = getattr(st, "st_birthtime", None)
    ts = bt if isinstance(bt, (int, float)) and bt > 0 else st.st_mtime
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, ValueError):
        return None


def _phases_from_agents(agents: list[WorkflowAgent]) -> list[WorkflowPhase]:
    """Reconstruct the phase list from agents when the rollup didn't record
    explicit ``workflow_phase`` entries (older / partial rollups)."""
    seen: dict[int, str] = {}
    for a in agents:
        seen.setdefault(a.phase_index, a.phase_title)
    return [WorkflowPhase(index=i, title=seen[i]) for i in sorted(seen)]


def _infer_name(scripts_dir: Path, run_id: str) -> str:
    """Best-effort workflow name from a ``<name>-wf_<runId>.js`` script
    filename in the session's ``workflows/scripts/`` dir."""
    if scripts_dir.is_dir():
        suffix = f"-{run_id}.js"
        try:
            for script in scripts_dir.glob(f"*-{run_id}.js"):
                if script.name.endswith(suffix):
                    return script.name[: -len(suffix)]
        except OSError:
            pass
    return "workflow"


def _result_preview(result: object, limit: int = 600) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(result)
    return text[:limit]
