"""AppleScript string escaping for the folder-picker prompt."""

from __future__ import annotations

import subprocess
import sys

import pytest

from unwind import dialog
from unwind.dialog import _escape_applescript_string, _pick_with_tk, _TK_SCRIPT


def test_plain_path_passthrough():
    assert _escape_applescript_string("/Users/me/work") == "/Users/me/work"


def test_double_quote_escaped():
    out = _escape_applescript_string('/tmp/a"b')
    assert out == '/tmp/a\\"b'


def test_backslash_escaped():
    out = _escape_applescript_string(r"/tmp/a\b")
    assert out == r"/tmp/a\\b"


def test_backslash_must_be_doubled_first():
    """If we escaped quotes before backslashes we'd produce \\" → \\\\\\"
    which closes the string. Verify the actual escape order."""
    out = _escape_applescript_string(r"/tmp/a\"b")
    # Backslash first becomes \\, then quote becomes \" → \\\\"
    assert out == r"/tmp/a\\\"b"


def test_newline_stripped():
    # Newline would terminate the AppleScript line. Strip it.
    out = _escape_applescript_string('/tmp/a\n" & do shell script "rm -rf /')
    assert "\n" not in out
    # The injected payload no longer contains a syntactic newline.
    assert "shell script" in out  # remaining text is benign string content


def test_control_chars_stripped():
    assert _escape_applescript_string("/tmp/\x00\x01a") == "/tmp/a"


# --- _pick_with_tk: initial is passed via argv, not embedded in script body ---


def test_tk_script_reads_initial_from_argv_not_substitution():
    """User-controlled initial must arrive via sys.argv, not via string interpolation."""
    assert "sys.argv[1]" in _TK_SCRIPT
    # No %-formatting or .format() of user input
    assert ".format(" not in _TK_SCRIPT
    assert "initialdir=" not in _TK_SCRIPT or 'kwargs["initialdir"]' in _TK_SCRIPT


def test_tk_passes_initial_as_argv(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    _pick_with_tk('/tmp/weird "name"\nwith\\backslash')
    cmd = captured["cmd"]
    assert cmd[0] == sys.executable
    assert cmd[1] == "-c"
    assert cmd[2] == _TK_SCRIPT  # unchanged script body
    assert cmd[3] == '/tmp/weird "name"\nwith\\backslash'  # raw, no escaping needed
    assert captured["timeout"] == 120


def test_tk_passes_empty_string_when_initial_none(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    _pick_with_tk(None)
    assert captured["cmd"][3] == ""


def test_tk_script_is_syntactically_valid_python():
    """Compile the fixed script body to confirm no syntax errors."""
    compile(_TK_SCRIPT, "<dialog _TK_SCRIPT>", "exec")


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(dialog.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dialog.shutil, "which", lambda _: "/usr/bin/osascript")


def test_macos_cancel_does_not_open_tk(macos, monkeypatch):
    calls = []

    def cancel(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 1, "", "execution error: User canceled. (-128)\n"
        )

    monkeypatch.setattr(subprocess, "run", cancel)
    assert dialog.pick_folder() is None
    assert len(calls) == 1
    assert calls[0][0] == "/usr/bin/osascript"


@pytest.mark.parametrize("native_failure", ["missing", "error", "launch"])
def test_unavailable_macos_picker_falls_back_to_tk(
    macos, monkeypatch, tmp_path, native_failure
):
    calls = []
    if native_failure == "missing":
        monkeypatch.setattr(dialog.shutil, "which", lambda _: None)

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "/usr/bin/osascript":
            if native_failure == "launch":
                raise OSError("could not launch")
            return subprocess.CompletedProcess(
                cmd, 1, "", "execution error: Not authorized. (-1743)"
            )
        return subprocess.CompletedProcess(cmd, 0, str(tmp_path), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert dialog.pick_folder() == tmp_path.resolve()
    assert calls[-1][0] == sys.executable


@pytest.mark.parametrize("backend", ["osascript", "tk"])
def test_timeout_is_typed_and_never_opens_another_picker(macos, monkeypatch, backend):
    if backend == "tk":
        monkeypatch.setattr(dialog.platform, "system", lambda: "Linux")
    calls = []

    def timeout(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(dialog.FolderPickerTimeout, match="120 seconds"):
        dialog.pick_folder()
    assert len(calls) == 1


@pytest.mark.parametrize("result", ["cancel", "failure", "launch"])
def test_tk_cancellation_is_distinct_from_unavailability(monkeypatch, result):
    monkeypatch.setattr(dialog.platform, "system", lambda: "Linux")

    def run(cmd, **kwargs):
        if result == "launch":
            raise OSError("could not launch")
        return subprocess.CompletedProcess(cmd, 0 if result == "cancel" else 2, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    if result == "cancel":
        assert dialog.pick_folder() is None
    else:
        with pytest.raises(dialog.FolderPickerUnavailable):
            dialog.pick_folder()
