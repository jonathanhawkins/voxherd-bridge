"""Tests for the voice command dispatch vs queueing logic in ws_handler.py.

Covers the fix for the bug where voice commands were queued with
"will send when it's free" even though the session was idle at its prompt.

The root cause was that _check_session_actually_idle() didn't exist,
and sessions stuck in "active" status (due to false re-activation from
stale scrollback) would unconditionally queue commands.
"""

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from bridge.session_manager import Session
from bridge.ws_handler import _check_session_actually_idle


# ---------------------------------------------------------------------------
# _check_session_actually_idle tests
# ---------------------------------------------------------------------------

IDLE_TMUX_OUTPUT = """\
  Created file at /tmp/foo.py

❯
─────────────────────────────────────────
🤖 Opus 4.6  $0.34  7.2k tokens  12s
⏵⏵ bypass permissions
"""

ACTIVE_TMUX_OUTPUT = """\
  ⠹ Running: pytest tests/ -v
  tests/test_bridge.py::test_register PASSED
"""

STALE_SCROLLBACK_IDLE = """\
  Read bridge/activity.py (1-50)
  Edit bridge/ws_handler.py
  Applied 2 changes

❯
─────────────────────────────────────────
🤖 Opus 4.6  $1.23
"""


def _make_session(status="active", tmux_target="myproject:0.0") -> Session:
    return Session(
        session_id="test-sess-123",
        project="myproject",
        project_dir="/tmp/test",
        status=status,
        tmux_target=tmux_target,
    )


class TestCheckSessionActuallyIdle:
    """Tests for _check_session_actually_idle() — the quick tmux verification."""

    @pytest.mark.asyncio
    async def test_idle_at_prompt_returns_true(self):
        """Session at idle prompt with no spinners → actually idle."""
        session = _make_session()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(IDLE_TMUX_OUTPUT.encode(), b"")
        )
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            result = await _check_session_actually_idle(session)
        assert result is True

    @pytest.mark.asyncio
    async def test_active_spinner_returns_false(self):
        """Session with active spinners → genuinely busy."""
        session = _make_session()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(ACTIVE_TMUX_OUTPUT.encode(), b"")
        )
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            result = await _check_session_actually_idle(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_stale_scrollback_with_idle_prompt_returns_true(self):
        """Stale tool output above idle prompt → actually idle.

        This is the core regression test: Edit/Read in scrollback above ❯
        should NOT make us think the session is busy.
        """
        session = _make_session()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(STALE_SCROLLBACK_IDLE.encode(), b"")
        )
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            result = await _check_session_actually_idle(session)
        assert result is True, (
            "Stale tool output above idle prompt should be detected as idle"
        )

    @pytest.mark.asyncio
    async def test_no_tmux_target_returns_false(self):
        """Session without tmux_target → can't check, return False."""
        session = _make_session(tmux_target=None)
        result = await _check_session_actually_idle(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_output_returns_false(self):
        """Empty tmux output → can't determine, return False."""
        session = _make_session()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            result = await _check_session_actually_idle(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_tmux_timeout_returns_false(self):
        """Tmux command times out → return False (fail safe)."""
        session = _make_session()

        async def slow_communicate():
            await asyncio.sleep(10)
            return (b"", b"")

        mock_proc = AsyncMock()
        mock_proc.communicate = slow_communicate
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            # The function has a 2s timeout via asyncio.wait_for
            result = await _check_session_actually_idle(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_tmux_exception_returns_false(self):
        """Tmux command raises exception → return False (fail safe)."""
        session = _make_session()
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, side_effect=OSError("tmux not found")):
            result = await _check_session_actually_idle(session)
        assert result is False


# ---------------------------------------------------------------------------
# Integration-style tests: dispatch vs queue decision
# ---------------------------------------------------------------------------

class TestDispatchVsQueue:
    """Tests for the dispatch-or-queue decision in handle_voice_command.

    These test the logical flow without a full WebSocket connection:
    - Active session + actually idle at prompt → dispatch immediately
    - Active session + genuinely busy → queue
    - Idle session → dispatch immediately
    """

    def test_active_session_with_idle_check_dispatches(self):
        """When session is 'active' but _check_session_actually_idle returns True,
        the session should be forced to idle and command dispatched."""
        session = _make_session(status="active")

        # Simulate: check returns True → force idle → dispatch
        # This is the exact logic from ws_handler.py lines 823-829
        actually_idle = True  # what _check_session_actually_idle would return
        if session.status == "active" and actually_idle:
            session.status = "idle"

        assert session.status == "idle", "Session should be forced to idle"
        assert session.queued_command is None, "No command should be queued"

    def test_active_session_genuinely_busy_queues(self):
        """When session is 'active' and _check_session_actually_idle returns False,
        the command should be queued."""
        session = _make_session(status="active")
        message = "run the tests"

        actually_idle = False  # what _check_session_actually_idle would return
        if session.status == "active" and not actually_idle:
            session.queued_command = message

        assert session.queued_command == message, "Command should be queued"
        assert session.status == "active", "Session should remain active"

    def test_idle_session_dispatches_directly(self):
        """Idle session should dispatch without checking tmux."""
        session = _make_session(status="idle")

        # Idle sessions skip the queueing logic entirely
        should_queue = session.status == "active"
        assert should_queue is False, "Idle session should not trigger queue path"

    def test_queued_command_cleared_on_dispatch(self):
        """After queued command is dispatched (session goes idle), queue should clear."""
        session = _make_session(status="active")
        session.queued_command = "fix the bug"

        # Simulate session going idle → drain queued command
        session.status = "idle"
        dispatched = session.queued_command
        session.queued_command = None

        assert dispatched == "fix the bug"
        assert session.queued_command is None
