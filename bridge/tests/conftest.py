"""Shared fixtures for VoxHerd bridge server tests.

Sets up the FastAPI test client with auth token, and provides helpers
for registering sessions and sending events.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch, AsyncMock

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

# Set a known auth token BEFORE importing bridge_server, so the module-level
# _AUTH_TOKEN picks it up.
TEST_AUTH_TOKEN = "test-token-for-bridge-tests"
os.environ["VOXHERD_AUTH_TOKEN"] = TEST_AUTH_TOKEN


# We need to mock several things before importing bridge_server:
# 1. The lifespan calls ensure_auth_token(), prune_dead(), tmux stuff, bonjour
# 2. MacTTS.start() tries to create asyncio tasks
# 3. SessionManager._load() tries to read ~/.voxherd/sessions.json

from bridge import bridge_server  # noqa: E402


def _auth_headers() -> dict[str, str]:
    """Return authorization headers for test requests."""
    return {
        "Authorization": f"Bearer {TEST_AUTH_TOKEN}",
        "X-VoxHerd": "1",
    }


# Create a persistent temp directory for test project_dirs under $HOME
# (the bridge validates project_dir is under user home).
_TEST_PROJECT_DIR = os.path.join(os.path.expanduser("~"), ".voxherd", "test-projects", "myproject")
os.makedirs(_TEST_PROJECT_DIR, exist_ok=True)


@pytest_asyncio.fixture
async def client():
    """Async HTTP client that talks to the FastAPI app via ASGI transport.

    Resets module-level state between tests so sessions don't leak.
    Mocks lifespan side-effects (TTS, bonjour, tmux cleanup, prune, voice loop).
    """
    # Save original state
    orig_sessions = bridge_server.sessions._sessions.copy()
    orig_active = bridge_server.sessions.active_project
    orig_last_announced = bridge_server.sessions.last_announced_project
    orig_last_announced_sid = bridge_server.sessions.last_announced_session_id
    orig_ios = bridge_server.ios_connections.copy()

    # Clear state for a clean test
    bridge_server.sessions._sessions.clear()
    bridge_server.sessions.active_project = None
    bridge_server.sessions.last_announced_project = None
    bridge_server.sessions.last_announced_session_id = None
    bridge_server.ios_connections.clear()

    # Patch side-effects: disk writes, TTS, bonjour, tmux, voice loop
    with (
        patch.object(bridge_server.sessions, "_save", return_value=None),
        patch.object(bridge_server.mac_tts, "speak", return_value=None),
        patch.object(bridge_server.mac_tts, "start", return_value=None),
        patch.object(bridge_server.mac_tts, "stop", return_value=None),
        patch.object(bridge_server.sessions, "prune_dead", new_callable=AsyncMock, return_value=[]),
        patch("bridge.bridge_server._init_voice_loop", return_value=None),
        patch("bridge.bridge_server._discover_tmux_sessions", new_callable=AsyncMock, return_value=0),
        patch("bridge.bridge_server._cancel_terminal_subs_for_session", new_callable=AsyncMock),
        patch("bridge.bridge_server.bonjour") as mock_bonjour,
        patch("bridge.bridge_server._periodic_prune", new_callable=AsyncMock),
        patch("bridge.bridge_server._activity_poll_loop", new_callable=AsyncMock),
    ):
        mock_bonjour.register = lambda **kwargs: None
        mock_bonjour.unregister = lambda: None
        transport = ASGITransport(app=bridge_server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as ac:
            yield ac

    # Restore original state
    bridge_server.sessions._sessions = orig_sessions
    bridge_server.sessions.active_project = orig_active
    bridge_server.sessions.last_announced_project = orig_last_announced
    bridge_server.sessions.last_announced_session_id = orig_last_announced_sid
    bridge_server.ios_connections = orig_ios


@pytest_asyncio.fixture
async def noauth_client():
    """Async HTTP client with auth disabled (no token set).

    Used to test that endpoints work without auth when the token is cleared.
    """
    # Temporarily disable auth — must patch the canonical location in bridge.auth
    # (bridge_server._AUTH_TOKEN is a re-export and won't affect the middleware)
    import bridge.auth as _auth_mod
    original_token = _auth_mod._AUTH_TOKEN
    _auth_mod._AUTH_TOKEN = None

    bridge_server.sessions._sessions.clear()
    bridge_server.sessions.active_project = None
    bridge_server.sessions.last_announced_project = None
    bridge_server.sessions.last_announced_session_id = None
    bridge_server.ios_connections.clear()

    with (
        patch.object(bridge_server.sessions, "_save", return_value=None),
        patch.object(bridge_server.mac_tts, "speak", return_value=None),
        patch.object(bridge_server.mac_tts, "start", return_value=None),
        patch.object(bridge_server.mac_tts, "stop", return_value=None),
        patch.object(bridge_server.sessions, "prune_dead", new_callable=AsyncMock, return_value=[]),
        patch("bridge.bridge_server._init_voice_loop", return_value=None),
        patch("bridge.bridge_server._discover_tmux_sessions", new_callable=AsyncMock, return_value=0),
        patch("bridge.bridge_server._cancel_terminal_subs_for_session", new_callable=AsyncMock),
        patch("bridge.bridge_server.bonjour") as mock_bonjour,
        patch("bridge.bridge_server._periodic_prune", new_callable=AsyncMock),
        patch("bridge.bridge_server._activity_poll_loop", new_callable=AsyncMock),
    ):
        mock_bonjour.register = lambda **kwargs: None
        mock_bonjour.unregister = lambda: None
        transport = ASGITransport(app=bridge_server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as ac:
            yield ac

    _auth_mod._AUTH_TOKEN = original_token


def auth_headers() -> dict[str, str]:
    """Convenience for tests to get auth headers."""
    return _auth_headers()


async def register_test_session(
    client: httpx.AsyncClient,
    session_id: str = "test-session-abc123",
    project: str = "myproject",
    project_dir: str | None = None,
    tmux_target: str | None = None,
    assistant: str | None = None,
) -> httpx.Response:
    """Helper: register a session via the API."""
    body = {
        "session_id": session_id,
        "project": project,
        "project_dir": project_dir or _TEST_PROJECT_DIR,
    }
    if tmux_target is not None:
        body["tmux_target"] = tmux_target
    if assistant is not None:
        body["assistant"] = assistant
    return await client.post(
        "/api/sessions/register",
        json=body,
        headers=_auth_headers(),
    )
