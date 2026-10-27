"""The Linux speech backend: the queue must keep moving.

``linux_tts`` had no tests at all, and Linux is a shipped systemd deployment
(``.claude/rules/linux.md``) -- the one platform where the bridge runs as a
service whose stdin is whatever systemd gave it. Three things here are exactly
the wedges ``mac_tts`` was fixed for, in a twin that was not:

* ``--`` before the text, so a leading dash is not read as a flag. ``espeak``
  has ``-w<file>``, which writes a WAV to a path the caller chose.
* ``stdin=DEVNULL``, because espeak with no text left to speak reads stdin
  forever, and this queue drains one item at a time.
* A bounded wait, so one process that never exits does not mean permanent
  silence with nothing logged.

The tests force the engine rather than detecting one, so they run on any host.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


async def _until(predicate, timeout: float = 5.0) -> None:
    """Poll *predicate* until true, or fail.

    Replaces fixed `asyncio.sleep(0.05)`/`0.6` waits between `speak()` and the
    worker. Those are the only synchronisation there is, and on a loaded host --
    the kind a load average in the hundreds produces -- they failed as
    `spawned` simply empty, or "the next utterance never got spoken": a false
    red that reads exactly like the real bug.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true within the deadline")

from bridge.linux_tts import LinuxTTS, SpeechItem, _find_tts_binary


@pytest.fixture
def tts():
    """A LinuxTTS with espeak-ng forced on, whatever the host actually has."""
    engine = LinuxTTS(voice="en", rate=175)
    engine._binary = "/usr/bin/espeak-ng"
    engine._engine = "espeak-ng"
    return engine


class FakeProc:
    """A spawned process that can be made to hang."""

    def __init__(self, *, hangs: bool = False) -> None:
        self.hangs = hangs
        self.killed = False
        self.returncode = 0
        self.stdin_payload: bytes | None = None
        self.finished = asyncio.Event()   # set when the worker is done with it
        self._done = asyncio.Event()

    async def wait(self):
        if self.hangs and not self.killed:
            await self._done.wait()  # only the kill below releases this
        self.finished.set()
        return self.returncode

    async def communicate(self, input=None):  # noqa: A002 - matches asyncio API
        # Recorded, because for piper this IS the utterance: `_build_cmd`
        # returns only `[binary, "--output-raw"]`, so the text reaches the
        # synthesiser nowhere else. A fake that threw `input` away let
        # `proc.communicate()` -- no text at all, every announcement silent on
        # a Linux host -- pass all eleven tests.
        self.stdin_payload = input
        await self.wait()
        return (b"", b"")

    def kill(self):
        self.killed = True
        self._done.set()


class TestTheCommandLine:
    def test_the_text_is_separated_from_the_flags(self, tts):
        cmd = tts._build_cmd("hello")
        assert "--" in cmd
        assert cmd.index("--") == len(cmd) - 2, "`--` must be the last thing before the text"
        assert cmd[-1] == "hello"

    def test_a_dash_leading_utterance_cannot_become_a_flag(self, tts):
        """`-w/tmp/x.wav` would otherwise write a file instead of speaking."""
        cmd = tts._build_cmd("-w/tmp/pwned.wav")
        assert cmd[cmd.index("--") + 1] == "-w/tmp/pwned.wav"

    def test_plain_espeak_is_protected_too(self, tts):
        tts._engine = "espeak"
        assert "--" in tts._build_cmd("-x")


