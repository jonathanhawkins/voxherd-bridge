"""OpenAI TTS backend using gpt-4o-mini-tts with Nova voice.

Drop-in replacement for MacTTS — same interface (.speak(), .start(),
.stop(), .enabled, .available). Falls back gracefully if no OPENAI_API_KEY
is set or API calls fail.

Audio is played via ``afplay`` (macOS) or ``aplay`` (Linux).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

_PLAY_CMD: str | None = None
if sys.platform == "darwin":
    _PLAY_CMD = shutil.which("afplay")
elif sys.platform != "win32":
    _PLAY_CMD = shutil.which("aplay")


@dataclass
class SpeechItem:
    """A queued TTS utterance with metadata for the voice loop."""

    text: str
    project: str | None = None
    session_id: str | None = None
    listen_after: bool = False


class OpenAITTS:
    """Async OpenAI TTS with queuing, matching the MacTTS interface."""

    def __init__(
        self,
        voice: str = "nova",
        instruction: str = "Speak in a cheerful, positive yet professional tone.",
    ) -> None:
        self.voice = voice
        self.instruction = instruction
        self.enabled = True
        self._queue: asyncio.Queue[SpeechItem] = asyncio.Queue(maxsize=50)
        self._worker_task: asyncio.Task | None = None
        self._api_key = os.environ.get("OPENAI_API_KEY", "")
        self._available = bool(self._api_key and _PLAY_CMD)

        self.on_speech_complete: Callable[[SpeechItem], Awaitable[None]] | None = None

    @property
    def available(self) -> bool:
        return self._available

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
        if self.enabled and self._available:
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

    async def _worker(self) -> None:
        """Drain the queue sequentially so utterances don't overlap."""
        try:
            import httpx
        except ImportError:
            log.error("httpx not installed — OpenAI TTS disabled")
            self._available = False
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                item = await self._queue.get()
                tmp_path = None
                try:
                    resp = await client.post(
                        "https://api.openai.com/v1/audio/speech",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={
                            "model": "gpt-4o-mini-tts",
                            "voice": self.voice,
                            "input": item.text,
                            "instructions": self.instruction,
                            "response_format": "mp3",
                        },
                    )
                    resp.raise_for_status()

                    # Write to a temp file and play
                    fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="vh-tts-")
                    os.write(fd, resp.content)
                    os.close(fd)

                    proc = await asyncio.create_subprocess_exec(
                        _PLAY_CMD, tmp_path,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()

                    if self.on_speech_complete and item.listen_after:
                        try:
                            await self.on_speech_complete(item)
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("OpenAI TTS failed: %s", exc)
                finally:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
