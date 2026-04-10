"""macOS text-to-speech via the ``say`` command.

Allows the bridge server to announce events audibly on the Mac,
so the full VoxHerd loop can be tested without an iOS device or glasses.
Announcements are queued to prevent overlapping speech.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from bridge.env_utils import get_subprocess_env

log = logging.getLogger(__name__)

# Preferred voices in priority order.  The first one found on the system wins.
# Premium > Enhanced > default quality.  Names must match ``say -v '?'`` output.
_PREFERRED_VOICES: list[str] = [
    # Premium neural voices (macOS 14+, must be downloaded)
    "Zoe (Premium)",
    "Evan (Premium)",
    "Ava (Premium)",
    "Tom (Premium)",
    # Enhanced voices (smaller download)
    "Samantha (Enhanced)",
    "Zoe (Enhanced)",
    "Evan (Enhanced)",
    "Ava (Enhanced)",
    # Decent defaults (always present)
    "Samantha",
    "Karen",
    "Daniel",
]


def detect_best_voice() -> str:
    """Return the highest-quality English voice installed on this Mac.

    Parses ``say -v '?'`` output and matches against ``_PREFERRED_VOICES``.
    Falls back to ``Samantha`` if nothing better is found.
    """
    try:
        result = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True, text=True, timeout=5,
            env=get_subprocess_env(),
        )
        if result.returncode != 0:
            return "Samantha"
        # Each line: "VoiceName            lang    # greeting"
        installed: set[str] = set()
        for line in result.stdout.splitlines():
            name = line.split("#")[0].rsplit(None, 1)[0].strip() if "#" in line else line.strip()
            if name:
                installed.add(name)
        for voice in _PREFERRED_VOICES:
            if voice in installed:
                return voice
    except Exception:
        pass
    return "Samantha"


@dataclass
class SpeechItem:
    """A queued TTS utterance with metadata for the voice loop."""

    text: str
    project: str | None = None
    session_id: str | None = None
    listen_after: bool = False


_QUEUE_MAXSIZE = 50


class MacTTS:
    """Async wrapper around macOS ``say`` command with queuing.

    Event-loop note: the speech queue is intentionally *not* created in
    ``__init__``. ``MacTTS()`` is instantiated at module-import time
    (``server_state.py``) before uvicorn's event loop exists. If
    ``asyncio.Queue()`` were created here, Python 3.9's ``get_event_loop()``
    would bind it to a throwaway default loop. Later, uvicorn runs the
    server on a *different* loop, and every ``await queue.get()`` in the
    worker would hang forever — ``put_nowait`` wakes the getter future on
    the dead loop, and the worker task (scheduled on uvicorn's loop) never
    sees the wakeup. Symptom: ``/api/tts`` returns ``{"ok": true}``, items
    enqueue, no ``say`` subprocess ever spawns, user hears nothing, no
    error anywhere. The queue is therefore created lazily in ``start()``,
    which runs inside uvicorn's lifespan on uvicorn's loop.
    """

    def __init__(self, voice: str = "auto", rate: int = 190) -> None:
        if voice == "auto":
            voice = detect_best_voice()
            log.info("Auto-detected TTS voice: %s", voice)
        self.voice = voice
        self.rate = rate
        self.enabled = True
        self._queue: asyncio.Queue[SpeechItem] | None = None
        self._worker_task: asyncio.Task | None = None
        # ``say`` is a required macOS system binary at /usr/bin/say. Do not
        # gate it behind ``shutil.which`` — inside frozen .app bundles the
        # probe can silently return None and flip this to False for the life
        # of the process. The worker invokes say via get_subprocess_env(),
        # which augments PATH, so the actual subprocess always resolves it.
        self._available = True

        # Callback fired after each utterance completes. Signature:
        #   async (SpeechItem) -> None
        # Set by MacVoiceLoop to trigger listen-after-speak.
        self.on_speech_complete: Callable[[SpeechItem], Awaitable[None]] | None = None

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        """Start the background worker that drains the speech queue.

        Must be called from inside a running event loop — the queue binds
        to this loop so the worker and all future ``speak()`` callers
        share it. See the class docstring for why this is lazy.
        """
        if self._worker_task is None:
            self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
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
        """Enqueue *text* for speech. Non-blocking, fire-and-forget.

        If ``start()`` has not yet run (queue not created), the item is
        dropped. In practice this only happens during early lifespan
        startup or in tests where ``start`` is mocked.
        """
        if not (self.enabled and self._available):
            return
        if self._queue is None:
            return
        item = SpeechItem(
            text=text,
            project=project,
            session_id=session_id,
            listen_after=listen_after,
        )
        if self._queue.full():
            # Drop oldest item to make room — avoids unbounded growth
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(item)

    @staticmethod
    def _insert_pause(text: str) -> str:
        """Insert a 300ms pause after the first sentence in TTS text.

        Uses macOS ``say``'s ``[[slnc 300]]`` tag for natural pacing
        between a project-name prefix and the summary body.
        """
        # Find the first period followed by a space (sentence boundary)
        idx = text.find(". ")
        if idx > 0 and idx < len(text) - 2:
            return text[:idx + 1] + " [[slnc 300]] " + text[idx + 2:]
        return text

    async def _worker(self) -> None:
        """Drain the queue sequentially so utterances don't overlap."""
        assert self._queue is not None  # start() created it before scheduling us
        while True:
            item = await self._queue.get()
            try:
                tts_text = self._insert_pause(item.text)
                proc = await asyncio.create_subprocess_exec(
                    "say", "-v", self.voice, "-r", str(self.rate), tts_text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=get_subprocess_env(),
                )
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
                pass  # best-effort — don't crash if say fails
