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

from fastapi import WebSocket, WebSocketDisconnect

from bridge.server_state import (
    sessions, ios_connections, _ios_lock, broadcast_to_ios, _state_sync_msg,
    send_signed, verify_message, log_event, _terminal_subs, _terminal_subs_lock,
    _cancel_terminal_subs_for_session, _cleanup_terminal_subs,
    _STICKY_ACTIVITY, _dispatch_timestamps,
)
from bridge.validation import (
    _sanitize_message, _validate_project_dir, _load_projects,
    _MAX_EVENT_PAYLOAD_LEN, _MAX_WS_CONNECTIONS, _is_safe_tmux_target,
    _DISPATCH_RATE_LIMIT, _DISPATCH_RATE_WINDOW, _PROJECTS_PATH,
    _validate_tmux_pane_target,
)
from bridge.auth import get_auth_token
from bridge import task_store
from bridge import tmux_manager
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
    "set_verbosity",
})


_ALLOWED_ORIGINS = frozenset({
    # Native apps (iOS URLSessionWebSocketTask) don't send Origin headers.
    # Browsers always send one, so we reject any browser-based origin that
    # isn't localhost.  This blocks malicious websites from connecting.
    None, "", "null",
})


def _is_allowed_origin(origin: str | None) -> bool:
    """Return True if the WebSocket Origin header is acceptable."""
    if origin in _ALLOWED_ORIGINS:
        return True
    # Allow localhost origins (e.g. wscat, test clients)
    if origin and (
        origin.startswith("http://localhost")
        or origin.startswith("http://127.0.0.1")
        or origin.startswith("https://localhost")
        or origin.startswith("https://127.0.0.1")
    ):
        return True
    return False


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
        msgs.append({
            "type": "choice_prompt",
            "session_id": sid,
            "project": session.project,
            "title": choice.title,
            "options": choice.options,
            "signature": choice.signature,
            "input_mode": choice.input_mode,
            "body": choice.body,
        })
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
        if not token or not secrets.compare_digest(token, auth_token):
            await websocket.close(code=4001, reason="Unauthorized")
            log_event("error", "bridge", "WebSocket connection rejected: unauthorized")
            return

    # Connection limit
    async with _ios_lock:
        if len(ios_connections) >= _MAX_WS_CONNECTIONS:
            await websocket.accept()
            await websocket.close(code=4002, reason="Too many connections")
            log_event("warning", "bridge", "WebSocket rejected: connection limit reached")
            return

    await websocket.accept()
    async with _ios_lock:
        ios_connections.append(websocket)

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

    try:
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
    except WebSocketDisconnect:
        pass
    finally:
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


async def _send_tasks_sync(project: str, ws: WebSocket) -> None:
    """Send a tasks_sync message for a project to a single WebSocket."""
    list_id = task_store.resolve_task_list_id(project)
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
    project = data.get("project", "")
    if not project:
        return
    await _send_tasks_sync(project, ws)


async def _handle_task_create(data: dict, ws: WebSocket) -> None:
    """Create a task and respond with updated task list."""
    project = data.get("project", "")
    subject = data.get("subject", "")
    if not project or not subject:
        return
    description = data.get("description", "")
    active_form = data.get("activeForm", "")
    list_id = task_store.resolve_task_list_id(project) or project
    task_store.create_task(list_id, subject, description, active_form)
    log_event("success", project, f"Task created (WS): {subject}")
    await _send_tasks_sync(project, ws)


async def _handle_task_update(data: dict, ws: WebSocket) -> None:
    """Update a task and respond with updated task list."""
    project = data.get("project", "")
    task_id = data.get("task_id", "")
    if not project or not task_id:
        return
    updates = {}
    for field in ("status", "subject", "description", "activeForm"):
        if field in data:
            updates[field] = data[field]
    if not updates:
        return
    list_id = task_store.resolve_task_list_id(project)
    if list_id:
        task_store.update_task(list_id, task_id, **updates)
        log_event("info", project, f"Task {task_id} updated (WS): {updates}")
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
    r"^[+*✳✴✶✷✸✻✽✱·∙⠇-⣿]+\s+\S.*\([^)]*\bthinking\b[^)]*\)",
    re.IGNORECASE,
)


def _is_task_summary_row(line: str) -> bool:
    """True when *line* is a row of Claude Code's TodoWrite widget.

    Matches all four components of the widget (header, checkbox body
    with optional tree-corner prefix, truncation tail, and the folded
    thinking-block parent line) so a single global filter removes the
    whole block regardless of where it sits in the captured pane.
    """
    plain = _strip_ansi(line)
    stripped = plain.strip()
    if not stripped:
        return False
    if _TASK_SUMMARY_HEADER_RE.match(stripped):
        return True
    if _TASK_SUMMARY_TAIL_RE.match(stripped):
        return True
    if _THINKING_FOLD_RE.match(stripped):
        return True
    if stripped[0] in _TASK_CHECKBOX_CHARS:
        return True
    # Strip leading box-drawing tree connectors (e.g. `└ `, `├─ `) and
    # try again — Claude Code uses these to attach the first widget row
    # to a folded parent block.
    inner = stripped.lstrip("".join(_TASK_TREE_PREFIX_CHARS))
    if inner and inner[0] in _TASK_CHECKBOX_CHARS:
        return True
    return False


