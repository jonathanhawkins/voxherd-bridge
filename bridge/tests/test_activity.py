"""Tests for bridge/activity.py — activity detection and idle prompt logic.

Covers the fix for the bug where stale tool names in terminal scrollback
falsely re-activated idle sessions, causing voice commands to queue with
"will send when it's free" even though nothing was running.
"""

import pytest

from bridge.activity import _detect_activity_type, _has_idle_prompt


# ---------------------------------------------------------------------------
# Terminal output fixtures
# ---------------------------------------------------------------------------

# Claude Code sitting at its idle prompt — nothing happening
IDLE_TERMINAL = """\
  Created file at /tmp/foo.py

  Wrote 42 lines to foo.py

❯
─────────────────────────────────────────
🤖 Opus 4.6  $0.34  7.2k tokens  12s
⏵⏵ bypass permissions
"""

# Claude Code actively running a tool — spinner visible
ACTIVE_WITH_SPINNER = """\
  ⠹ Running: pytest tests/ -v
"""

# Claude Code actively editing — tool pattern visible, no idle prompt
ACTIVE_EDITING = """\
  Edit bridge/server.py
  Applied 3 changes
  ⠼ Verifying...
"""

# Stale scrollback with tool names ABOVE the idle prompt
# This was the root cause of the false re-activation bug
STALE_SCROLLBACK_WITH_IDLE_PROMPT = """\
  Read bridge/activity.py (1-50)
  Edit bridge/ws_handler.py
  Applied 2 changes
  Bash: pytest tests/ -v
  All 70 tests passed.

❯
─────────────────────────────────────────
🤖 Opus 4.6  $1.23  45k tokens  2m
⏵⏵ bypass permissions
"""

# Active tool output with no idle prompt — genuinely busy
ACTIVE_NO_IDLE_PROMPT = """\
  Read bridge/activity.py (1-50)
  Edit bridge/ws_handler.py
  Applied 2 changes
"""

# Empty terminal
EMPTY_TERMINAL = ""

# Only whitespace
WHITESPACE_TERMINAL = "   \n\n  \n"

# Idle prompt with > instead of ❯
IDLE_PROMPT_ANGLE_BRACKET = """\
  Done.

>
─────────────────────
🤖 Haiku  $0.01
"""

# Spinner but also idle prompt (edge case — spinner wins)
SPINNER_WITH_IDLE_PROMPT = """\
  ⠹ Running something
❯
"""


# ---------------------------------------------------------------------------
# _has_idle_prompt tests
# ---------------------------------------------------------------------------

class TestHasIdlePrompt:
    """Tests for _has_idle_prompt() — idle prompt detection near bottom of terminal."""

    def test_idle_prompt_present(self):
        assert _has_idle_prompt(IDLE_TERMINAL) is True

    def test_idle_prompt_angle_bracket(self):
        assert _has_idle_prompt(IDLE_PROMPT_ANGLE_BRACKET) is True

    def test_idle_prompt_in_stale_scrollback(self):
        """Idle prompt at bottom with stale tool output above — still idle."""
        assert _has_idle_prompt(STALE_SCROLLBACK_WITH_IDLE_PROMPT) is True

    def test_no_idle_prompt_active(self):
        assert _has_idle_prompt(ACTIVE_WITH_SPINNER) is False

    def test_no_idle_prompt_editing(self):
        assert _has_idle_prompt(ACTIVE_EDITING) is False

    def test_no_idle_prompt_no_prompt(self):
        assert _has_idle_prompt(ACTIVE_NO_IDLE_PROMPT) is False

    def test_empty_terminal(self):
        assert _has_idle_prompt(EMPTY_TERMINAL) is False

    def test_whitespace_terminal(self):
        assert _has_idle_prompt(WHITESPACE_TERMINAL) is False

    def test_bare_prompt_line(self):
        assert _has_idle_prompt("❯\n") is True

    def test_prompt_with_trailing_space(self):
        assert _has_idle_prompt("❯ \n") is True


# ---------------------------------------------------------------------------
# _detect_activity_type tests
# ---------------------------------------------------------------------------

