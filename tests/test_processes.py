"""Detector-level tests for processes._is_claude_process.

The macOS-specific quirk: psutil reports the bare ``claude`` CLI's name as
its version string (e.g. ``2.1.126``), not ``claude``. The detector must
match via cmdline[0] basename as well.
"""
from __future__ import annotations

from unwind.processes import _is_claude_process


def test_matches_lowercased_name():
    assert _is_claude_process("claude", ["whatever"]) is True
    assert _is_claude_process("Claude", ["whatever"]) is True


def test_matches_bare_claude_command_with_version_name():
    """The exact failure mode that hid an active session: psutil.name() returns
    a version string, cmdline is just ``['claude']``."""
    assert _is_claude_process("2.1.126", ["claude"]) is True


def test_matches_full_path_in_cmdline():
    cmd = ["/Users/x/Library/Application Support/Claude/.../claude", "--flag"]
    assert _is_claude_process("foo", cmd) is True


def test_rejects_unrelated_processes():
    assert _is_claude_process("python", ["python", "script.py"]) is False
    assert _is_claude_process("node", ["node", "server.js"]) is False
    assert _is_claude_process("", []) is False
    assert _is_claude_process(None, None) is False


def test_does_not_match_substring_only():
    """``claude-helper`` shouldn't match (it's not ``claude``)."""
    assert _is_claude_process("claude-helper", ["claude-helper"]) is False
    assert _is_claude_process("foo", ["/path/to/claude-helper"]) is False


def test_handles_non_string_cmdline_entries():
    assert _is_claude_process("foo", [None, "x"]) is False  # type: ignore[list-item]


# --- session_status mtime fallback ----------------------------------------

import time
from unittest.mock import patch

from unwind.processes import session_status, LIVE_MTIME_WINDOW_SEC


def test_session_status_done_when_no_process_and_mtime_old():
    """The exact regression we just fixed: process is gone (user exited),
    JSONL mtime is older than the live window → status must be ``done``.
    Without the tightened window, this would have shown ``live`` for up
    to 5 minutes after exit."""
    project_path = "/tmp/some-project"
    last_epoch = time.time() - (LIVE_MTIME_WINDOW_SEC + 5)
    with patch("unwind.processes.project_activity") as pa:
        pa.return_value.claude_running = False
        assert session_status(project_path, last_epoch) == "done"


def test_session_status_live_when_process_running():
    project_path = "/tmp/some-project"
    last_epoch = time.time() - 3600  # mtime irrelevant when process is up
    with patch("unwind.processes.project_activity") as pa:
        pa.return_value.claude_running = True
        assert session_status(project_path, last_epoch) == "live"


def test_session_status_live_during_cwd_registration_race():
    """Process detection lags slightly behind a fresh session — the mtime
    fallback should cover that brief gap."""
    project_path = "/tmp/some-project"
    last_epoch = time.time() - 5  # 5s old, well within window
    with patch("unwind.processes.project_activity") as pa:
        pa.return_value.claude_running = False
        assert session_status(project_path, last_epoch) == "live"
