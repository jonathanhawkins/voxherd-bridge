"""Shared module-level state for the VoxHerd bridge server.

All mutable state that's shared across routes, WebSocket handlers, and
background loops lives here. Other modules import from this module to
avoid circular dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
@dataclass
class TerminalSub:
    """A live terminal subscription: the poll task and the mode it runs in.

    ``mode`` is kept alongside the task so a re-subscribe can tell a genuine
    duplicate (no-op, keeps the in-flight snapshot) from a mode switch, which
    has to restart the loop. Clients on one session may hold different modes:
    a headset reads ``ansi`` while the phone reads ``filtered`` off the same pane.
    """

    task: asyncio.Task
    mode: str = "filtered"


@dataclass
class GeometryLease:
    """One client's claim on a tmux window's dimensions.

    Only ``ansi`` subscribers take a lease, and only one client holds it per
    session at a time -- the headset sizes the pane to its panel so the TUI
    reflows, while the phone and the lens render whatever size they are given
    and never resize anything.

    The ``prev_*`` fields are what release puts back. ``prev_window_size`` is
    the raw ``window-size`` window option, which is ``""`` when the window was
    inheriting the global value: tmux's ``resize-window`` silently sets that
    option to ``manual`` as a side effect, so restoring the dimensions without
    also restoring the option would leave the pane pinned and no longer
    tracking the terminal the user actually has open.
    """

    ws: WebSocket
    cols: int
    rows: int
    prev_cols: int
    prev_rows: int
    prev_window_size: str
    # When this lease was taken. Handover between clients is gated on age:
    # see _acquire_geometry for why liveness cannot be asked directly.
    acquired_at: float = 0.0


_terminal_subs: dict[str, dict[WebSocket, TerminalSub]] = {}
# session_id -> the single live geometry lease for that session.
_terminal_geometry: dict[str, GeometryLease] = {}

# Set by ws_handler at import time. Called when subscriptions are torn down
# from inside this module, so a lease never outlives the subscription that
# took it -- the pruned-session path matters because the pane survives a
# re-registration under a new session_id.
_geometry_release_hook: Callable[[str, WebSocket], Awaitable[None]] | None = None


def set_geometry_release_hook(
    hook: Callable[[str, WebSocket], Awaitable[None]] | None,
) -> None:
    """Register the callback used to drop geometry leases on teardown."""
    global _geometry_release_hook
    _geometry_release_hook = hook
_terminal_subs_lock = asyncio.Lock()  # guards _terminal_subs mutations

# DEBUG: counts of inbound WS messages by outcome/type (exposed via
# /api/debug/clients) — used to diagnose whether the phone's terminal_subscribe
# actually reaches the bridge and passes HMAC. Keys derive from client-supplied
# `type` strings, so bound both key length and key count — a buggy/hostile
# client must not be able to grow this dict without limit.
inbound_stats: dict[str, int] = {}
_INBOUND_STATS_MAX_KEYS = 64
_INBOUND_STATS_KEY_LEN = 48


def bump_inbound(key: str) -> None:
    key = key[:_INBOUND_STATS_KEY_LEN]
    if key not in inbound_stats and len(inbound_stats) >= _INBOUND_STATS_MAX_KEYS:
        key = "other"
    inbound_stats[key] = inbound_stats.get(key, 0) + 1

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
# OpenAI TTS can override platform TTS when ``--openai-tts`` is passed
# (set up in cli.py after arg parsing).
if sys.platform == "darwin":
    mac_tts = MacTTS()
elif sys.platform == "win32":
    mac_tts = WinTTS()  # type: ignore[assignment]
else:
    mac_tts = LinuxTTS()  # type: ignore[assignment]

# Flag set by cli.py when --openai-tts is active
openai_tts_enabled = False

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
_bonjour_task: asyncio.Task | None = None
#: The timer that re-announces Bonjour when this machine's address moves.
_bonjour_refresh_task = None

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
    """Build a state_sync message including active_project.

    ``active_choice_sessions`` / ``active_form_sessions`` carry the
    AUTHORITATIVE set of sessions that have a live numbered-choice prompt or
    multi-question form on their Mac terminal right now (the keys of the
    activity poll's decision trackers). iOS prunes any choice/form lens card
    whose session is absent from these sets, which self-heals a card stranded
    when the one-shot ``choice_prompt_cancelled`` / ``question_form_cancelled``
    broadcast was missed during a disconnect — e.g. the app was backgrounded
    (WebSocket torn down) while the user resolved the menu directly on the
    computer, then reconnected when the glasses went on and the stale options
    were still showing. This mirrors the ``pruneStalePermissions`` contract
    for the binary approve/deny card; the complementary rising-edge replay of
    ACTIVE decisions lives in ws_handler._connect_replay_decision_messages.
    """
    # Lazy import: ``activity`` imports from ``server_state``, so importing it
    # at module load would be circular. By the time this runs at request time
    # both modules are fully loaded. Fail open to empty sets so a state_sync
    # is never blocked by a tracker hiccup.
    try:
        from bridge import activity
        active_choice_sessions = list(activity._LAST_CHOICE_FOR_SESSION.keys())
        active_form_sessions = list(activity._LAST_FORM_FOR_SESSION.keys())
    except Exception:
        active_choice_sessions = []
        active_form_sessions = []
    return {
        "type": "state_sync",
        "sessions": sessions.to_dict(),
        "active_project": sessions.active_project,
        "last_announced_session_id": sessions.last_announced_session_id,
        "active_choice_sessions": active_choice_sessions,
        "active_form_sessions": active_form_sessions,
        # Carried here, not only broadcast on change: a client that was away
        # when the theme changed would otherwise come back on the wrong one
        # and stay there. state_sync is the authority; the broadcast is just
        # the fast path. Same reasoning as active_choice_sessions.
        "client_settings": dict(client_settings),
        # Same authority argument: a client that connects after the mic
        # started has missed every voice_state broadcast, and would show
        # a dead microphone while one is plainly listening.
        "voice_state": dict(voice_state),
        # Whether a headset holds the speech floor. A headset speaks only when
        # this says it does, so one too old to know stays quiet rather than
        # doubling every summary.
        "speech_floor": speech_floor_state(),
    }



# ---------------------------------------------------------------------------
# Speech floor
# ---------------------------------------------------------------------------

# Narration is spoken on the Mac and broadcast to every client, and every phone
# speaks it too. A headset speaking through Earshot takes the floor instead:
# while it holds it, the Mac and the phones stay quiet and the headset says it.
#
# The floor is a lease. The headset renews it every 20 s; unrenewed, it lapses
# and everyone else talks again. It is also released the moment its holder's
# socket closes -- but a force-stopped client can leave a half-open socket that
# never closes, so the lapse is what actually guarantees it. A floor held for a
# headset that died would be silence that looks exactly like nothing happening.
SPEECH_FLOOR_TTL_S = 60.0

_speech_floor_until: float | None = None
_speech_floor_holder: object | None = None


def claim_speech_floor(claim: bool, holder: object | None = None, now: float | None = None) -> bool:
    """Take or renew the floor for *holder*, or let it go.

    Returns True when that changed whether it is held -- the only time other
    clients need telling. A renewal changes nothing.
    """
    global _speech_floor_until, _speech_floor_holder
    import time as _time
    now = _time.monotonic() if now is None else now
    was = speech_floor_held(now)
    if claim:
        _speech_floor_until = now + SPEECH_FLOOR_TTL_S
        _speech_floor_holder = holder
    else:
        _speech_floor_until = None
        _speech_floor_holder = None
    return was != claim


def release_speech_floor_of(holder: object) -> bool:
    """Its holder went away: release at once. Returns True if it held the floor."""
    if holder is None or _speech_floor_holder is not holder or not speech_floor_held():
        return False
    return claim_speech_floor(False)


def speech_floor_held(now: float | None = None) -> bool:
    """Whether a headset holds the floor, so narration is its to speak."""
    import time as _time
    now = _time.monotonic() if now is None else now
    return _speech_floor_until is not None and now < _speech_floor_until


def speech_floor_state(now: float | None = None) -> dict:
    """The floor as ``state_sync`` carries it."""
    import time as _time
    now = _time.monotonic() if now is None else now
    held = speech_floor_held(now)
    return {
        "held": held,
        "expires_in": round(_speech_floor_until - now, 1) if held and _speech_floor_until else 0,
    }


# ---------------------------------------------------------------------------
# Microphone state
# ---------------------------------------------------------------------------

# What the microphone is doing, for clients that do not have one.
#
# The headset and the glasses have no microphone of their own -- it is the Mac's,
# often across the room -- so from the headset there is no difference between
# "nothing is listening", "listening and hearing nothing", and "heard you and
# did not recognise it". All three shipped at once and every one of them looked
# like silence, which is why they took a day to tell apart.
#
# ``heard`` deliberately carries the transcript even when it was *not* a wake
# phrase. That is the diagnostic that matters: seeing your own words come back
# proves the microphone, the permission and the recogniser all work, and moves
# the question to the phrasing.
voice_state: dict = {
    "listening": False,
    "heard": None,
    "wake": False,
    "at": None,
}


def set_voice_state(**fields) -> dict:
    """Update the mic state and return the message to broadcast."""
    import time as _time
    voice_state.update(fields)
    voice_state["at"] = _time.time()
    return {"type": "voice_state", **voice_state}


# ---------------------------------------------------------------------------
# Client-directed settings
# ---------------------------------------------------------------------------

# Settings the bridge holds on behalf of its display clients, so that a voice
# command reaches whichever glasses are listening rather than only the device
# that heard it. Values are validated against ALLOWED_CLIENT_SETTINGS before
# being stored -- these go out to every client, so an unbounded string here
# would be an unbounded string there.
_CLIENT_SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".voxherd", "client_settings.json")

_CLIENT_SETTING_DEFAULTS: dict[str, str] = {"theme": "dark", "deck": "auto"}


def _load_client_settings() -> dict[str, str]:
    """Read the stored settings, falling back to defaults on anything odd.

    **These have to survive a bridge restart.** They did not, and the failure
    was invisible in the worst way: rebuilding the macOS bundle reset the theme
    to dark, ``state_sync`` is authoritative so it pushed dark to the glasses,
    and in daylight a dark panel on an additive display is an unreadable grey
    rectangle. It looks exactly like the app has broken, and the user reported
    it as such.

    A malformed or unreadable file degrades to the defaults rather than
    raising: the bridge must start.
    """
    settings = dict(_CLIENT_SETTING_DEFAULTS)
    try:
        with open(_CLIENT_SETTINGS_FILE, encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            for name, value in stored.items():
                allowed = ALLOWED_CLIENT_SETTINGS.get(name)
                # Validate on the way *in* as well as on the way out: a file
                # edited by hand must not be able to put an arbitrary string
                # on four devices.
                if allowed and isinstance(value, str) and value in allowed:
                    settings[name] = value
    except FileNotFoundError:
        pass
    except Exception as exc:
        log_event("warning", "bridge", f"Client settings unreadable, using defaults: {exc}")
    return settings


def _save_client_settings() -> None:
    """Persist the settings. Never raises -- a failed save must not break a command."""
    try:
        os.makedirs(os.path.dirname(_CLIENT_SETTINGS_FILE), exist_ok=True)
        tmp = _CLIENT_SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(client_settings, fh)
        os.replace(tmp, _CLIENT_SETTINGS_FILE)
    except Exception as exc:
        log_event("warning", "bridge", f"Could not save client settings: {exc}")

ALLOWED_CLIENT_SETTINGS: dict[str, frozenset[str]] = {
    "theme": frozenset({"light", "dark"}),
    # Deck slot presets. "auto" hands layout back to
    # the client, which then follows the session count -- one session fills the
    # arc, a second splits it.
    "deck": frozenset({"auto", "focus", "pair", "war-room", "survey"}),
}

client_settings: dict[str, str] = _load_client_settings()


def set_client_setting(name: str, value: str) -> bool:
    """Store a client setting, returning False when it is not one we allow."""
    allowed = ALLOWED_CLIENT_SETTINGS.get(name)
    if allowed is None or value not in allowed:
        return False
    client_settings[name] = value
    _save_client_settings()
    return True


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
                await ws.close()
            except Exception:
                pass
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
    for ws, sub in subs.items():
        sub.task.cancel()
        if _geometry_release_hook is not None:
            try:
                await _geometry_release_hook(session_id, ws)
            except Exception:
                pass
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
                subs[ws].task.cancel()
                del subs[ws]
            if not subs:
                del _terminal_subs[session_id]
