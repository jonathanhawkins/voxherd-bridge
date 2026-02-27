"""Shared module-level state for the VoxHerd bridge server.

All mutable state that's shared across routes, WebSocket handlers, and
background loops lives here. Other modules import from this module to
avoid circular dependencies.
"""

import asyncio
import hashlib
import hmac
import json
import sys
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect

from bridge.session_manager import SessionManager

# Platform-conditional imports: macOS modules use ctypes to load
# /usr/lib/libSystem.B.dylib at module scope, which crashes on Linux/Windows.
if sys.platform == "darwin":
    from bridge.mac_stt import MacSTT
    from bridge.mac_tts import MacTTS
    from bridge.mac_voice_loop import MacVoiceLoop
else:
    if sys.platform == "win32":
        from bridge.win_tts import WinTTS
    else:
        from bridge.linux_tts import LinuxTTS

    # Minimal STT stub — only macOS has the Swift STT binary.
    class MacSTT:  # type: ignore[no-redef]
        """No-op STT stub for non-macOS platforms."""

        def __init__(self, timeout: int = 8, locale: str = "en-US") -> None:
            self.enabled = False
            self.timeout = timeout
            self.locale = locale

        @property
        def available(self) -> bool:
            return False

        async def listen(self, timeout: int | None = None, quiet: bool = False) -> str | None:
            return None

    # Stub so `voice_loop: MacVoiceLoop | None = None` still type-checks.
    MacVoiceLoop = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Core state
# ---------------------------------------------------------------------------

sessions = SessionManager()
ios_connections: list[WebSocket] = []
_ios_lock = asyncio.Lock()  # guards ios_connections during broadcast/add/remove

# Set by cli.py at startup so endpoints can reference the actual listen port.
server_port: int = 7777

# Terminal subscriptions: maps session_id -> dict of {websocket: asyncio.Task}
_terminal_subs: dict[str, dict[WebSocket, asyncio.Task]] = {}
_terminal_subs_lock = asyncio.Lock()  # guards _terminal_subs mutations

# Callback set by cli.py to print events to the terminal via rich.
# Signature: (level, project, message) where level is one of
# "success", "warning", "error", "info".
_log_callback: Callable[[str, str, str], None] = lambda level, project, message: None


def log_event(level: str, project: str, message: str) -> None:
    """Log an event. The actual handler is set at startup via ``set_log_handler``."""
    _log_callback(level, project, message)


def set_log_handler(handler: Callable[[str, str, str], None]) -> None:
    """Replace the log event handler. Called by cli.py at startup."""
    global _log_callback
    _log_callback = handler

# TTS — enabled via CLI ``--tts`` flag.
# Variable keeps the name ``mac_tts`` to avoid cascading renames across
# routes.py, ws_handler.py, cli.py, etc.
if sys.platform == "darwin":
    mac_tts = MacTTS()
elif sys.platform == "win32":
    mac_tts = WinTTS()  # type: ignore[assignment]
else:
    mac_tts = LinuxTTS()  # type: ignore[assignment]

# STT — enabled via CLI ``--listen`` flag (macOS only).
mac_stt = MacSTT()

# Voice loop — wires TTS -> STT -> intent -> dispatch.
voice_loop: MacVoiceLoop | None = None

# Narration engine — replaces direct mac_tts.speak() calls in routes.py.
# Instantiated in bridge_server.py lifespan.
narration: "NarrationEngine | None" = None  # type: ignore[name-defined]

# Wake word mode — enabled via CLI ``--wake-word`` flag.
wake_word_enabled = False

# Listen-after-TTS — disabled via CLI ``--no-listen-after`` flag.
# When False, TTS announcements won't auto-start Mac STT.
listen_after_enabled = True

# Headless mode — set by cli.py before uvicorn.run().
# When > 0, the lifespan emits a JSON ready signal on stdout.
headless_port: int = 0

# Background tasks for polling loops
_prune_task: asyncio.Task | None = None
_activity_poll_task: asyncio.Task | None = None

# Sticky activity state: tracks the last "specific" (non-thinking) activity
# type per session so we don't flicker to thinking between tool calls.
_STICKY_ACTIVITY: dict[str, tuple[str, float]] = {}  # session_id -> (activity_type, timestamp)

# Rate limiting: track dispatch timestamps per session
_dispatch_timestamps: dict[str, list[float]] = {}  # session_id -> [timestamps]

# ---------------------------------------------------------------------------
# Broadcast & state sync helpers
# ---------------------------------------------------------------------------


