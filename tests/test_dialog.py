"""AppleScript string escaping for the folder-picker prompt."""
from __future__ import annotations

import subprocess
import sys

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
    out = _escape_applescript_string(r'/tmp/a\"b')
    # Backslash first becomes \\, then quote becomes \" → \\\\"
    assert out == r'/tmp/a\\\"b'


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