class TestDetectActivityType:
    """Tests for _detect_activity_type() — what is Claude Code doing?"""

    def test_idle_prompt_returns_none(self):
        """When idle prompt is visible and no spinners, return None (idle)."""
        result = _detect_activity_type(IDLE_TERMINAL)
        assert result is None

    def test_spinner_detected_as_thinking(self):
        """Bare spinner with no tool pattern → thinking."""
        result = _detect_activity_type("  ⠹ Working on it...\n")
        assert result == "thinking"

    def test_edit_tool_detected(self):
        """Edit tool pattern without idle prompt → writing."""
        result = _detect_activity_type(ACTIVE_EDITING)
        assert result is not None  # should detect some activity

    def test_stale_scrollback_with_idle_prompt_returns_none(self):
        """Tool names in scrollback ABOVE idle prompt must NOT trigger activity.

        This is the core regression test for the false re-activation bug.
        Before the fix, stale Edit/Read/Bash output above the ❯ prompt
        would re-activate an idle session, blocking voice commands.
        """
        result = _detect_activity_type(STALE_SCROLLBACK_WITH_IDLE_PROMPT)
        assert result is None, (
            "Stale tool names above idle prompt should not be detected as activity. "
            "This was the root cause of the 'queued for VoxHerd' bug."
        )

    def test_active_no_idle_prompt_detects_activity(self):
        """Tool output without idle prompt → detected as activity."""
        result = _detect_activity_type(ACTIVE_NO_IDLE_PROMPT)
        # Should detect Read or Edit
        assert result is not None

    def test_empty_returns_none(self):
        result = _detect_activity_type(EMPTY_TERMINAL)
        assert result is None

    def test_spinner_overrides_idle_prompt(self):
        """If both spinner and idle prompt present, spinner means activity."""
        result = _detect_activity_type(SPINNER_WITH_IDLE_PROMPT)
        # Spinner detected → should not return None
        assert result is not None


# ---------------------------------------------------------------------------
# Re-activation guard regression tests
# ---------------------------------------------------------------------------

class TestReactivationGuard:
    """Regression tests for the idle-prompt guard on session re-activation.

    The activity poll loop should NOT re-activate a session when:
    - Tool names appear in terminal scrollback (stale history)
    - BUT the idle prompt (❯) is visible at the bottom

    These tests verify the building blocks that the poll loop relies on.
    """

    def test_stale_edit_above_prompt_not_reactivated(self):
        """Edit in scrollback + idle prompt = should not re-activate."""
        terminal = "  Edit foo.py\n  Applied 1 change\n\n❯\n───\n🤖 Opus\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        # The re-activation condition is: detected is not None AND NOT has_idle_prompt
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is False, "Should not re-activate with idle prompt visible"

    def test_stale_read_above_prompt_not_reactivated(self):
        """Read in scrollback + idle prompt = should not re-activate."""
        terminal = "  Read bridge/server.py (1-100)\n\n❯\n───\n🤖 Opus\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is False

    def test_stale_bash_above_prompt_not_reactivated(self):
        """Bash in scrollback + idle prompt = should not re-activate."""
        terminal = "  Bash: npm test\n  All tests pass\n\n❯\n───\n🤖 Opus\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is False

    def test_active_spinner_no_prompt_should_reactivate(self):
        """Spinner visible + no idle prompt = genuinely busy, should re-activate."""
        terminal = "  ⠹ Running: pytest tests/\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is True, "Active spinner without idle prompt should re-activate"

    def test_no_activity_with_idle_prompt_should_auto_idle(self):
        """No detected activity + idle prompt visible = should auto-idle immediately."""
        detected = _detect_activity_type(IDLE_TERMINAL)
        has_prompt = _has_idle_prompt(IDLE_TERMINAL)
        # The auto-idle condition for active sessions:
        # detected is None AND has_idle_prompt → idle immediately
        should_auto_idle = detected is None and has_prompt
        assert should_auto_idle is True, "Should auto-idle when at prompt with no activity"
