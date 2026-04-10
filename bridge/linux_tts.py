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
        self._queue: asyncio.Queue[SpeechItem] = asyncio.Queue(maxsize=50)
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
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self._queue.put_nowait(item)

    def _build_cmd(self, text: str) -> list[str]:
        """Build the command line for the detected TTS engine."""
        if self._engine == "espeak-ng":
            return [self._binary, "-v", self.voice, "-s", str(self.rate), text]
        elif self._engine == "espeak":
            return [self._binary, "-v", self.voice, "-s", str(self.rate), text]
        elif self._engine == "piper-tts":
            # piper reads from stdin
            return [self._binary, "--output-raw"]
        return []

    async def _worker(self) -> None:
        """Drain the queue sequentially so utterances don't overlap."""
        while True:
            item = await self._queue.get()
            try:
                if self._engine in ("espeak-ng", "espeak"):
                    cmd = self._build_cmd(item.text)
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=get_subprocess_env(),
                    )
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
                    await proc.communicate(input=item.text.encode())

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
