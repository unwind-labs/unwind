"""Tests for slug derivation against real Claude Code project layouts."""
from __future__ import annotations

import pytest

from unwind.projects import slug_for


@pytest.mark.parametrize(
    "path, expected",
    [
        # Plain ASCII path — every "/" becomes "-".
        ("/Users/amolk/work/agent-callstack/unwind",
         "-Users-amolk-work-agent-callstack-unwind"),
        # Leading-dot directory: "/." collapses to "--".
        ("/Users/amolk/.claude", "-Users-amolk--claude"),
        # Underscores become hyphens.
        ("/Users/me/foo_bar", "-Users-me-foo-bar"),
        # Hyphens already in the path are preserved as-is.
        ("/Users/me/foo-bar", "-Users-me-foo-bar"),
        # Spaces and dots together — the bug report case.
        ("/Users/akelkar/work/harness-engineering/04. mcp",
         "-Users-akelkar-work-harness-engineering-04--mcp"),
        # Multiple consecutive special chars each map to their own "-".
        ("/Users/me/a.b_c d", "-Users-me-a-b-c-d"),
        # Parentheses (a real-world case in some app paths).
        ("/Users/me/Project (final)", "-Users-me-Project--final-"),
    ],
)
def test_slug_for_matches_claude_code(path: str, expected: str) -> None:
    assert slug_for(path) == expected
