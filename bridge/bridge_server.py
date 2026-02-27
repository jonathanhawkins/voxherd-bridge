"""FastAPI bridge server for VoxHerd.

This is the assembly module that creates the FastAPI app, wires up the
lifespan, includes route modules, and registers the WebSocket handler.

The actual endpoint implementations live in:
- bridge.routes       — REST endpoints (APIRouter)
- bridge.ws_handler   — WebSocket endpoint and handlers
- bridge.activity     — Activity detection and polling loops
- bridge.auth         — Auth token management and middleware
- bridge.validation   — Input validation helpers
- bridge.server_state — Shared state (sessions, connections, TTS/STT)
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Re-export shared state so existing code (cli.py, tests) can still do
# ``from bridge.bridge_server import sessions, mac_tts, ...``
# ---------------------------------------------------------------------------

from bridge.server_state import (  # noqa: F401 — re-exports
    sessions,
    ios_connections,
    _ios_lock,
    server_port,
    _terminal_subs,
    _terminal_subs_lock,
    log_event,
    mac_tts,
    mac_stt,
    voice_loop,
    wake_word_enabled,
    listen_after_enabled,
    broadcast_to_ios,
    send_signed,
    _sign_message,
    _state_sync_msg,
    _agent_tts_prefix,
    _cancel_terminal_subs_for_session,
    _cleanup_terminal_subs,
    _STICKY_ACTIVITY,
    _dispatch_timestamps,
    _prune_task,
    _activity_poll_task,
)
import bridge.server_state as _state

from bridge.auth import (  # noqa: F401 — re-exports
    _AUTH_TOKEN_FILE,
    _AUTH_TOKEN,
    _load_auth_token,
    persist_auth_token,
    ensure_auth_token,
    get_auth_token,
    _check_auth,
    auth_middleware,
)
import bridge.auth as _auth

from bridge.validation import (  # noqa: F401 — re-exports
    _validate_session_id,
    _validate_project_name,
    _sanitize_summary,
    _sanitize_message,
    _validate_project_dir,
    _load_projects,
    _is_safe_tmux_target,
    _ANSI_RE,
    _MAX_EVENT_PAYLOAD_LEN,
    _MAX_WS_CONNECTIONS,
    _PROJECTS_PATH,
)

from bridge.activity import (  # noqa: F401 — re-exports
    _activity_poll_loop,
    _periodic_prune,
    _discover_tmux_sessions,
    _detect_activity_type,
    _resolve_activity_type,
    _is_status_bar_line,
    _extract_snippet,
    _is_claude_code_process,
    _pane_fg_command,
    _tmux_pane_path,
    _SHELL_COMMANDS,
)

from bridge.routes import router as _api_router

from bridge.ws_handler import (  # noqa: F401 — re-exports
    ios_websocket,
    handle_voice_command,
    _dispatch_agent,
)

from bridge import bonjour
from bridge import tmux_manager
from bridge.assistant import looks_like_assistant_process


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown logic for the bridge server."""
    # --- Startup ---
    token = ensure_auth_token()
    _state.log_event("info", "bridge", f"Auth token loaded ({_AUTH_TOKEN_FILE})")

    _state.mac_tts.start()

    # Instantiate narration engine (batches/coalesces TTS announcements)
    from bridge.narration import NarrationEngine
    _state.narration = NarrationEngine(
        tts=_state.mac_tts,
        broadcast_fn=broadcast_to_ios,
        log_fn=_state.log_event,
        sessions=sessions,
        stt=_state.mac_stt,
        listen_after_enabled=_state.listen_after_enabled,
    )

    _init_voice_loop()

    # Clean up stale VoxHerd tmux sessions (vh-* with dead processes)
    try:
        from bridge.tmux_manager import cleanup_stale_sessions
        cleaned = cleanup_stale_sessions()
        if cleaned:
            _state.log_event("warning", "bridge", f"Startup: cleaned {len(cleaned)} stale tmux session(s)")
    except Exception as e:
        _state.log_event("warning", "bridge", f"Tmux cleanup skipped: {e}")

    # Prune dead entries from the session registry
    pruned = await _state.sessions.prune_dead()
    if pruned:
        _state.log_event("warning", "bridge", f"Startup: pruned {len(pruned)} dead session(s)")
        for sid, project in pruned:
            await _state._cancel_terminal_subs_for_session(sid, project=project)

    # Validate "active" sessions — check if the expected assistant is running in the pane.
    for session in list(_state.sessions.get_all_sessions().values()):
        if not session.tmux_target:
            continue
        try:
            fg_cmd = await _pane_fg_command(session.tmux_target)
            if fg_cmd in _SHELL_COMMANDS or not fg_cmd:
                _state.sessions.update_status(
                    session.session_id, "idle",
                    activity_type="sleeping",
                    stop_reason="bridge_restart",
                )
                _state.log_event("info", session.project, "Startup: marked idle (shell prompt in pane)")
            elif not looks_like_assistant_process(session.assistant, fg_cmd):
                _state.sessions.remove_session(session.session_id)
                _state.log_event(
                    "info",
                    session.project,
                    f"Startup: removed (pane running '{fg_cmd}', not {session.assistant})",
                )
        except Exception:
            pass

    # Discover assistant CLI instances in tmux that started before the bridge.
    discovered = await _discover_tmux_sessions()
    if discovered:
        _state.log_event("success", "bridge", f"Startup: discovered {discovered} running session(s) in tmux")

    _state._prune_task = asyncio.create_task(_periodic_prune())
    _state._activity_poll_task = asyncio.create_task(_activity_poll_loop())
    try:
        bonjour.register(port=_state.server_port, auth_token=token or "")
        _state.log_event("info", "bridge", "Bonjour: advertising _voxherd._tcp")
    except Exception as e:
        _state.log_event("warning", "bridge", f"Bonjour registration failed: {e}")

    # Headless ready signal — must come from lifespan because FastAPI ignores
    # @app.on_event("startup") when a lifespan context manager is set.
    if _state.headless_port > 0:
        import json as _json, sys as _sys
        ready_line = _json.dumps({"status": "ready", "port": _state.headless_port})
        _sys.stdout.write(ready_line + "\n")
        _sys.stdout.flush()

    yield

    # --- Shutdown ---
    _state.log_event("info", "bridge", "Shutting down bridge server...")
    if _state._prune_task:
        _state._prune_task.cancel()
    if _state._activity_poll_task:
        _state._activity_poll_task.cancel()
    _state.mac_tts.stop()
    try:
        bonjour.unregister()
    except Exception as e:
        _state.log_event("warning", "bridge", f"Bonjour unregister failed: {e}")
    _state.log_event("info", "bridge", "Bridge server stopped.")


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=_lifespan)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Adds a short request ID to each HTTP request for log correlation."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex[:8]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# Request ID middleware (before auth so IDs are available in auth logs)
app.add_middleware(RequestIDMiddleware)

# Auth middleware
app.middleware("http")(auth_middleware)

# REST routes
app.include_router(_api_router)

# WebSocket endpoint
app.websocket("/ws/ios")(ios_websocket)


# ---------------------------------------------------------------------------
# Voice loop initialization
# ---------------------------------------------------------------------------


def _init_voice_loop() -> None:
    """Create and wire the Mac voice loop if STT is enabled."""
    if _state.mac_stt.enabled:
        from bridge.mac_voice_loop import MacVoiceLoop
        _state.voice_loop = MacVoiceLoop(
            tts=_state.mac_tts,
            stt=_state.mac_stt,
            sessions=_state.sessions,
            dispatch=_dispatch_agent,
            log=_state.log_event,
        )
        _state.voice_loop.wire()
        if _state.wake_word_enabled:
            _state.voice_loop.start_wake_word()
