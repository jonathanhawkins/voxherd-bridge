"""REST API routes for the VoxHerd bridge server.

Extracted from ``bridge_server.py`` — all REST endpoints live here,
mounted on a FastAPI ``APIRouter``.
"""

import asyncio
import json
import os
import re
import socket

from fastapi import APIRouter, Request

from bridge.server_state import (
    sessions, ios_connections, broadcast_to_ios, _state_sync_msg,
    _agent_tts_prefix, log_event, mac_tts, mac_stt, server_port,
    _STICKY_ACTIVITY, _cancel_terminal_subs_for_session,
    listen_after_enabled,
)
import bridge.server_state as _state
from bridge.validation import (
    _validate_session_id, _validate_project_name, _sanitize_summary,
    _sanitize_message, _validate_project_dir, _load_projects,
    _MAX_EVENT_PAYLOAD_LEN, _PROJECTS_PATH, _validate_tmux_pane_target,
)
from bridge.assistant import (
    is_supported_assistant,
    normalize_assistant,
)
from bridge.activity import _tmux_pane_path
from bridge import task_store
from bridge import sub_agent_monitor
from bridge import tmux_manager
from bridge.tailscale import detect_tailscale
from bridge.env_utils import get_subprocess_env

router = APIRouter()


def _is_loopback_client(request: Request) -> bool:
    """Return True if request source appears to be localhost."""
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


# ---------------------------------------------------------------------------
# Health check (unauthenticated — excluded from auth middleware)
# ---------------------------------------------------------------------------