def _strip_task_summary(lines: list[str]) -> list[str]:
    """Drop Claude Code's TodoWrite task-summary widget rows.

    Per-line filter — no anchoring or contiguity requirement. The
    header and tail patterns are specific enough that false-positives
    on real assistant content are vanishingly unlikely; the checkbox
    prefix is a small risk if a user pastes a Markdown checklist, but
    on the lens the user is reading streaming assistant output, not
    composing checklists, so the trade is correct.
    """
    return [ln for ln in lines if not _is_task_summary_row(ln)]


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
    r"[^)]*\("                                            # … up to an opening paren
    r"[^)]*\b(?:\d+\s*[ms]\b|esc\s+to\s+interrupt\b)",    # elapsed or interrupt inside the parens
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


async def _terminal_poll_loop(session_id: str, ws: WebSocket) -> None:
    """Poll tmux capture-pane every ~1s and push content when it changes."""
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

            lines = art_rows + body

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
    _target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
    if tmux_err:
        try:
            await send_signed(ws,{
                "type": "terminal_error",
                "session_id": session_id,
                "message": f"Invalid tmux target for session '{session_id}'",
            })
        except Exception:
            pass
        return

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
        if existing is not None and not existing.done():
            log_event("info", session.project,
                      f"Terminal subscribe ignored (already active): {session_id[:12]}...")
            return
        # No live subscription (or the previous one finished/errored).
        # Cancel any tombstone task and start fresh.
        if existing is not None:
            existing.cancel()

        task = asyncio.create_task(_terminal_poll_loop(session_id, ws))
        _terminal_subs.setdefault(session_id, {})[ws] = task
    log_event("info", session.project, f"Terminal subscribed: {session_id[:12]}...")


async def _handle_terminal_unsubscribe(data: dict, ws: WebSocket) -> None:
    """Stop streaming terminal content for a session."""
    session_id = data.get("session_id", "")
    async with _terminal_subs_lock:
        subs = _terminal_subs.get(session_id)
        if subs and ws in subs:
            subs[ws].cancel()
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
    if not keys:
        return

    # Cap key input length to prevent abuse
    _MAX_KEYS_LEN = 10_000
    if len(keys) > _MAX_KEYS_LEN:
        keys = keys[:_MAX_KEYS_LEN]

    # Optional choice-commit gate for older iOS clients that still
    # build the keystroke client-side. New clients use Branch A above.
    expected_choice_sig = data.get("choice_signature")
    if expected_choice_sig:
        from bridge.activity import _LAST_CHOICE_FOR_SESSION
        live = _LAST_CHOICE_FOR_SESSION.get(session_id)
        if live is None or live.signature != expected_choice_sig:
            try:
                await send_signed(ws, {
                    "type": "error",
                    "session_id": session_id,
                    "message": "Choice prompt no longer live — commit ignored.",
                })
            except Exception:
                pass
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

    literal = data.get("literal", False)

    try:
        cmd = ["tmux", "send-keys", "-t", tmux_target]
        if literal:
            cmd.append("-l")
        cmd.append(keys)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await proc.wait()
        log_event("info", session.project, f"Sent keys: {keys}{' (literal)' if literal else ''}")
    except Exception as exc:
        log_event("error", session.project, f"Failed to send keys: {exc}")


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
                "tmux", "send-keys", "-t", tmux_target, "-l", keys,
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


async def _capture_and_detect_choice(tmux_target: str):
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
        note_text = _sanitize_message(str(data.get("text", ""))).strip()[:500]
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
            "tmux", "send-keys", "-t", tmux_target, *nav_keys,
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
    prompt = _sanitize_message(data.get("prompt", ""))
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
            "tmux", "send-keys", "-t", tmux_session, "-l", prompt,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await p1.wait()
        p2 = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", tmux_session, "Enter",
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
            "tmux", "send-keys", "-t", tmux_session, "C-c",
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
            _STICKY_ACTIVITY.pop(sid, None)
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

    # Rate limit check
    if not _check_dispatch_rate(session.session_id):
        await send_signed(websocket,{
            "type": "error",
            "message": f"Rate limit exceeded for '{target_project}' — try again shortly",
        })
        log_event("warning", target_project, "Command dispatch rate-limited")
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


async def _dispatch_agent(session_id: str, project_dir: str, message: str) -> None:
    """Send a command to a session's configured assistant.

    If the session has a ``tmux_target``, types the message directly into
    the interactive terminal via ``tmux send-keys``. Otherwise falls back
    to assistant-specific resume commands when available.
    """
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
                "tmux", "send-keys", "-t", tmux_target, "-l", message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=get_subprocess_env(),
            )
            _, err1 = await asyncio.wait_for(p1.communicate(), timeout=5.0)
            if p1.returncode != 0:
                await _handle_dead_pane(session_id, tmux_target, err1)
                return
            p2 = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", tmux_target, "Enter",
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
