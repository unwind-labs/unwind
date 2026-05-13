"""AppleScript string escaping for the folder-picker prompt."""
from __future__ import annotations

from unwind.dialog import _escape_applescript_string


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