@router.get("/health")
async def health_check() -> dict:
    """Unauthenticated health probe. Returns basic server status."""
    return {"ok": True, "service": "voxherd-bridge"}


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@router.post("/api/events")
async def receive_event(request: Request, event: dict) -> dict:
    """Receive a hook event from an assistant CLI (stop, notification, etc.)."""
    # Hooks should run on the same machine as the bridge.
    if not _is_loopback_client(request):
        return {"error": "hook endpoints are local-only"}

    # Validate payload size
    content_length = request.headers.get("content-length", "0")
    try:
        parsed_length = int(content_length or 0)
    except (TypeError, ValueError):
        return {"error": "Invalid Content-Length"}
    if parsed_length > _MAX_EVENT_PAYLOAD_LEN:
        return {"error": "Payload too large"}

    # Type-check string fields to prevent type confusion
    for field in (
        "session_id",
        "event",
        "project",
        "summary",
        "stop_reason",
        "project_dir",
        "assistant",
        "tmux_target",
    ):
        val = event.get(field)
        if val is not None and not isinstance(val, str):
            return {"error": f"field '{field}' must be a string"}

    # Boolean fields
    if event.get("skip_tts") is not None and not isinstance(event.get("skip_tts"), bool):
        return {"error": "field 'skip_tts' must be a boolean"}

    session_id = event.get("session_id", "")
    event_type = event.get("event", "")
    project = event.get("project", "unknown")
    raw_assistant = event.get("assistant")
    if raw_assistant and not is_supported_assistant(raw_assistant):
        return {"error": f"unsupported assistant '{raw_assistant}'"}
    assistant = normalize_assistant(raw_assistant, default="claude")
    tmux_target, tmux_err = _validate_tmux_pane_target(event.get("tmux_target"))
    if tmux_err:
        return {"error": tmux_err}

    # Validate session_id format
    if session_id:
        err = _validate_session_id(session_id)
        if err:
            log_event("warning", "bridge", f"Rejected event: {err}")
            return {"error": err}

    # Validate project name
    err = _validate_project_name(project)
    if err:
        log_event("warning", "bridge", f"Rejected event: {err}")
        return {"error": err}

    # Auto-register unknown sessions so hooks self-heal after bridge restarts.
    if session_id and not sessions.get_session(session_id):
        project_dir = event.get("project_dir", "")
        if project_dir:
            _session, _removed = sessions.register_session(
                session_id,
                project,
                project_dir,
                tmux_target=tmux_target,
                assistant=assistant,
            )
            log_event("success", project, f"Auto-registered session: {session_id[:12]}...")

    if event_type == "stop":
        summary = _sanitize_summary(event.get("summary", ""))
        stop_reason = event.get("stop_reason", "end_turn")
        skip_tts = event.get("skip_tts", False)
        session = sessions.get_session(session_id)
        # Map stop reason to activity type
        if stop_reason == "error":
            idle_activity = "errored"
        elif stop_reason in ("interrupted", "sigint"):
            idle_activity = "stopped"
        else:
            idle_activity = "completed"
        sessions.update_status(session_id, "idle", summary=summary, activity_type=idle_activity, stop_reason=stop_reason)
        updated_session = sessions.get_session(session_id)
        _STICKY_ACTIVITY.pop(session_id, None)  # clear sticky state on stop
        sessions.set_last_announced(project, session_id=session_id)
        log_event("success", project, f"Session stopped: {summary}")

        # Drain queued command if one was waiting for this session to go idle.
        from bridge.activity import _drain_queued_command
        if session and session.queued_command:
            asyncio.create_task(_drain_queued_command(session))

        # Only listen after TTS if there are no outstanding voice dispatches.
        # Each dispatch increments _pending_dispatch_count; each stop decrements.
        # This correctly handles rapid-fire commands (A then B): A's stop
        # decrements but still suppresses listen, B's stop decrements to 0.
        should_listen = mac_stt.enabled and listen_after_enabled
        if session and session._pending_dispatch_count > 0:
            session._pending_dispatch_count -= 1
            should_listen = False

        # Skip TTS if the project has its own Stop hook (avoids double speech)
        if skip_tts:
            log_event("info", project, "TTS skipped: project has own stop hook")
        elif _state.narration:
            # Delegate TTS to narration engine (handles batching/cooldowns)
            await _state.narration.on_stop(
                project=project, session_id=session_id, summary=summary,
                stop_reason=stop_reason,
                agent_number=updated_session.agent_number if updated_session else 1,
                listen_after=should_listen,
            )
        else:
            # Fallback: direct TTS (narration engine not initialized)
            tts_prefix = _agent_tts_prefix(project, session_id)
            mac_tts.speak(
                f"{tts_prefix} finished. {summary}",
                project=project,
                session_id=session_id,
                listen_after=should_listen,
            )

    elif event_type == "notification":
        message = event.get("message", "Needs attention.")
        # Skip "waiting for input" notifications — that's just Claude Code
        # saying it's idle, which is noise in voice mode.
        if "waiting for your input" in message.lower():
            log_event("info", project, f"Notification (suppressed): {message}")
        else:
            sessions.update_status(session_id, "waiting", activity_type="approval")
            sessions.set_last_announced(project, session_id=session_id)
            log_event("info", project, f"Notification: {message}")
            if _state.narration:
                updated = sessions.get_session(session_id)
                await _state.narration.on_notification(
                    project=project, session_id=session_id, message=message,
                    agent_number=updated.agent_number if updated else 1,
                )
            else:
                tts_prefix = _agent_tts_prefix(project, session_id)
                mac_tts.speak(
                    f"{tts_prefix} is waiting. {message}",
                    project=project,
                    session_id=session_id,
                    listen_after=mac_stt.enabled and listen_after_enabled,
                )
    elif event_type == "subagent_start":
        agent_id = event.get("agent_id", "")
        agent_type = event.get("agent_type", "unknown")
        timestamp = event.get("timestamp", "")
        session = sessions.get_session(session_id)
        if session and agent_id:
            session.add_subagent(agent_id, agent_type, timestamp)
            log_event("info", project,
                      f"Sub-agent started: {agent_type} ({agent_id[:12]}...) "
                      f"[{session.sub_agent_count} active]")
            await broadcast_to_ios({
                "type": "sub_agent_update",
                "session_id": session_id,
                "project": project,
                "sub_agent_count": session.sub_agent_count,
                "sub_agent_tasks": session.sub_agent_tasks or [],
            })
        return {"ok": True}

    elif event_type == "subagent_stop":
        agent_id = event.get("agent_id", "")
        agent_type = event.get("agent_type", "unknown")
        session = sessions.get_session(session_id)
        if session and agent_id:
            session.remove_subagent(agent_id)
            log_event("info", project,
                      f"Sub-agent stopped: {agent_type} ({agent_id[:12]}...) "
                      f"[{session.sub_agent_count} active]")
            await broadcast_to_ios({
                "type": "sub_agent_update",
                "session_id": session_id,
                "project": project,
                "sub_agent_count": session.sub_agent_count,
                "sub_agent_tasks": session.sub_agent_tasks or [],
            })
        return {"ok": True}

    else:
        log_event("info", project, f"Event: {event_type}")

    # Enrich the event with the session's current state for iOS display.
    # iOS reads "last_summary" but hooks send "summary" — include both keys.
    enriched = {"type": "agent_event", **event}
    updated_session = sessions.get_session(session_id)
    if updated_session:
        enriched["activity_type"] = updated_session.activity_type
        enriched["stop_reason"] = updated_session.stop_reason
        enriched["agent_number"] = updated_session.agent_number
        enriched["sub_agent_count"] = updated_session.sub_agent_count
        enriched["last_summary"] = updated_session.last_summary
        enriched["assistant"] = updated_session.assistant
    await broadcast_to_ios(enriched)
    return {"ok": True}


