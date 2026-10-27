"""Linux text-to-speech via ``espeak-ng`` (or ``espeak``, or ``piper-tts``).

Drop-in replacement for ``mac_tts.MacTTS`` on Linux systems.
Auto-detects which TTS binary is available. Announcements are queued
to prevent overlapping speech, same as the macOS version.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from bridge.env_utils import get_subprocess_env
from bridge.tts_budget import utterance_timeout


@dataclass
class SpeechItem:
    """A queued TTS utterance with metadata for the voice loop."""

    text: str
    project: str | None = None
    session_id: str | None = None
    listen_after: bool = False


def _find_tts_binary() -> tuple[str | None, str]:
    """Find an available TTS binary. Returns (path, engine_name)."""
    for name in ("espeak-ng", "espeak", "piper-tts"):
        path = shutil.which(name)
        if path:
            return path, name
    return None, ""


class LinuxTTS:
    """Async wrapper around Linux TTS commands with queuing."""

    def __init__(self, voice: str = "en", rate: int = 175) -> None:
        self.voice = voice
        self.rate = rate
        self.enabled = True
        # Created in start(), NOT here. server_state.py instantiates this
        # class at module IMPORT, before uvicorn has built the loop it will run
        # the server on; an asyncio.Queue made at that point can bind to a
        # different loop, and `await queue.get()` on the worker then never sees
        # a `put_nowait` from a request handler. That is the documented cause of
        # silent TTS in the bundled macOS app (see "Important Constraints" in
        # CLAUDE.md) -- mac_tts.py was converted and these twins were not.
        self._queue: asyncio.Queue[SpeechItem] | None = None
        self._worker_task: asyncio.Task | None = None
        self._binary, self._engine = _find_tts_binary()

        # Callback fired after each utterance completes. Signature:
        #   async (SpeechItem) -> None
        # Matches MacTTS interface for voice loop compatibility.
        self.on_speech_complete: Callable[[SpeechItem], Awaitable[None]] | None = None

    @property
    def available(self) -> bool:
        return self._binary is not None

    def start(self) -> None:
        """Start the background worker that drains the speech queue."""
        if self._worker_task is None:
            self._queue = asyncio.Queue(maxsize=50)
            self._worker_task = asyncio.create_task(self._worker())

    def stop(self) -> None:
        """Cancel the background worker."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            self._worker_task = None

    def speak(
        self,
        text: str,
        *,
        project: str | None = None,
        session_id: str | None = None,
        listen_after: bool = False,
    ) -> None:
        """Enqueue *text* for speech. Non-blocking, fire-and-forget."""
        if self.enabled and self._binary:
            item = SpeechItem(
                text=text,
                project=project,
                session_id=session_id,
                listen_after=listen_after,
            )
            if self._queue is None:
                return
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self._queue.put_nowait(item)

    def _build_cmd(self, text: str) -> list[str]:
        """Build the command line for the detected TTS engine."""
        # "--" before the text, as in mac_tts: espeak reads a dash-leading
        # argument as a flag, and `-w<file>` writes a WAV to an arbitrary path.
        if self._engine == "espeak-ng":
            return [self._binary, "-v", self.voice, "-s", str(self.rate), "--", text]
        elif self._engine == "espeak":
            return [self._binary, "-v", self.voice, "-s", str(self.rate), "--", text]
        elif self._engine == "piper-tts":
            # piper reads its text from stdin.
            #
            # KNOWN GAP, pre-existing and not fixed here: `--output-raw` writes
            # raw PCM to stdout for piping into a player (`aplay`), and the
            # worker sends stdout to DEVNULL -- so the audio is discarded. There
            # is no `-m <model>` either, which piper requires. So on a host with
            # piper and no espeak-ng, `_find_tts_binary` picks piper,
            # `available` reports True, every announcement is queued and logged
            # as spoken, and nobody hears anything.
            #
            # Fixing it is a design choice (pipe into a player and own that
            # dependency, or drop piper from detection so `available` is honest)
            # rather than a patch, which is why it is written down instead.
            return [self._binary, "--output-raw"]
        return []

    async def _worker(self) -> None:
        """Drain the queue sequentially so utterances don't overlap."""
        while True:
            assert self._queue is not None  # start() created it
            item = await self._queue.get()
            try:
                if self._engine in ("espeak-ng", "espeak"):
                    cmd = self._build_cmd(item.text)
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        # espeak with no text left to speak reads stdin forever,
                        # and this queue drains one item at a time: inheriting
                        # the bridge's stdin wedged every later announcement.
                        # `--` in _build_cmd stops the text being taken as a
                        # flag; this closes the other door. Linux is a shipped
                        # systemd deployment, where stdin is whatever the
                        # service inherited.
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=get_subprocess_env(),
                    )
                    # Bounded, for the same reason: an unbounded wait here is
                    # one hung process away from permanent silence.
                    budget = utterance_timeout(item.text)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=budget)
                    except (TimeoutError, asyncio.TimeoutError):
                        try:
                            proc.kill()
                        except (ProcessLookupError, OSError):
                            pass
                        await proc.wait()
                elif self._engine == "piper-tts":
                    # piper reads text from stdin, outputs audio
                    proc = await asyncio.create_subprocess_exec(
                        self._binary, "--output-raw",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=get_subprocess_env(),
                    )
                    # piper is handed its text on stdin, so it cannot wedge
                    # reading the bridge's -- but it can still fail to exit.
                    budget = utterance_timeout(item.text)
                    try:
                        await asyncio.wait_for(
                            proc.communicate(input=item.text.encode()), timeout=budget
                        )
                    except (TimeoutError, asyncio.TimeoutError):
                        try:
                            proc.kill()
                        except (ProcessLookupError, OSError):
                            pass
                        await proc.wait()

                # Fire callback after speech completes.
                if self.on_speech_complete and item.listen_after:
                    try:
                        await self.on_speech_complete(item)
                    except Exception:
                        pass  # best-effort
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # best-effort — don't crash if TTS fails
