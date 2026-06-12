"""Regression tests for dispatching to a tmux pane that no longer exists.

Bug ("stuck saying Sending to WeaveHacks4"): when a voice command resolved to
a session whose tmux pane was gone (common for a swarm whose worker/conductor
tmux sessions come and go), `_dispatch_agent` fired `tmux send-keys`, which
exits NON-ZERO ("can't find pane: …") WITHOUT raising. The old code DEVNULL'd
stderr and never checked the return code, so it logged success and the command
silently vanished. iOS had already been told `command_accepted` and announced
"Sending to <project>", and no Stop hook ever fires (nothing ran), so the
session stayed wedged "active" — every retry just re-announced the send.

Fix: check the send-keys return code; on a dead pane, drop the stale session,
tell iOS the command wasn't delivered, and push a fresh state_sync.
"""

from unittest.mock import patch, AsyncMock

import pytest

import bridge.ws_handler as ws_handler
from bridge.ws_handler import _dispatch_agent, sessions
from bridge.session_manager import Session


def _register(session_id="dead-sess-1", project="WeaveHacks4",
              tmux_target="vh-WeaveHacks4-worker:0.0") -> Session:
    s = Session(
        session_id=session_id,
        project=project,
        project_dir="/tmp/weavehacks4",
        status="active",
        tmux_target=tmux_target,
        assistant="claude",
    )
    sessions._sessions[session_id] = s
    return s


def _proc(returncode: int, stderr: bytes = b""):
    p = AsyncMock()
    p.returncode = returncode
    p.communicate = AsyncMock(return_value=(b"", stderr))
    return p


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Keep the global session registry clean and avoid disk writes."""
    saved = sessions._sessions.copy()
    sessions._sessions.clear()
    with patch.object(sessions, "_save", return_value=None):
        yield
    sessions._sessions.clear()
    sessions._sessions.update(saved)


class TestDeadPaneDispatch:
    @pytest.mark.asyncio
    async def test_dead_pane_drops_session_and_notifies(self):
        """send-keys returns non-zero → stale session removed + iOS told."""
        _register()
        broadcasts: list[dict] = []

        async def _capture(msg):
            broadcasts.append(msg)

        dead = _proc(returncode=1, stderr=b"can't find pane: vh-WeaveHacks4-worker:0.0")
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock, return_value=dead), \
             patch.object(ws_handler, "broadcast_to_ios", new=_capture):
            await _dispatch_agent("dead-sess-1", "/tmp/weavehacks4", "run the tests")

        # The stale session is gone so routing stops resolving to a dead pane.
        assert sessions.get_session("dead-sess-1") is None
        # iOS gets a clear error (not silence) + a fresh state_sync to prune.
        kinds = [m.get("type") for m in broadcasts]
        assert "error" in kinds, kinds
        assert "state_sync" in kinds, kinds
        err = next(m for m in broadcasts if m.get("type") == "error")
        assert "WeaveHacks4" in err["message"]
        assert err["session_id"] == "dead-sess-1"

    @pytest.mark.asyncio
    async def test_enter_failure_also_handled(self):
        """First send-keys OK, the Enter send-keys fails → still handled."""
        _register(session_id="dead-sess-2")
        broadcasts: list[dict] = []

        async def _capture(msg):
            broadcasts.append(msg)

        ok = _proc(returncode=0)
        dead = _proc(returncode=1, stderr=b"can't find pane")
        # First call (literal text) succeeds, second call (Enter) fails.
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock, side_effect=[ok, dead]), \
             patch.object(ws_handler, "broadcast_to_ios", new=_capture):
            await _dispatch_agent("dead-sess-2", "/tmp/weavehacks4", "go")

        assert sessions.get_session("dead-sess-2") is None
        assert any(m.get("type") == "error" for m in broadcasts)

    @pytest.mark.asyncio
    async def test_live_pane_dispatch_keeps_session(self):
        """Both send-keys succeed → session stays, no error broadcast."""
        _register(session_id="live-sess")
        broadcasts: list[dict] = []

        async def _capture(msg):
            broadcasts.append(msg)

        ok = _proc(returncode=0)
        with patch("bridge.ws_handler.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock, return_value=ok), \
             patch.object(ws_handler, "broadcast_to_ios", new=_capture):
            await _dispatch_agent("live-sess", "/tmp/weavehacks4", "run the tests")

        # Session survives; no dead-pane error fired.
        assert sessions.get_session("live-sess") is not None
        assert not any(m.get("type") == "error" for m in broadcasts)