class TestTheWorker:
    async def test_espeak_never_inherits_the_services_stdin(self, tts):
        """The wedge: no text left to speak, so it reads stdin forever."""
        spawned = {}

        async def fake_exec(*args, **kwargs):
            spawned.update(kwargs)
            return FakeProc()

        with patch("asyncio.create_subprocess_exec", fake_exec):
            tts.start()
            tts.speak("hello")
            await _until(lambda: bool(spawned))
            tts.stop()

        assert spawned.get("stdin") == asyncio.subprocess.DEVNULL

    async def test_a_hung_utterance_is_killed_and_the_queue_moves_on(self, tts):
        """One process that never exits must not mean permanent silence."""
        procs: list[FakeProc] = []

        async def fake_exec(*args, **kwargs):
            proc = FakeProc(hangs=len(procs) == 0)
            procs.append(proc)
            return proc

        with patch("asyncio.create_subprocess_exec", fake_exec), \
             patch("bridge.linux_tts.utterance_timeout", lambda _text: 0.05):
            tts.start()
            tts.speak("the one that hangs")
            tts.speak("the one after it")
            await _until(lambda: len(procs) == 2 and procs[0].killed)
            tts.stop()

        assert procs[0].killed, "the hung process was never killed"
        assert len(procs) == 2, "the next utterance never got spoken"

    async def test_an_utterance_that_finishes_is_not_killed(self, tts):
        procs: list[FakeProc] = []

        async def fake_exec(*args, **kwargs):
            procs.append(FakeProc())
            return procs[-1]

        with patch("asyncio.create_subprocess_exec", fake_exec):
            tts.start()
            tts.speak("short")
            await _until(lambda: bool(procs) and procs[0].finished.is_set())
            tts.stop()

        assert procs and not procs[0].killed

    async def test_the_completion_callback_still_fires(self, tts):
        """The voice loop hangs off this; the bounded wait must not drop it."""
        seen: list[SpeechItem] = []

        async def on_complete(item):
            seen.append(item)

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        tts.on_speech_complete = on_complete
        with patch("asyncio.create_subprocess_exec", fake_exec):
            tts.start()
            tts.speak("hello", listen_after=True)
            await _until(lambda: bool(seen))
            tts.stop()

        assert [i.text for i in seen] == ["hello"]

    async def test_a_hung_piper_is_also_bounded(self, tts):
        tts._engine = "piper-tts"
        tts._binary = "/usr/bin/piper-tts"
        procs: list[FakeProc] = []

        async def fake_exec(*args, **kwargs):
            procs.append(FakeProc(hangs=True))
            return procs[-1]

        with patch("asyncio.create_subprocess_exec", fake_exec), \
             patch("bridge.linux_tts.utterance_timeout", lambda _text: 0.05):
            tts.start()
            tts.speak("hello")
            await _until(lambda: bool(procs) and procs[0].killed)
            tts.stop()

        assert procs[0].killed


class TestTheBudget:
    def test_a_long_utterance_gets_more_than_the_floor(self):
        from bridge.tts_budget import TIMEOUT_FLOOR_S, utterance_timeout

        # `narration` batches up to five summaries into one utterance, so this
        # is a real size, and a flat cap cut it off mid-sentence.
        assert utterance_timeout("x" * 10_000) > TIMEOUT_FLOOR_S
        assert utterance_timeout("hi") == TIMEOUT_FLOOR_S

    def test_the_queue_is_not_created_before_a_loop_exists(self):
        """server_state instantiates this at import; see CLAUDE.md.

        A queue built here binds to whatever default loop is around, and
        ``await queue.get()`` on uvicorn's loop then never sees a
        ``put_nowait`` -- the documented cause of silent TTS.
        """
        assert LinuxTTS()._queue is None


def test_binary_detection_reports_nothing_rather_than_guessing():
    """On a host with no synthesiser, ``available`` must be False, not a crash."""
    with patch("shutil.which", lambda _name: None):
        assert _find_tts_binary() == (None, "")
        assert not LinuxTTS().available


class TestPiperGetsTheUtterance:
    """piper is handed its text on stdin and nowhere else.

    `_build_cmd` returns `[binary, "--output-raw"]` for piper -- no text
    argument -- so `communicate(input=...)` is the only way the words arrive.
    Changing it to a bare `proc.communicate()` (piper synthesising silence for
    every announcement on a Linux host) passed every other test in this file.
    """

    async def test_the_text_is_written_to_piper_stdin(self, tts):
        tts._engine = "piper-tts"
        tts._binary = "/usr/bin/piper-tts"
        procs: list[FakeProc] = []

        async def fake_exec(*args, **kwargs):
            procs.append(FakeProc())
            return procs[-1]

        with patch("asyncio.create_subprocess_exec", fake_exec):
            tts.start()
            tts.speak("speak these exact words")
            await _until(lambda: bool(procs) and procs[0].finished.is_set())
            tts.stop()

        assert procs[0].stdin_payload == b"speak these exact words"

    async def test_non_ascii_survives_the_encode(self, tts):
        tts._engine = "piper-tts"
        tts._binary = "/usr/bin/piper-tts"
        procs: list[FakeProc] = []

        async def fake_exec(*args, **kwargs):
            procs.append(FakeProc())
            return procs[-1]

        with patch("asyncio.create_subprocess_exec", fake_exec):
            tts.start()
            tts.speak("caf\u00e9 \u2014 fa\u00e7ade")
            await _until(lambda: bool(procs) and procs[0].finished.is_set())
            tts.stop()

        assert procs[0].stdin_payload is not None
        assert procs[0].stdin_payload.decode() == "caf\u00e9 \u2014 fa\u00e7ade"
