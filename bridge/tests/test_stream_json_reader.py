"""Tests for ``_consume_stream_json`` in bridge/ws_handler.py.

The reader pumps NDJSON events from a dispatched ``claude --resume -p``
subprocess into the session's ring buffer. The risky paths are:

  - Malformed JSON in the stream (silent skip, not abort).
  - ValueError raised by ``readline`` on lines exceeding the buffer
    limit (a 200KB Read result was crashing the reader in production;
    the fix raises the limit AND catches the exception so the reader
    self-recovers rather than dying silently).
  - Cancellation during a long readline (clean exit, no exception).
  - EOF: ``proc.wait()`` called in the finally clause; task slot
    cleared on the session so the next dispatch starts fresh.
  - Session pruned mid-stream: the reader keeps running but no longer
    appends (would otherwise raise AttributeError on the missing buffer).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import ws_handler
from bridge.session_manager import Session, SessionManager


def _make_session(session_id: str = "sid") -> Session:
    """Construct a Session in roughly the state ``_dispatch_agent`` leaves
    it: registered as active, with a fresh empty stream buffer."""
    s = Session(
        session_id=session_id,
        project="proj",
        project_dir="/tmp",
        status="active",
        tmux_target=None,
    )
    s._stream_json_buffer = deque(maxlen=50)
    return s


def _make_proc(lines: list[bytes], wait_returns: int = 0) -> MagicMock:
    """Build a mock subprocess whose ``stdout.readline`` yields each
    bytes line in order, then empty bytes (EOF). ``proc.wait()`` is
    awaitable and returns ``wait_returns``.

    Lines passed to this helper can either be a plain ``bytes`` payload
    (yielded normally) or an Exception instance — the latter causes
    ``readline`` to raise on that iteration.
    """
    proc = MagicMock()
    proc.stdout = MagicMock()
    queue = list(lines) + [b""]  # b"" signals EOF

    async def readline():
        if not queue:
            return b""
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    proc.stdout.readline = readline
    proc.wait = AsyncMock(return_value=wait_returns)
    return proc


@pytest.fixture
def fresh_session_mgr(tmp_path, monkeypatch):
    """Replace the module-level ``sessions`` SessionManager with a fresh
    one writing to a tmpdir, so each test starts from a clean slate
    without touching the real ~/.voxherd/sessions.json."""
    monkeypatch.setattr(
        "bridge.session_manager._PERSIST_PATH",
        str(tmp_path / "sessions.json"),
    )
    mgr = SessionManager()
    monkeypatch.setattr(ws_handler, "sessions", mgr)
    return mgr


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:

    async def test_events_land_in_buffer(self, fresh_session_mgr):
        s = _make_session()
        fresh_session_mgr._sessions[s.session_id] = s
        events = [
            {"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "user", "message": {"content": []}},
            {"type": "result", "subtype": "success"},
        ]
        proc = _make_proc([json.dumps(e).encode() + b"\n" for e in events])

        await ws_handler._consume_stream_json(proc, s.session_id)

        assert len(s._stream_json_buffer) == 3
        assert list(s._stream_json_buffer)[0]["type"] == "assistant"
        assert list(s._stream_json_buffer)[-1]["type"] == "result"
        proc.wait.assert_awaited()

    async def test_buffer_capped_at_maxlen(self, fresh_session_mgr):
        s = _make_session()
        fresh_session_mgr._sessions[s.session_id] = s
        # 75 events, buffer cap is 50 → newest 50 survive
        events = [{"type": "tick", "n": i} for i in range(75)]
        proc = _make_proc([json.dumps(e).encode() + b"\n" for e in events])

        await ws_handler._consume_stream_json(proc, s.session_id)

        assert len(s._stream_json_buffer) == 50
        assert list(s._stream_json_buffer)[0]["n"] == 25
        assert list(s._stream_json_buffer)[-1]["n"] == 74


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:

    async def test_malformed_json_skipped(self, fresh_session_mgr):
        s = _make_session()
        fresh_session_mgr._sessions[s.session_id] = s
        proc = _make_proc([
            json.dumps({"type": "assistant"}).encode() + b"\n",
            b"not json at all\n",
            b"{partial\n",
            json.dumps({"type": "result"}).encode() + b"\n",
        ])

        await ws_handler._consume_stream_json(proc, s.session_id)

        # Only the two valid lines made it into the buffer.
        assert len(s._stream_json_buffer) == 2
        types = [e["type"] for e in s._stream_json_buffer]
        assert types == ["assistant", "result"]

    async def test_value_error_continues_not_aborts(self, fresh_session_mgr):
        """readline raises ValueError on lines exceeding the
        StreamReader limit. The reader must catch and continue,
        otherwise an oversized Read result would silently terminate
        the whole stream — losing ALL subsequent events."""
        s = _make_session()
        fresh_session_mgr._sessions[s.session_id] = s
        proc = _make_proc([
            json.dumps({"type": "before"}).encode() + b"\n",
            ValueError("Separator is not found, and chunk exceed the limit"),
            json.dumps({"type": "after"}).encode() + b"\n",
        ])

        await ws_handler._consume_stream_json(proc, s.session_id)

        types = [e["type"] for e in s._stream_json_buffer]
        assert types == ["before", "after"], (
            f"ValueError mid-stream killed the reader; got {types}"
        )

    async def test_incomplete_read_exits_cleanly(self, fresh_session_mgr):
        s = _make_session()
        fresh_session_mgr._sessions[s.session_id] = s
        proc = _make_proc([
            json.dumps({"type": "x"}).encode() + b"\n",
            asyncio.IncompleteReadError(partial=b"", expected=None),
        ])

        # Must not raise.
        await ws_handler._consume_stream_json(proc, s.session_id)
        assert [e["type"] for e in s._stream_json_buffer] == ["x"]

    async def test_session_pruned_midstream_does_not_crash(self, fresh_session_mgr):
        """If the session disappears between two readlines (e.g. user
        killed it from another path), the reader must not raise — it
        just stops appending."""
        s = _make_session()
        fresh_session_mgr._sessions[s.session_id] = s

        # Tear the session down after the first event lands in the buffer.
        events_q = [
            json.dumps({"type": "first"}).encode() + b"\n",
            None,  # placeholder; we'll trigger removal here
            json.dumps({"type": "third"}).encode() + b"\n",
        ]

        async def readline():
            while events_q:
                item = events_q.pop(0)
                if item is None:
                    # Simulate pruning. Don't return — recurse to next item.
                    fresh_session_mgr.remove_session(s.session_id)
                    continue
                return item
            return b""

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = readline
        proc.wait = AsyncMock(return_value=0)

        # The session is gone after the prune, so its buffer ref also
        # goes — we can't assert on it. Just verify the reader returns
        # without raising.
        await ws_handler._consume_stream_json(proc, s.session_id)
        proc.wait.assert_awaited()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:

    async def test_stream_json_task_slot_cleared_on_eof(self, fresh_session_mgr):
        """After natural EOF the session's ``_stream_json_task`` slot
        is reset to None so a subsequent dispatch can reuse it."""
        s = _make_session()
        # Pretend a task is already attached (the real _dispatch_agent
        # would have stored one). The reader doesn't care WHAT object
        # is there, only that it exists and gets cleared at the end.
        s._stream_json_task = object()
        fresh_session_mgr._sessions[s.session_id] = s

        proc = _make_proc([json.dumps({"type": "result"}).encode() + b"\n"])
        await ws_handler._consume_stream_json(proc, s.session_id)

        assert s._stream_json_task is None

    async def test_proc_wait_called_in_finally(self, fresh_session_mgr):
        s = _make_session()
        fresh_session_mgr._sessions[s.session_id] = s
        proc = _make_proc([RuntimeError("simulated crash")])

        # Even on an arbitrary mid-stream exception the reader must
        # still call proc.wait() so we don't leave zombie subprocesses.
        await ws_handler._consume_stream_json(proc, s.session_id)
        proc.wait.assert_awaited()
