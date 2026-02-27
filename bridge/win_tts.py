"""Windows text-to-speech via the ``pyttsx3`` library (wraps Windows SAPI5).

Drop-in replacement for ``mac_tts.MacTTS`` and ``linux_tts.LinuxTTS`` on
Windows. Announcements are queued to prevent overlapping speech, same
interface as the other platform TTS modules.

Falls back to a no-op if pyttsx3 is not installed.
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class SpeechItem:
    """A queued TTS utterance with metadata for the voice loop."""

    text: str
    project: str | None = None
    session_id: str | None = None
    listen_after: bool = False


def _check_pyttsx3() -> bool:
    """Check if pyttsx3 is available."""
    try:
        import pyttsx3  # noqa: F401
        return True
    except ImportError:
        return False


class WinTTS:
    """Async wrapper around Windows SAPI5 TTS via pyttsx3 with queuing.

    pyttsx3 uses COM and must run on a dedicated thread. Speech commands
    are dispatched to that thread via a threading.Event + queue pattern,
    while the public API remains async to match MacTTS/LinuxTTS.
    """

    def __init__(self, voice: str = "auto", rate: int = 200) -> None:
        self.voice = voice
        self.rate = rate
        self.enabled = True
        self._queue: asyncio.Queue[SpeechItem] = asyncio.Queue(maxsize=50)
        self._worker_task: asyncio.Task | None = None
        self._available = _check_pyttsx3()

        # Callback fired after each utterance completes. Signature:
        #   async (SpeechItem) -> None
        # Matches MacTTS/LinuxTTS interface for voice loop compatibility.
        self.on_speech_complete: Callable[[SpeechItem], Awaitable[None]] | None = None

        if self._available:
            log.info("Windows TTS available via pyttsx3 (SAPI5)")
        else:
            log.warning("pyttsx3 not installed — Windows TTS disabled")

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

    def _speak_sync(self, text: str) -> None:
        """Speak text synchronously on a background thread.

        pyttsx3 uses COM internally and requires its own thread. We create
        a fresh engine per utterance to avoid COM apartment issues with
        asyncio's thread pool.
        """
        try:
            import pyttsx3
            engine = pyttsx3.init()

            # Configure voice
            if self.voice != "auto":
                voices = engine.getProperty("voices")
                for v in voices:
                    if self.voice.lower() in v.name.lower():
                        engine.setProperty("voice", v.id)
                        break

            engine.setProperty("rate", self.rate)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception as e:
            log.debug("Windows TTS speak failed: %s", e)

    async def _worker(self) -> None:
        """Drain the queue sequentially so utterances don't overlap."""
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            try:
                # Run pyttsx3 on a thread to avoid blocking the event loop.
                await loop.run_in_executor(None, self._speak_sync, item.text)

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