@router.post("/api/sessions/register")
async def register_session(request: Request, body: dict) -> dict:
    """Register a new assistant session (called by SessionStart hook)."""
    # SessionStart hooks should run on the same machine as the bridge.
    if not _is_loopback_client(request):
        return {"error": "hook endpoints are local-only"}

    # Type-check string fields
    for field in ("session_id", "project", "project_dir", "tmux_target", "assistant"):
        val = body.get(field)
        if val is not None and not isinstance(val, str):
            return {"error": f"field '{field}' must be a string"}

    session_id = body.get("session_id", "")
    project = body.get("project", "")
    project_dir = body.get("project_dir", "")
    raw_assistant = body.get("assistant")
    if raw_assistant and not is_supported_assistant(raw_assistant):
        return {"error": f"unsupported assistant '{raw_assistant}'"}
    assistant = normalize_assistant(raw_assistant, default="claude")

    # Validate required fields
    err = _validate_session_id(session_id)
    if err:
        return {"error": err}
    err = _validate_project_name(project)
    if err:
        return {"error": err}
    if not project_dir:
        return {"error": "project_dir is required"}
    project_dir, dir_err = _validate_project_dir(project_dir)
    if dir_err:
        return {"error": f"invalid project_dir: {dir_err}"}

    tmux_target, tmux_err = _validate_tmux_pane_target(body.get("tmux_target"))
    if tmux_err:
        return {"error": tmux_err}

    session, removed_ids = sessions.register_session(
        session_id,
        project,
        project_dir,
        tmux_target=tmux_target,
        assistant=assistant,
    )

    # Auto-switch active project to the newly registered session
    sessions.set_active_project(project)

    # Include full session data (with agent_number) so iOS creates a complete SessionInfo
    reg_msg = {"type": "session_registered", "replaces": removed_ids, "set_active_project": project}
    reg_msg.update(session.to_dict())
    reg_msg["agent_number"] = session.agent_number
    await broadcast_to_ios(reg_msg)
    log_event("success", project, f"Session registered: {session_id}")
    mac_tts.speak(f"{project} registered.", project=project, session_id=session_id)
    return {"ok": True}


@router.get("/api/sessions")
async def list_sessions() -> dict:
    """Return all sessions with their current status."""
    return sessions.to_dict()


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Remove a single session by ID."""
    session = sessions.get_session(session_id)
    if session:
        project = session.project
        sessions.remove_session(session_id)
        _STICKY_ACTIVITY.pop(session_id, None)
        await _cancel_terminal_subs_for_session(session_id)
        await broadcast_to_ios({
            "type": "session_removed",
            "session_id": session_id,
            "project": project,
        })
        await broadcast_to_ios(_state_sync_msg())
        log_event("warning", "bridge", f"Deleted session: {session_id[:12]}...")
        return {"ok": True}
    return {"error": "Session not found"}


@router.delete("/api/sessions")
async def clear_sessions() -> dict:
    """Remove all sessions."""
    all_s = sessions.get_all_sessions()
    count = len(all_s)
    for sid in list(all_s.keys()):
        await _cancel_terminal_subs_for_session(sid)
        _STICKY_ACTIVITY.pop(sid, None)
        sessions.remove_session(sid)
    await broadcast_to_ios(_state_sync_msg())
    log_event("warning", "bridge", f"Cleared {count} sessions")
    return {"ok": True, "cleared": count}


@router.get("/api/sessions/{project}/summary")
async def get_project_summary(project: str) -> dict:
    """Return the latest summary for a project (fuzzy matched)."""
    session = sessions.get_session_by_project(project)
    if session is None:
        return {"error": "Project not found"}
    return {"project": session.project, "summary": session.last_summary}


@router.get("/api/sessions/{project}/subagents")
async def get_project_subagents(project: str) -> dict:
    """Return sub-agent info for a project's sessions."""
    matched = sessions.get_sessions_by_project(project)
    if not matched:
        return {"error": "Project not found"}

    result: list[dict] = []
    for s in matched:
        count, tasks = await asyncio.to_thread(
            sub_agent_monitor.get_sub_agent_info, s.session_id, s.project
        )
        result.append({
            "session_id": s.session_id,
            "project": s.project,
            "agent_number": s.agent_number,
            "sub_agent_count": count,
            "sub_agent_tasks": tasks,
        })
    return {"sessions": result}


