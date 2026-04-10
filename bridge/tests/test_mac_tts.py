"""Regression tests for bridge/mac_tts.py.

These tests lock in the fix for a real shipping bug: TTS silently dropped
every utterance inside the frozen VoxHerdBridge.app. ``/api/tts`` happily
returned ``{"ok": true}``, items enqueued, no ``say`` subprocess ever
spawned, user heard nothing, nothing in the logs.

Two overlapping root causes were uncovered and fixed:

1. **Cross-loop queue binding.** ``MacTTS()`` is instantiated at
   module-import time in ``server_state.py``. In Python 3.9, calling
   ``asyncio.Queue()`` outside a running loop binds it to a throwaway
   default loop via ``get_event_loop()``. Uvicorn later spins up a
   *different* loop to run the server, so ``await queue.get()`` in the
   worker hangs forever — the getter future's wakeup is scheduled on
   the dead loop. ``put_nowait`` appears to succeed but the worker never
   drains. Fix: create the queue lazily in ``start()`` inside uvicorn's
   loop.

2. **Fragile shutil.which probe.** ``__init__`` gated ``_available`` on
   ``shutil.which("say") is not None``. ``say`` is a required macOS
   system binary at ``/usr/bin/say`` and must never be probed this way
   — in frozen ``.app`` bundles the probe has been observed to
   misbehave, and even in the normal case it's fake defense. Fix:
   trust the binary, always True, and pass ``env=get_subprocess_env()``
   to the actual invocation.

Both fixes live in ``bridge/mac_tts.py`` with comments pointing back here.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="mac_tts is macOS-only",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stripped_path(monkeypatch):
    """Simulate a launchd/Launch Services environment whose PATH does
    NOT include /usr/bin at process startup.
    """
    monkeypatch.setenv("PATH", "/nonexistent/path-that-does-not-contain-say")
    yield


# ---------------------------------------------------------------------------
# shutil.which fragility — bug #2
# ---------------------------------------------------------------------------


def test_mac_tts_available_under_stripped_path(stripped_path):
    """``MacTTS.available`` must be True even when PATH excludes /usr/bin."""
    from bridge.mac_tts import MacTTS

    tts = MacTTS(voice="Samantha")  # skip auto-detect to keep the test hermetic
    assert tts.available is True, (
        "MacTTS.available must be True even under a PATH that excludes "
        "/usr/bin — see bridge/mac_tts.py comment for rationale."
    )
    assert tts.enabled is True


def test_mac_tts_init_does_not_probe_path_for_say(stripped_path):
    """Guard against anyone reintroducing a ``shutil.which('say')`` probe."""
    import shutil as _shutil

    real_which = _shutil.which

    def guarded_which(cmd, *args, **kwargs):
        if cmd == "say":
            raise AssertionError(
                "MacTTS.__init__ must not call shutil.which('say') — "
                "it silently returns None inside frozen .app bundles. "
                "Trust the binary and use get_subprocess_env() at invocation."
            )
        return real_which(cmd, *args, **kwargs)

    with patch.object(_shutil, "which", side_effect=guarded_which):
        from bridge.mac_tts import MacTTS
        MacTTS(voice="Samantha")  # must not raise


def test_detect_best_voice_works_under_stripped_path(stripped_path):
    """``detect_best_voice()`` must run ``say -v '?'`` successfully under
    a stripped PATH, which requires threading ``get_subprocess_env()``
    into the subprocess call.
    """
    from bridge.mac_tts import detect_best_voice

    voice = detect_best_voice()
    assert isinstance(voice, str) and voice, (
        "detect_best_voice must return a non-empty voice name under a "
        "stripped PATH — bridge/mac_tts.py must pass "
        "env=get_subprocess_env() to subprocess.run"
    )


# ---------------------------------------------------------------------------
# Cross-loop queue binding — bug #1 (the one that actually caused silence)
# ---------------------------------------------------------------------------


def test_init_does_not_create_queue_outside_running_loop():
    """``MacTTS.__init__`` must NOT create ``asyncio.Queue`` when there
    is no running event loop.

    If it did, the queue would bind to a default loop that uvicorn
    never touches, and the worker task created later (on uvicorn's
    loop) would hang forever on ``await queue.get()`` — exactly the
    bug that silenced TTS in the frozen bundle.
    """
    from bridge.mac_tts import MacTTS

    # Ensure we really are outside any running loop.
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()

    tts = MacTTS(voice="Samantha")
    assert tts._queue is None, (
        "Queue must be created lazily in start(), not in __init__. "
        "Creating it at module-import time binds it to a stale loop "
        "and causes the worker to hang."
    )


def test_speak_before_start_drops_silently():
    """Items sent to speak() before start() is called must be dropped
    (not raise). Typical cause: early-startup code paths or tests that
    skip start().
    """
    from bridge.mac_tts import MacTTS

    tts = MacTTS(voice="Samantha")
    tts.speak("this should be silently dropped, not raise")  # must not raise


def test_speak_works_when_macttsb_is_instantiated_outside_loop():
    """End-to-end cross-loop regression.

    Simulate the production shape: ``MacTTS()`` is created in a sync
    context (module-import time), THEN ``asyncio.run()`` is called to
    stand up a server, THEN ``start()`` and ``speak()`` run inside
    that fresh loop. The worker must successfully drain the item.

    Before the fix, this would hang because the queue was bound to
    a loop that wasn't the one ``asyncio.run`` created.
    """
    from bridge.mac_tts import MacTTS

    # Step 1: create the instance outside any running loop.
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
    tts = MacTTS(voice="Samantha")

    subprocess_calls: list[tuple] = []

    async def fake_subprocess_exec(*args, **kwargs):
        subprocess_calls.append((args, kwargs))
        proc = AsyncMock()
        proc.wait = AsyncMock(return_value=0)
        proc.pid = 99999
        return proc

    async def scenario() -> None:
        with patch(
            "bridge.mac_tts.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess_exec,
        ):
            tts.start()
            tts.speak("cross-loop regression probe")
            # Give the worker a chance to drain. A correct implementation
            # picks up the item on the next loop tick; a broken one hangs.
            for _ in range(50):
                if subprocess_calls:
                    break
                await asyncio.sleep(0.02)
            tts.stop()

    asyncio.run(scenario())

    assert len(subprocess_calls) == 1, (
        "Worker must drain the queue when MacTTS is instantiated outside "
        "the loop that later runs start()/speak(). Zero calls here means "
        "the queue was bound to a stale loop and the worker hung on "
        "queue.get() — the exact bug that silenced TTS in the frozen bundle."
    )
    args, kwargs = subprocess_calls[0]
    assert args[0] == "say"
    assert kwargs.get("env", {}).get("PATH", "").find("/usr/bin") >= 0


# ---------------------------------------------------------------------------
# End-to-end: start() + speak() + worker drain under stripped PATH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speak_invokes_subprocess_under_stripped_path(stripped_path):
    """speak() -> worker must actually spawn a subprocess even under a
    stripped PATH — the worker uses ``get_subprocess_env()`` which
    augments PATH regardless of what the parent was launched with.
    """
    from bridge.mac_tts import MacTTS

    tts = MacTTS(voice="Samantha")
    tts.start()
    try:
        with patch(
            "bridge.mac_tts.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=0)
            mock_exec.return_value = mock_proc

            tts.speak("regression probe under stripped path")

            for _ in range(50):
                if mock_exec.await_count > 0:
                    break
                await asyncio.sleep(0.02)

            assert mock_exec.await_count == 1, (
                "Worker must invoke asyncio.create_subprocess_exec exactly "
                "once per queued utterance."
            )
            args, kwargs = mock_exec.call_args
            assert args[0] == "say", f"expected 'say' as argv[0], got {args[0]!r}"
            assert "env" in kwargs and kwargs["env"].get("PATH"), (
                "subprocess_exec must be called with env=get_subprocess_env(), "
                "whose PATH is non-empty"
            )
            assert "/usr/bin" in kwargs["env"]["PATH"], (
                "get_subprocess_env() must add /usr/bin to PATH so that "
                "`say` resolves inside frozen bundles"
            )
    finally:
        tts.stop()
