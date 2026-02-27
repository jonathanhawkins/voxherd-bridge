"""WebSocket endpoint and all related handlers for the VoxHerd bridge.

Extracted from bridge_server.py. The main ``ios_websocket`` function is
registered by bridge_server via ``app.websocket("/ws/ios")(ios_websocket)``.
"""

import asyncio
import json
import re
import secrets
import time

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
from bridge.assistant import (
    default_assistant,
    is_supported_assistant,
    normalize_assistant,
    resume_command_for_assistant,
    spawn_command_for_assistant,
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
    log_event("warning", "bridge", "iOS client connected")

    try:
        while True:
            raw = await websocket.receive_text()
            # Reject oversized messages
            if len(raw) > _MAX_EVENT_PAYLOAD_LEN:
                log_event("warning", "bridge", f"Oversized WS message dropped ({len(raw)} bytes)")
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                log_event("warning", "bridge", "Malformed JSON on WebSocket")
                continue

            # Verify HMAC signature when auth is enabled
            if not verify_message(dict(data)):
                log_event("warning", "bridge", "HMAC verification failed on incoming WS message")
                continue

            msg_type = data.get("type", "")

            # Drop unknown message types
            if msg_type not in _KNOWN_WS_TYPES:
                log_event("warning", "bridge", f"Unknown WS message type: {msg_type!r}")
                continue

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


async def _terminal_poll_loop(session_id: str, ws: WebSocket) -> None:
    """Poll tmux capture-pane every ~1s and push content when it changes."""
    session = sessions.get_session(session_id)
    if not session or not session.tmux_target:
        return
    tmux_target, tmux_err = _validate_tmux_pane_target(session.tmux_target)
    if tmux_err or not tmux_target:
        return

    last_snapshot: list[str] = []
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
                "-p", "-e", "-S", "-100",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=get_subprocess_env(),
            )
            stdout, _ = await proc.communicate()
            raw = stdout.decode("utf-8", errors="replace")
            lines = raw.splitlines()

            if lines != last_snapshot:
                last_snapshot = lines
                try:
                    await send_signed(ws,{
                        "type": "terminal_content",
                        "session_id": session_id,
                        "lines": lines,
                    })
                except Exception:
                    break

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass


async def _handle_terminal_subscribe(data: dict, ws: WebSocket) -> None:
    """Start polling tmux for a session and streaming to the subscriber."""
    session_id = data.get("session_id", "")
    session = sessions.get_session(session_id)
    if not session or not session.tmux_target:
        try:
            await send_signed(ws,{
                "type": "error",
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
                "type": "error",
                "message": f"Invalid tmux target for session '{session_id}'",
            })
        except Exception:
            pass
        return

    # Cancel any existing subscription for this ws + session combo.
    async with _terminal_subs_lock:
        if session_id in _terminal_subs and ws in _terminal_subs[session_id]:
            _terminal_subs[session_id][ws].cancel()

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

    When ``literal`` is true, ``tmux send-keys -l`` is used so the text is
    sent character-by-character without interpreting key names (e.g. "Up" is
    sent as the letters U-p, not the Up arrow).  When false (default), the
    value is treated as a tmux key name ("Down" sends the Down arrow escape
    sequence, "Enter" sends Return, etc.).
    """
    session_id = data.get("session_id", "")
    keys = data.get("keys", "")
    if not keys:
        return

    # Cap key input length to prevent abuse
    _MAX_KEYS_LEN = 10_000
    if len(keys) > _MAX_KEYS_LEN:
        keys = keys[:_MAX_KEYS_LEN]

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


# ---------------------------------------------------------------------------
# Session spawning
# ---------------------------------------------------------------------------


async def _handle_spawn_session(data: dict, ws: WebSocket) -> None:
    """Spawn a new assistant session in a tmux window."""
    project_name = data.get("project", "")
    prompt = _sanitize_message(data.get("prompt", ""))
    raw_assistant = data.get("assistant")

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
        env = get_subprocess_env()
        if assistant == "claude":
            # Set env to share task list across all Claude agents.
            env["CLAUDE_CODE_TASK_LIST_ID"] = "voxherd"

        spawn_cmd = spawn_command_for_assistant(assistant)

        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-session", "-d", "-s", tmux_session,
            "-c", project_dir, "--", *spawn_cmd,
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
            await send_signed(websocket,{
                "type": "command_queued",
                "session_id": session.session_id,
                "project": session.project,
                "assistant": session.assistant,
                "message": message,
            })
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
    })
    log_event("warning", project, f"Command dispatched: {message}")


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
            p1 = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", tmux_target, "-l", message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=get_subprocess_env(),
            )
            await p1.wait()
            p2 = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", tmux_target, "Enter",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=get_subprocess_env(),
            )
            await p2.wait()
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

            env = get_subprocess_env()
            if assistant == "claude":
                env["CLAUDE_CODE_TASK_LIST_ID"] = "voxherd"

            await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *resume_cmd,
                    cwd=project_dir,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            log_event("error", "bridge", f"Timed out spawning {assistant} subprocess")
        except Exception as exc:
            log_event("error", "bridge", f"Failed to spawn {assistant}: {exc}")


# Backward-compat import path for existing tests/callers.
_dispatch_claude = _dispatch_agent
