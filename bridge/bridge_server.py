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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Re-export shared state so existing code (cli.py, tests) can still do
# ``from bridge.bridge_server import sessions, mac_tts, ...``
# ---------------------------------------------------------------------------

from bridge.server_state import (  # noqa: F401 — re-exports
    sessions,
    ios_connections,
    _ios_lock,
    _terminal_subs,
    _terminal_subs_lock,
    log_event,
    mac_tts,
    mac_stt,
    voice_loop,
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
    restore_stranded_panes,
    _dispatch_agent,
)

from bridge import bonjour
from bridge import tmux_manager
from bridge.assistant import looks_like_assistant_process


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


#: How often to check whether this machine's addresses have moved. Half a
#: minute: a network change should not strand a headset for long, and the check
#: is one `getaddrinfo` that usually finds nothing changed.
_BONJOUR_REFRESH_SECONDS = 30.0


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown logic for the bridge server."""
    # --- Startup ---
    token = ensure_auth_token()
    _state.log_event("info", "bridge", f"Auth token loaded ({_AUTH_TOKEN_FILE})")

    # Resolve the names this machine answers to, once, here rather than on the
    # first request: auth_middleware pins the Host header against this set, and
    # resolving it shells out to scutil and may consult Tailscale -- neither
    # belongs on the event loop while a request waits.
    #
    # In a thread, and bounded. `scutil` is capped at 2 s and `detect_tailscale`
    # runs two subprocesses at 3 s each, so an unresponsive `tailscaled` makes
    # this ~8 s of blocking work -- and uvicorn does not bind its listening
    # socket until this lifespan yields, while BridgeProcessManager.swift
    # restarts the bridge if it has not signalled ready within 15 s. Together
    # with the rest of startup (cleanup_stale_sessions, prune_dead, a
    # _pane_fg_command per session, tmux discovery) that is a restart loop in
    # which the bridge never answers. `routes.py` already wraps the same call in
    # `asyncio.to_thread`; this is the one place that did not.
    #
    # On timeout the thread is NOT stopped -- `asyncio.wait_for` cannot -- so it
    # runs to completion and sets the module global on its own. The names simply
    # arrive late, which is why no retry is scheduled here.
    from bridge.auth import refresh_own_names

    try:
        own = await asyncio.wait_for(asyncio.to_thread(refresh_own_names), timeout=4.0)
        _state.log_event("info", "bridge", f"Answering to {len(own)} host name(s)")
    except (TimeoutError, asyncio.TimeoutError):
        _state.log_event(
            "warning", "bridge",
            "Host-name resolution is slow (scutil/tailscale); starting anyway. "
            "REST routes may answer 421 for this machine's own names until it lands.",
        )

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
        floor_held=_state.speech_floor_held,
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

    # Put back any pane a previous run was killed while holding. Before
    # discovery, so what is discovered is already the right size -- and before
    # anything is served, so no client can take a lease over a stranded pane
    # and record the panel size as its original.
    try:
        await restore_stranded_panes()
    except Exception as e:
        _state.log_event("warning", "bridge", f"Pane geometry restore failed: {e}")

    # Discover assistant CLI instances in tmux that started before the bridge.
    discovered = await _discover_tmux_sessions()
    if discovered:
        _state.log_event("success", "bridge", f"Startup: discovered {discovered} running session(s) in tmux")

    _state._prune_task = asyncio.create_task(_periodic_prune())
    _state._activity_poll_task = asyncio.create_task(_activity_poll_loop())

    # Bonjour registration is synchronous and can block for 10+ seconds when
    # it fails (e.g. no mDNS responder).  Run it in a thread so it doesn't
    # block the lifespan from yielding — uvicorn won't bind the listening
    # socket until after yield, so blocking here delays HTTP availability.
    async def _register_bonjour() -> None:
        try:
            await asyncio.to_thread(
                bonjour.register, port=_state.server_port, auth_token=token or ""
            )
            _state.log_event("info", "bridge", "Bonjour: advertising _voxherd._tcp")
        except Exception as e:
            _state.log_event("warning", "bridge", f"Bonjour registration failed: {e}")

    _state._bonjour_task = asyncio.create_task(_register_bonjour())

    # A laptop changes address whenever it changes network, and `register`
    # captured the addresses once -- so the advert kept naming wherever the Mac
    # was when the bridge started, and a headset discovering the bridge by name
    # got a dead address and reported "can't reach the bridge". In a thread
    # because the lookup can block on an unresponsive resolver.
    async def _bonjour_refresh_loop() -> None:
        while True:
            await asyncio.sleep(_BONJOUR_REFRESH_SECONDS)
            # Also establishes an advert that never came up: the bridge starts
            # at login, routinely before the network, and a `register` that
            # threw there used to be permanent.
            moved = await asyncio.to_thread(bonjour.refresh)
            if moved:
                _state.log_event(
                    "info", "bridge", f"Bonjour: advertising on {', '.join(moved)}"
                )

    _state._bonjour_refresh_task = asyncio.create_task(_bonjour_refresh_loop())

    # Headless ready signal — emitted just before yield so the macOS menu-bar
    # app knows the server is about to accept connections.
    if _state.headless_port > 0:
        import json as _json, sys as _sys
        ready_line = _json.dumps({"status": "ready", "port": _state.headless_port})
        _sys.stdout.write(ready_line + "\n")
        _sys.stdout.flush()

    yield

    # --- Shutdown ---
    _state.log_event("info", "bridge", "Shutting down bridge server...")
    # Before the loop's own teardown cancels every task at once: a catalog
    # probe caught mid-spawn by that hangs the loop for good (see stop_probes).
    from bridge import catalog
    await catalog.stop_probes()
    if _state._prune_task:
        _state._prune_task.cancel()
    if _state._activity_poll_task:
        _state._activity_poll_task.cancel()
    if _state._bonjour_task:
        _state._bonjour_task.cancel()
    if getattr(_state, "_bonjour_refresh_task", None):
        _state._bonjour_refresh_task.cancel()
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


#: Largest request body the bridge will read, for every route. Generous next to
#: the biggest legitimate payload (a task description, a TTS line, a 5 000-char
#: dispatch) and small enough that concurrent requests cannot exhaust memory.
MAX_REQUEST_BODY_BYTES = 1024 * 1024


class BodySizeLimitMiddleware:
    """Refuse an oversized request body before anything parses it.

    Raw ASGI rather than ``BaseHTTPMiddleware`` because the limit has to apply
    *before* the body is read. The per-route check this replaces could not work:
    ``receive_event(request, event: dict)`` means FastAPI has already buffered
    and ``json.loads``-ed the whole body before the handler's first line runs,
    so a 60 MB POST was fully resident before the size test. It was also
    bypassable twice over -- chunked encoding sends no ``Content-Length`` (the
    code then read ``"0"``), and a client can simply lie in the header. The
    token-free pairing endpoints were reachable the same way, which made this
    an unauthenticated remote memory-exhaustion path.

    The body is drained here, bounded, and replayed to the app, so peak memory
    per in-flight request is the cap rather than whatever the client sends.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared = None
        expects_continue = False
        for name, value in scope.get("headers", []):
            if name == b"expect" and value.strip().lower() == b"100-continue":
                expects_continue = True
            elif name == b"content-length":
                try:
                    declared = int(value)
                except (TypeError, ValueError):
                    await self._reject(send, "Invalid Content-Length",
                                       None if expects_continue else receive)
                    return

        # A client holding its body for a ``100 Continue`` has sent nothing, so
        # there is nothing in flight to drain -- and the first ``receive()`` is
        # what makes uvicorn emit the ``100 Continue``, i.e. draining INVITES
        # the body we have already decided to refuse. Measured: the 413 went
        # from t+0.00 to t+2.00 and the server answered ``100 Continue`` first.
        drain_from = None if expects_continue else receive

        if declared is not None and declared > self.max_bytes:
            # Drained, not just refused: this branch reads none of the body, so
            # answering and returning closed the socket while the client was
            # still writing and it read a broken pipe instead of the 413.
            await self._reject(send, receive=drain_from)
            return

        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                # Covers the chunked and lying-header cases the old check missed.
                await self._reject(send, receive=drain_from)
                return
            if chunk:
                chunks.append(chunk)
            more = message.get("more_body", False)

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    #: How much of an over-sized body to keep reading, after deciding to refuse
    #: it, so the client can finish writing and read the response. Bounded: the
    #: point is not to accept the data, only to avoid slamming the socket shut
    #: mid-upload.
    #:
    #: Twice the cap, deliberately, and it was 8x. This middleware is registered
    #: last, so it is OUTERMOST -- it runs before ``auth_middleware`` -- which is
    #: the whole point (a token-free endpoint must not be able to spend memory),
    #: and a generous drain turns that around: measured with a real socket, a
    #: declared-40-MiB POST to ``/api/pair/start`` made the bridge read
    #: 8,441,856 bytes, eight times the limit it is enforcing, from an
    #: unauthenticated client, repeatably. The broken pipe this exists to
    #: prevent only happens when the whole body is already in flight, which is
    #: bodies just over the cap: an honest 1 MiB+64 needs 1,048,640 drained and
    #: gets its 413 at t+0.00, while a 12 MiB upload reads its 413 immediately
    #: with or without the drain. So 2 MiB covers the case and caps the
    #: amplification at 2x.
    DRAIN_LIMIT_BYTES = 2 * 1024 * 1024

    #: And bounded in time, because a client is not obliged to send what it
    #: declared. A header claiming 99 MiB followed by 64 bytes leaves uvicorn
    #: reporting ``more_body`` forever: the client is done writing and waiting
    #: for a reply, so a byte-bounded-only drain deadlocked the pair until the
    #: client timed out -- measured, after the drain was added for the honest
    #: case. Loopback and LAN finish 2 MiB far inside this; a client that
    #: declares a body and then stalls holds one connection for this long, so it
    #: is kept short.
    DRAIN_TIMEOUT_SECONDS = 1.0

    async def _drain(self, receive: Receive) -> None:
        """Read and discard the rest of the request, bounded by bytes and time.

        Without this, answering 413 and returning made uvicorn close while the
        client was still sending, so anyone who actually tripped the limit got a
        broken pipe instead of the response -- the "looks like it is hanging"
        failure the limit exists to prevent. A declared-oversize body was worst:
        that branch reads none of the body at all.

        Hitting either bound just stops reading. The response still goes out;
        uvicorn closes a connection whose body was not consumed, so a slow
        trickle costs this one socket and nothing is left in the pipe to be read
        as a following request.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.DRAIN_TIMEOUT_SECONDS
        drained = 0
        while drained < self.DRAIN_LIMIT_BYTES:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                return
            except Exception:
                return
            if message.get("type") == "http.disconnect":
                return
            drained += len(message.get("body", b""))
            if not message.get("more_body", False):
                return

    async def _reject(
        self,
        send: Send,
        detail: str = "Payload too large",
        receive: "Receive | None" = None,
    ) -> None:
        status = 400 if detail == "Invalid Content-Length" else 413
        if receive is not None:
            await self._drain(receive)
        payload = _json_bytes({"error": detail})
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


def _json_bytes(obj: dict) -> bytes:
    import json as _j

    return _j.dumps(obj).encode()


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

# Body size limit. Registered LAST so it runs FIRST: Starlette builds the stack
# so that the most recently added middleware is outermost. Reading a 60 MB body
# must not be a prerequisite for rejecting it, and the token-free pairing
# endpoints need the limit too — so this has to sit outside auth, not inside it.
app.add_middleware(BodySizeLimitMiddleware)

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
            on_state=_broadcast_voice_state,
        )
        _state.voice_loop.wire()
        if _state.wake_word_enabled:
            _state.voice_loop.start_wake_word()


_voice_broadcasts: set = set()


def _broadcast_voice_state(fields: dict) -> None:
    """Push the mic state to every client.

    Fire-and-forget on purpose -- the authority is the copy carried in
    ``state_sync``, so a client that misses this still reconciles on connect.
    """
    msg = _state.set_voice_state(**fields)
    try:
        # Held in a set until it finishes: the event loop keeps only a weak
        # reference, so a bare ``create_task`` can be collected mid-flight and
        # the broadcast silently never happens.
        task = asyncio.get_running_loop().create_task(broadcast_to_ios(msg))
        _voice_broadcasts.add(task)
        task.add_done_callback(_voice_broadcasts.discard)
    except RuntimeError:
        # No loop (tests, or shutdown). The state is still updated, which is
        # what state_sync reads.
        pass
