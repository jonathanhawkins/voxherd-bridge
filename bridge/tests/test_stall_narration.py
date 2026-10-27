"""Stall narration: "voxherd appears stalled." (VERBOSE only).

A working session that shows no new activity for a minute is announced once.
Nothing called the check from February to September 2026, so VERBOSE's one
extra announcement was silently missing -- which looks exactly like a quiet
herd.

The ways it goes wrong once it runs are both a voice that cannot be trusted:
one that calls a session stalled the moment it starts a new command (the clock
still counting the idle stretch before it), and one that never speaks again
after its first warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bridge import activity, session_manager
from bridge import server_state as state
from bridge.narration import NarrationEngine, Verbosity
from bridge.session_manager import SessionManager

# A project_dir a real project could have. register_session refuses
# _TEST_PROJECT_DIR (a forbidden system dir) the way the REST register always did.
_TEST_PROJECT_DIR = os.path.join(os.path.expanduser("~"), ".voxherd-test-projects", "shared")

# A pane stuck on one edit: the same activity ("writing") every tick.
STUCK_ON_AN_EDIT = """\
  Edit bridge/server.py
  Applied 3 changes
  ⠼ Verifying...
"""


class FakeTts:
    def __init__(self) -> None:
        self.said: list[str] = []

    def speak(self, text: str, **_kwargs) -> None:
        self.said.append(text)


@pytest.fixture
def mgr(tmp_path, monkeypatch) -> SessionManager:
    monkeypatch.setattr(session_manager, "_PERSIST_PATH", str(tmp_path / "sessions.json"))
    # A clock knows a new turn by the turn's start, so give every start its own
    # value, even on a platform whose clock is too coarse to tell two calls apart.
    monkeypatch.setattr(session_manager, "time", SimpleNamespace(monotonic=itertools.count(1.0).__next__))
    m = SessionManager()
    m._sessions.clear()
    return m


def _engine(sessions, verbosity: Verbosity = Verbosity.VERBOSE, floor: bool = False):
    tts, sent = FakeTts(), []

    async def broadcast(msg: dict) -> None:
        sent.append(msg)

    engine = NarrationEngine(
        tts=tts, broadcast_fn=broadcast, log_fn=lambda *a: None, sessions=sessions,
        floor_held=lambda: floor,
    )
    engine.set_verbosity(verbosity)
    return engine, tts, sent


def _working(mgr: SessionManager, sid: str = "s-1", *, tmux: str | None = "vh-voxherd:0.0",
             quiet: bool = False):
    session, _ = mgr.register_session(sid, "voxherd", _TEST_PROJECT_DIR, tmux_target=tmux, quiet=quiet)
    assert session.status == "active"
    return session


def _stalls(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m["type"] == "narration" and m["event_type"] == "stalled"]


# --- the announcement -----------------------------------------------------------


async def test_a_stalled_session_is_announced_once(mgr) -> None:
    engine, tts, sent = _engine(mgr)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)  # the clock starts
    await engine.check_stalls(now=t0 + 59)
    assert sent == []
    await engine.check_stalls(now=t0 + 61)
    assert tts.said == ["voxherd appears stalled."]
    assert [(m["session_id"], m["tts_text"]) for m in _stalls(sent)] == [
        ("s-1", "voxherd appears stalled."),
    ]
    await engine.check_stalls(now=t0 + 300)
    assert len(_stalls(sent)) == 1


@pytest.mark.parametrize("verbosity", [Verbosity.NORMAL, Verbosity.QUIET])
async def test_only_verbose_announces_stalls(mgr, verbosity) -> None:
    engine, tts, sent = _engine(mgr, verbosity)
    _working(mgr)
    t0 = time.monotonic()
    for dt in (0, 61, 300):
        await engine.check_stalls(now=t0 + dt)
    assert tts.said == []
    assert sent == []


async def test_switching_to_verbose_hears_at_once_about_a_stuck_session(mgr) -> None:
    # The clocks run whatever the verbosity: "tell me everything" should not
    # wait another minute to mention the session that has been stuck for five.
    engine, _, sent = _engine(mgr, Verbosity.NORMAL)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.check_stalls(now=t0 + 300)
    engine.set_verbosity(Verbosity.VERBOSE)
    await engine.check_stalls(now=t0 + 301.5)
    assert len(_stalls(sent)) == 1


# --- what counts as progress --------------------------------------------------------


async def test_new_activity_starts_the_clock_over(mgr) -> None:
    engine, _, sent = _engine(mgr)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.on_activity_change("s-1", "voxherd", "thinking", "writing", now=t0 + 50)
    await engine.check_stalls(now=t0 + 61)
    assert sent == []
    await engine.check_stalls(now=t0 + 111)
    assert len(_stalls(sent)) == 1


async def test_working_again_rearms_the_warning(mgr) -> None:
    engine, _, sent = _engine(mgr)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.check_stalls(now=t0 + 61)
    await engine.on_activity_change("s-1", "voxherd", "running", "writing", now=t0 + 70)
    await engine.check_stalls(now=t0 + 129)
    assert len(_stalls(sent)) == 1
    await engine.check_stalls(now=t0 + 131)
    assert len(_stalls(sent)) == 2


async def test_a_new_command_does_not_inherit_the_idle_stretch_before_it(mgr) -> None:
    # The common case: the session sat idle for ten minutes, then the user
    # typed at the Mac. Counting from its last activity would call it stalled
    # within a second of starting.
    engine, _, sent = _engine(mgr)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    mgr.update_status("s-1", "idle")
    await engine.check_stalls(now=t0 + 30)
    mgr.update_status("s-1", "active", activity_type="thinking")
    await engine.check_stalls(now=t0 + 600)
    await engine.check_stalls(now=t0 + 659)
    assert sent == []
    await engine.check_stalls(now=t0 + 661)
    assert len(_stalls(sent)) == 1


async def test_a_session_that_becomes_active_again_can_be_warned_again(mgr) -> None:
    engine, _, sent = _engine(mgr)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.check_stalls(now=t0 + 61)  # warned: turn one
    mgr.update_status("s-1", "idle")
    await engine.check_stalls(now=t0 + 200)
    mgr.update_status("s-1", "active", activity_type="working")
    await engine.check_stalls(now=t0 + 300)
    await engine.check_stalls(now=t0 + 361)  # turn two, stuck from its start
    assert len(_stalls(sent)) == 2


async def test_a_turn_that_ends_and_restarts_between_checks_is_still_a_new_turn(mgr) -> None:
    # A queued command is sent the moment a turn ends, so the session can be
    # idle and active again inside one poll tick: no check ever sees it idle.
    engine, _, sent = _engine(mgr)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.check_stalls(now=t0 + 61)  # warned: turn one
    mgr.update_status("s-1", "idle")
    mgr.update_status("s-1", "active", activity_type="working")
    await engine.check_stalls(now=t0 + 62)
    await engine.check_stalls(now=t0 + 121)
    assert len(_stalls(sent)) == 1
    await engine.check_stalls(now=t0 + 123)
    assert len(_stalls(sent)) == 2


async def test_one_agents_progress_does_not_vouch_for_another(mgr) -> None:
    engine, tts, _ = _engine(mgr)
    _working(mgr, "s-1", tmux="vh-voxherd:0.0")
    _working(mgr, "s-2", tmux="vh-voxherd:1.0")
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.on_activity_change("s-1", "voxherd", "thinking", "writing", now=t0 + 50)
    await engine.check_stalls(now=t0 + 61)
    assert tts.said == ["agent 2 on voxherd appears stalled."]


# --- who is never called stalled -------------------------------------------------


async def test_a_quiet_session_is_never_announced(mgr) -> None:
    engine, tts, sent = _engine(mgr)
    _working(mgr, quiet=True)
    t0 = time.monotonic()
    for dt in (0, 61, 300):
        await engine.check_stalls(now=t0 + dt)
    assert tts.said == []
    assert sent == []


async def test_a_session_outside_tmux_is_never_announced(mgr) -> None:
    # Only panes are polled. A session with none is "active" from its
    # registration until its Stop hook, however hard it is working.
    engine, _, sent = _engine(mgr)
    _working(mgr, tmux=None)
    t0 = time.monotonic()
    for dt in (0, 61, 300):
        await engine.check_stalls(now=t0 + dt)
    assert sent == []


async def test_a_session_waiting_on_the_user_is_not_stalled(mgr) -> None:
    engine, _, sent = _engine(mgr)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    mgr.update_status("s-1", "waiting")
    await engine.check_stalls(now=t0 + 61)
    await engine.check_stalls(now=t0 + 300)
    assert sent == []


async def test_a_session_that_stops_working_leaves_no_clock_behind(mgr) -> None:
    # The bridge runs for weeks; every session it ever watched must not.
    engine, _, _ = _engine(mgr)
    _working(mgr, "s-1", tmux="vh-voxherd:0.0")
    _working(mgr, "s-2", tmux="vh-voxherd:1.0")
    await engine.check_stalls()
    assert set(engine._stall_clocks) == {"s-1", "s-2"}
    mgr.update_status("s-1", "waiting")
    mgr.remove_session("s-2")
    await engine.check_stalls()
    assert engine._stall_clocks == {}


async def test_a_stall_waits_out_the_projects_cooldown(mgr) -> None:
    engine, _, sent = _engine(mgr)
    engine._stall_threshold = 5.0
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.on_notification("voxherd", "s-1", "Claude needs your permission to use Bash")
    await engine.check_stalls(now=t0 + 6)  # stalled, but voxherd has just spoken
    assert _stalls(sent) == []
    await engine.check_stalls(now=t0 + 30)  # deferred, not dropped
    assert len(_stalls(sent)) == 1


async def test_over_a_headset_a_stall_is_neither_spoken_nor_sent(mgr) -> None:
    engine, tts, sent = _engine(mgr, floor=True)
    _working(mgr)
    t0 = time.monotonic()
    await engine.check_stalls(now=t0)
    await engine.check_stalls(now=t0 + 61)
    assert tts.said == []
    assert sent == []


# --- the poll loop runs it ---------------------------------------------------------


async def _until(done, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)


@contextlib.asynccontextmanager
async def _polling():
    task = asyncio.create_task(activity._activity_poll_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_the_poll_loop_announces_a_session_stuck_on_one_activity(tmp_path, monkeypatch) -> None:
    """The whole path: the loop polls a pane that shows the same edit forever,
    reports the one change it sees, and calls the check after each tick."""
    monkeypatch.setattr(session_manager, "_PERSIST_PATH", str(tmp_path / "sessions.json"))
    herd = SessionManager()
    herd._sessions.clear()
    herd.register_session("s-1", "voxherd", _TEST_PROJECT_DIR, tmux_target="vh-voxherd:0.0")

    async def pane_exists(_target):
        return True

    async def claude_in_front(_target):
        return "2.1.283"

    async def capture(*_args, **_kwargs):
        return SimpleNamespace(communicate=AsyncMock(return_value=(STUCK_ON_AN_EDIT.encode(), b"")))

    monkeypatch.setattr(herd, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(activity, "sessions", herd)
    monkeypatch.setattr(activity, "_pane_fg_command", claude_in_front)
    monkeypatch.setattr("asyncio.create_subprocess_exec", capture)
    monkeypatch.setattr(activity, "broadcast_to_ios", AsyncMock())
    monkeypatch.setattr(activity, "_state_sync_msg", lambda: {"type": "state_sync"})
    monkeypatch.setattr(activity, "_LAST_REAL_ACTIVITY", {})
    monkeypatch.setattr(activity, "_STICKY_ACTIVITY", {})

    engine, tts, sent = _engine(herd)
    engine._stall_threshold = 0.5  # a tick is 1.5 s; the second one finds it stalled
    monkeypatch.setattr(state, "narration", engine)

    async with _polling():
        await _until(lambda: _stalls(sent), timeout=6.0)
    assert tts.said == ["voxherd appears stalled."]


async def test_a_failing_check_is_logged_and_the_loop_goes_on(tmp_path, monkeypatch) -> None:
    # An exception escaping here would end the poll loop for good, and with it
    # auto-idle, choice prompts and the terminal mirror.
    monkeypatch.setattr(session_manager, "_PERSIST_PATH", str(tmp_path / "sessions.json"))
    herd = SessionManager()
    herd._sessions.clear()
    monkeypatch.setattr(activity, "sessions", herd)
    broken = SimpleNamespace(check_stalls=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(state, "narration", broken)
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(activity, "log_event", lambda level, _project, msg: logged.append((level, msg)))

    async with _polling():
        await _until(lambda: broken.check_stalls.await_count >= 2, timeout=6.0)
    assert broken.check_stalls.await_count >= 2
    assert ("error", "Stall check failed: RuntimeError: boom") in logged