@router.get("/api/ports")
async def scan_ports() -> dict:
    """Scan common dev server ports and return which ones are listening."""
    common_ports = [3000, 3001, 4000, 4200, 5000, 5173, 5174, 8000, 8080, 8888, 9000]
    active: list[dict] = []

    async def check_port(port: int) -> dict | None:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.3
            )
            writer.close()
            await writer.wait_closed()
            return {"port": port, "url": f"http://localhost:{port}"}
        except (OSError, asyncio.TimeoutError):
            return None

    results = await asyncio.gather(*[check_port(p) for p in common_ports])
    active = [r for r in results if r is not None]
    return {"ports": active}


@router.get("/api/connection-info")
async def connection_info() -> dict:
    """Return local and Tailscale connection info for iOS auto-discovery."""
    local_hostname = socket.gethostname()
    local_url = f"ws://{local_hostname}.local:{server_port}/ws/ios"
    ts = await asyncio.to_thread(detect_tailscale, server_port)
    return {
        "local": local_url,
        "tailscale": {"ip": ts["ip"], "hostname": ts["hostname"]} if ts else None,
    }


@router.get("/api/projects")
async def list_projects() -> dict:
    """Return available projects that can be spawned, merging configured + discovered."""
    configured = _load_projects()
    configured_names = {p["name"].lower() for p in configured}
    result = [dict(p, source="configured") for p in configured]

    # Auto-discover from tmux sessions
    for ts in await tmux_manager.async_list_sessions():
        name = ts["name"]
        if name.lower() in configured_names:
            continue
        if name in (tmux_manager.BRIDGE_SESSION, "bridge"):
            continue
        pane_dir = await _tmux_pane_path(name)
        if pane_dir:
            result.append({"name": name, "dir": pane_dir, "source": "discovered"})

    return {"projects": result}