def _sign_message(msg: dict) -> dict:
    """Add HMAC-SHA256 ``_sig`` field to an outgoing message.

    The iOS client verifies this signature using the same auth token.
    Format matches the iOS ``verifyHMAC`` implementation: sorted keys,
    compact JSON (no spaces), SHA-256 hex digest.
    """
    from bridge.auth import get_auth_token
    token = get_auth_token()
    if not token:
        return msg
    body = json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sig = hmac.new(token.encode(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return {**msg, "_sig": sig}


def verify_message(msg: dict) -> bool:
    """Verify the HMAC-SHA256 ``_sig`` field on an incoming message.

    Returns True if valid or if auth is disabled (no token).
    The ``_sig`` field is removed from *msg* in-place.
    """
    from bridge.auth import get_auth_token
    token = get_auth_token()
    if not token:
        return True
    sig = msg.pop("_sig", None)
    if not sig:
        log_event("warning", "bridge", "HMAC verify: no _sig field in message")
        return False
    body = json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = hmac.new(token.encode(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    valid = hmac.compare_digest(sig, expected)
    if not valid:
        # Log diagnostic info without leaking auth tokens or payload contents.
        msg_type = msg.get("type", "unknown")
        body_fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        log_event(
            "error", "bridge",
            f"HMAC mismatch: type={msg_type} sig={sig[:8]}... "
            f"expected={expected[:8]}... bodyLen={len(body)} bodyHash={body_fingerprint}",
        )
    return valid


async def send_signed(ws: WebSocket, msg: dict) -> None:
    """Sign and send a message to a single iOS WebSocket client."""
    await ws.send_json(_sign_message(msg))


def _state_sync_msg() -> dict:
    """Build a state_sync message including active_project."""
    return {
        "type": "state_sync",
        "sessions": sessions.to_dict(),
        "active_project": sessions.active_project,
        "last_announced_session_id": sessions.last_announced_session_id,
    }


async def broadcast_to_ios(message: dict) -> None:
    """Send *message* to every connected iOS WebSocket client.

    Dead connections are silently removed from the list.
    Uses a lock to prevent concurrent modification of ios_connections.
    Messages are HMAC-signed before sending.
    Each send has a 5-second timeout to prevent a slow client from
    blocking broadcasts to other clients.
    """
    signed = _sign_message(message)
    async with _ios_lock:
        dead: list[WebSocket] = []
        for ws in ios_connections:
            try:
                await asyncio.wait_for(ws.send_json(signed), timeout=5.0)
            except (WebSocketDisconnect, RuntimeError, OSError):
                dead.append(ws)
            except asyncio.TimeoutError:
                log_event("warning", "bridge", "WebSocket send timed out, dropping client")
                dead.append(ws)
            except Exception as exc:
                # Non-connection error (e.g. TypeError from JSON serialization)
                # affects the message itself, not a single client — stop trying.
                log_event("error", "bridge", f"Broadcast failed (message error): {exc}")
                break
        for ws in dead:
            try:
                ios_connections.remove(ws)
            except ValueError:
                pass


def _agent_tts_prefix(project: str, session_id: str) -> str:
    """Return TTS prefix like 'voxherd agent 2' if multiple agents exist, else just 'voxherd'."""
    same_project = sessions.get_sessions_by_project(project)
    if len(same_project) > 1:
        session = sessions.get_session(session_id)
        if session:
            return f"{project} agent {session.agent_number}"
    return project


# ---------------------------------------------------------------------------
# Terminal subscription management
# ---------------------------------------------------------------------------


async def _cancel_terminal_subs_for_session(session_id: str, project: str = "") -> None:
    """Cancel all terminal subscriptions for a pruned/removed session.

    Sends a ``terminal_subscription_ended`` message to each subscriber so
    the iOS client knows to re-subscribe with the replacement session_id.
    """
    async with _terminal_subs_lock:
        subs = _terminal_subs.pop(session_id, None)
    if not subs:
        return
    for ws, task in subs.items():
        task.cancel()
        try:
            await send_signed(ws, {
                "type": "terminal_subscription_ended",
                "session_id": session_id,
                "project": project,
            })
        except Exception:
            pass


async def _cleanup_terminal_subs(ws: WebSocket) -> None:
    """Cancel all terminal subscriptions for a disconnected WebSocket."""
    async with _terminal_subs_lock:
        for session_id in list(_terminal_subs):
            subs = _terminal_subs[session_id]
            if ws in subs:
                subs[ws].cancel()
                del subs[ws]
            if not subs:
                del _terminal_subs[session_id]