@router.get("/api/tmux/sessions")
async def list_tmux_sessions() -> dict:
    """Unified view of all tmux sessions cross-referenced with registered sessions and projects."""
    from bridge.session_manager import Session

    all_tmux = await tmux_manager.async_list_sessions()
    configured = _load_projects()
    configured_by_name = {p["name"].lower(): p for p in configured}
    registered = sessions.get_all_sessions()

    # Build lookup: tmux session name -> list of registered Sessions
    # tmux_target is like "voxherd:0.0" -> tmux session "voxherd"
    registered_by_tmux: dict[str, list[Session]] = {}
    for s in registered.values():
        if s.tmux_target:
            tmux_name = s.tmux_target.split(":")[0]
            registered_by_tmux.setdefault(tmux_name, []).append(s)

    projects: list[dict] = []
    seen_names: set[str] = set()

    for ts in all_tmux:
        name = ts["name"]
        if name in (tmux_manager.BRIDGE_SESSION, "bridge"):
            continue

        seen_names.add(name.lower())
        has_live = await tmux_manager.async_session_has_live_process(name)
        reg_sessions = registered_by_tmux.get(name, [])
        cfg = configured_by_name.get(name.lower())

        if reg_sessions:
            # Emit one entry per registered session (multi-agent support)
            for reg_session in reg_sessions:
                entry: dict = {
                    "name": reg_session.project,
                    "dir": reg_session.project_dir or None,
                    "tmux_session": name,
                    "has_live_process": has_live,
                    "is_registered": True,
                    "registered_session_id": reg_session.session_id,
                    "assistant": reg_session.assistant,
                    "status": reg_session.status,
                    "activity_type": reg_session.activity_type,
                    "last_summary": reg_session.last_summary,
                    "is_configured": cfg is not None,
                    "agent_number": reg_session.agent_number,
                }
                projects.append(entry)
        else:
            pane_dir = ""
            if cfg:
                pane_dir = cfg.get("dir", "")
            else:
                pane_dir = await _tmux_pane_path(name)

            entry = {
                "name": name,
                "dir": pane_dir or None,
                "tmux_session": name,
                "has_live_process": has_live,
                "is_registered": False,
                "registered_session_id": None,
                "assistant": None,
                "status": None,
                "activity_type": None,
                "last_summary": None,
                "is_configured": cfg is not None,
                "agent_number": None,
            }
            projects.append(entry)

    # Append configured projects that have NO tmux session (available to start)
    for cfg in configured:
        if cfg["name"].lower() not in seen_names:
            projects.append({
                "name": cfg["name"],
                "dir": cfg.get("dir"),
                "tmux_session": None,
                "has_live_process": False,
                "is_registered": False,
                "registered_session_id": None,
                "assistant": None,
                "status": None,
                "activity_type": None,
                "last_summary": None,
                "is_configured": True,
            })

    running = sum(1 for p in projects if p["has_live_process"])
    reg_count = sum(1 for p in projects if p["is_registered"])
    cfg_count = sum(1 for p in projects if p["is_configured"])

    return {
        "projects": projects,
        "stats": {
            "total": len(projects),
            "running": running,
            "registered": reg_count,
            "configured": cfg_count,
        },
    }


@router.post("/api/projects/add")
async def add_project(body: dict) -> dict:
    """Add a discovered project to projects.json."""
    name = body.get("name", "").strip()
    raw_dir = body.get("dir", "").strip()
    if not name or not raw_dir:
        return {"error": "name and dir are required"}

    # Validate and canonicalize the directory path
    project_dir, dir_err = _validate_project_dir(raw_dir)
    if dir_err:
        return {"error": f"Invalid project directory: {dir_err}"}

    existing = _load_projects()
    for p in existing:
        if p["name"].lower() == name.lower():
            return {"error": f"Project '{name}' already exists"}

    existing.append({"name": name, "dir": project_dir})
    os.makedirs(os.path.dirname(_PROJECTS_PATH), exist_ok=True)
    with open(_PROJECTS_PATH, "w") as f:
        json.dump(existing, f, indent=2)
    return {"ok": True}


@router.post("/api/tts")
async def tts_speak(body: dict) -> dict:
    """Speak text through the bridge TTS queue."""
    text = body.get("text", "")
    if not text:
        return {"error": "text is required"}
    project = body.get("project", "tts")
    listen_after = body.get("listen_after", mac_stt.enabled and listen_after_enabled)
    log_event("info", project, f"TTS: {text}")
    mac_tts.speak(text, project=project, listen_after=listen_after)
    return {"ok": True}


@router.post("/api/intent/parse")
async def parse_intent(body: dict) -> dict:
    """Parse a voice transcription into a structured intent using Haiku.

    Called by the iOS app when regex-based parsing fails.
    """
    transcription = body.get("transcription", "")
    known_projects = body.get("known_projects", [])
    active_project = body.get("active_project", "")

    if not transcription:
        return {"error": "No transcription provided"}

    project_list = ", ".join(known_projects) if known_projects else "none"
    active_note = f" The currently active project is '{active_project}'." if active_project else ""

    prompt = (
        f"You are a voice command parser for a multi-project development tool. "
        f"Known projects: {project_list}.{active_note}\n\n"
        f"Parse this voice command into a JSON action:\n"
        f"Transcription: \"{transcription}\"\n\n"
        f"Respond with ONLY a JSON object. Valid actions:\n"
        f'- {{"action": "status"}}\n'
        f'- {{"action": "tell", "project": "name", "message": "command"}}\n'
        f'- {{"action": "switch", "project": "name"}}\n'
        f'- {{"action": "approve", "project": "name"}}\n'
        f'- {{"action": "deny", "project": "name"}}\n'
        f'- {{"action": "what_did", "project": "name"}}\n'
        f'- {{"action": "pause", "project": "name"}}\n'
        f'- {{"action": "resume", "project": "name"}}\n'
        f"Match project names fuzzily to the known projects list."
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--model", "claude-haiku-4-5-20251001",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=get_subprocess_env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        result = json.loads(stdout.decode().strip())
        log_event("info", "bridge", f"Intent parsed: {result.get('action', 'unknown')}")
        return result
    except asyncio.TimeoutError:
        log_event("error", "bridge", "Intent parsing timed out")
        return {"error": "Intent parsing timed out"}
    except Exception as exc:
        log_event("error", "bridge", f"Intent parsing failed: {exc}")
        return {"error": "Intent parsing failed"}


@router.post("/api/command")
async def rest_command(body: dict) -> dict:
    """Dispatch a command via REST (for Siri Shortcuts, not WebSocket)."""
    from bridge.ws_handler import _dispatch_agent

    project = body.get("project", "")
    message = body.get("message", "")
    session_id = body.get("session_id")
    agent_number = body.get("agent_number")
    raw_assistant = body.get("assistant")

    if not project or not message:
        return {"error": "project and message are required"}
    if session_id is not None and not isinstance(session_id, str):
        return {"error": "field 'session_id' must be a string"}
    if raw_assistant is not None and not isinstance(raw_assistant, str):
        return {"error": "field 'assistant' must be a string"}
    if raw_assistant and not is_supported_assistant(raw_assistant):
        return {"error": f"unsupported assistant '{raw_assistant}'"}
    assistant = normalize_assistant(raw_assistant, default="claude") if raw_assistant else None
    if agent_number is not None:
        try:
            agent_number = int(agent_number)
        except (TypeError, ValueError):
            return {"error": "field 'agent_number' must be an integer"}

    if session_id:
        session = sessions.get_session(session_id)
    elif agent_number is not None:
        session = sessions.get_session_by_project_and_number(
            project, agent_number, assistant=assistant
        )
    else:
        session = sessions.get_session_by_project(project, assistant=assistant)
    if session is None:
        suffix = f" (assistant={assistant})" if assistant else ""
        return {"error": f"No session found for project '{project}'{suffix}"}

    sessions.update_status(session.session_id, "active", activity_type="working")
    asyncio.create_task(_dispatch_agent(session.session_id, session.project_dir, message))

    await broadcast_to_ios({
        "type": "command_accepted",
        "session_id": session.session_id,
        "project": session.project,
        "assistant": session.assistant,
        "message": message,
        "activity_type": "working",
    })
    log_event("warning", session.project, f"REST command dispatched: {message}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Task endpoints
# ---------------------------------------------------------------------------


@router.get("/api/tasks/{project}")
async def list_tasks(project: str) -> dict:
    """Return all tasks for a project's task list."""
    list_id = task_store.resolve_task_list_id(project)
    if list_id is None:
        return {"tasks": [], "project": project}
    tasks = task_store.list_tasks(list_id)
    return {"tasks": tasks, "project": project}


@router.post("/api/tasks/{project}")
async def create_task(project: str, body: dict) -> dict:
    """Create a new task in a project's task list."""
    subject = body.get("subject", "")
    if not subject:
        return {"error": "subject is required"}
    description = body.get("description", "")
    active_form = body.get("activeForm", "")
    list_id = task_store.resolve_task_list_id(project) or project
    task = task_store.create_task(list_id, subject, description, active_form)
    log_event("success", project, f"Task created: {subject}")
    return {"ok": True, "task": task}


@router.patch("/api/tasks/{project}/{task_id}")
async def update_task(project: str, task_id: str, body: dict) -> dict:
    """Update a task's status or other fields."""
    list_id = task_store.resolve_task_list_id(project)
    if list_id is None:
        return {"error": f"No task list for project '{project}'"}
    task = task_store.update_task(list_id, task_id, **body)
    if task is None:
        return {"error": f"Task '{task_id}' not found"}
    log_event("info", project, f"Task {task_id} updated: {body}")
    return {"ok": True, "task": task}
