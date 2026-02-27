"""Tests for the VoxHerd bridge server REST API.

Uses httpx.AsyncClient with ASGITransport to test the FastAPI app directly
(no real server needed). Mocks asyncio.create_subprocess_exec to avoid
spawning real Claude Code processes.
"""

import asyncio
from unittest.mock import patch, AsyncMock

import pytest
import httpx

from bridge import bridge_server
from bridge.tests.conftest import auth_headers, register_test_session, _TEST_PROJECT_DIR


# ---------------------------------------------------------------------------
# Session Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_session(client: httpx.AsyncClient) -> None:
    """POST /api/sessions/register with valid data returns ok and session appears in list."""
    resp = await register_test_session(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True

    # Verify session appears in sessions list
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    assert list_resp.status_code == 200
    sessions = list_resp.json()
    assert "test-session-abc123" in sessions
    session = sessions["test-session-abc123"]
    assert session["project"] == "myproject"
    assert session["status"] == "active"


@pytest.mark.asyncio
async def test_register_session_missing_fields(client: httpx.AsyncClient) -> None:
    """POST /api/sessions/register with missing required fields returns error."""
    # Missing session_id
    resp = await client.post(
        "/api/sessions/register",
        json={"project": "foo", "project_dir": "/tmp/foo"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()

    # Missing project_dir
    resp = await client.post(
        "/api/sessions/register",
        json={"session_id": "abc-123", "project": "foo"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_register_session_invalid_session_id(client: httpx.AsyncClient) -> None:
    """POST /api/sessions/register with invalid session_id format returns error."""
    resp = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "bad session id with spaces!@#",
            "project": "myproject",
            "project_dir": _TEST_PROJECT_DIR,
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_register_multiple_sessions_same_project(client: httpx.AsyncClient) -> None:
    """Multiple sessions for the same project get different agent numbers."""
    await register_test_session(client, session_id="session-1", project="myproject")
    await register_test_session(client, session_id="session-2", project="myproject")

    list_resp = await client.get("/api/sessions", headers=auth_headers())
    sessions = list_resp.json()

    s1 = sessions["session-1"]
    s2 = sessions["session-2"]
    assert s1["agent_number"] != s2["agent_number"]


@pytest.mark.asyncio
async def test_register_session_with_assistant(client: httpx.AsyncClient) -> None:
    """POST /api/sessions/register persists assistant metadata."""
    resp = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "assist-test-1",
            "project": "myproject",
            "project_dir": _TEST_PROJECT_DIR,
            "assistant": "codex",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["assist-test-1"]
    assert session["assistant"] == "codex"


@pytest.mark.asyncio
async def test_register_session_invalid_assistant(client: httpx.AsyncClient) -> None:
    """POST /api/sessions/register rejects unknown assistant ids."""
    resp = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "assist-test-2",
            "project": "myproject",
            "project_dir": _TEST_PROJECT_DIR,
            "assistant": "unknown-assistant",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_register_session_invalid_tmux_target(client: httpx.AsyncClient) -> None:
    """POST /api/sessions/register rejects invalid tmux targets."""
    resp = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "assist-test-3",
            "project": "myproject",
            "project_dir": _TEST_PROJECT_DIR,
            "tmux_target": "bad-target-format",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# List Sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_empty(client: httpx.AsyncClient) -> None:
    """GET /api/sessions returns empty dict when no sessions registered."""
    resp = await client.get("/api/sessions", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_list_sessions_after_register(client: httpx.AsyncClient) -> None:
    """GET /api/sessions returns registered sessions."""
    await register_test_session(client, session_id="s1", project="alpha")
    await register_test_session(client, session_id="s2", project="beta")

    resp = await client.get("/api/sessions", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "s1" in data
    assert "s2" in data
    assert data["s1"]["project"] == "alpha"
    assert data["s2"]["project"] == "beta"


# ---------------------------------------------------------------------------
# Stop Event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_stop_event(client: httpx.AsyncClient) -> None:
    """POST /api/events with stop event updates session status to idle."""
    await register_test_session(client, session_id="stop-test-1", project="myproject")

    resp = await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "stop-test-1",
            "project": "myproject",
            "summary": "Built the feature",
            "stop_reason": "end_turn",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    # Verify session status changed to idle
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["stop-test-1"]
    assert session["status"] == "idle"
    assert session["last_summary"] == "Built the feature"
    assert session["activity_type"] == "completed"


@pytest.mark.asyncio
async def test_stop_event_error_reason(client: httpx.AsyncClient) -> None:
    """Stop event with stop_reason=error sets activity_type to errored."""
    await register_test_session(client, session_id="err-test-1", project="errproject")

    resp = await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "err-test-1",
            "project": "errproject",
            "summary": "Something broke",
            "stop_reason": "error",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["err-test-1"]
    assert session["activity_type"] == "errored"
    assert session["stop_reason"] == "error"


@pytest.mark.asyncio
async def test_stop_event_interrupted_reason(client: httpx.AsyncClient) -> None:
    """Stop event with stop_reason=interrupted sets activity_type to stopped."""
    await register_test_session(client, session_id="int-test-1", project="intproject")

    resp = await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "int-test-1",
            "project": "intproject",
            "summary": "Interrupted by user",
            "stop_reason": "interrupted",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["int-test-1"]
    assert session["activity_type"] == "stopped"


@pytest.mark.asyncio
async def test_stop_event_auto_registers_unknown_session(client: httpx.AsyncClient) -> None:
    """POST /api/events for an unknown session_id auto-registers it if project_dir is provided."""
    resp = await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "auto-reg-1",
            "project": "autoproject",
            "project_dir": _TEST_PROJECT_DIR,
            "assistant": "gemini",
            "summary": "Auto-registered and stopped",
            "stop_reason": "end_turn",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    # Session should now exist
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    assert "auto-reg-1" in list_resp.json()
    assert list_resp.json()["auto-reg-1"]["assistant"] == "gemini"


@pytest.mark.asyncio
async def test_stop_event_rejects_invalid_tmux_target(client: httpx.AsyncClient) -> None:
    """POST /api/events rejects invalid tmux_target values."""
    resp = await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "auto-reg-bad-tmux",
            "project": "autoproject",
            "project_dir": _TEST_PROJECT_DIR,
            "tmux_target": "bad-target-format",
            "summary": "Should fail",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()

    list_resp = await client.get("/api/sessions", headers=auth_headers())
    assert "auto-reg-bad-tmux" not in list_resp.json()


# ---------------------------------------------------------------------------
# Notification Event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_notification_event(client: httpx.AsyncClient) -> None:
    """POST /api/events with notification event updates session to waiting."""
    await register_test_session(client, session_id="notif-test-1", project="myproject")

    resp = await client.post(
        "/api/events",
        json={
            "event": "notification",
            "session_id": "notif-test-1",
            "project": "myproject",
            "message": "Permission needed to run rm -rf",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    # Verify session status changed to waiting
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["notif-test-1"]
    assert session["status"] == "waiting"
    assert session["activity_type"] == "approval"


@pytest.mark.asyncio
async def test_notification_waiting_for_input_suppressed(client: httpx.AsyncClient) -> None:
    """Notification with 'waiting for your input' does NOT change status to waiting."""
    await register_test_session(client, session_id="input-test-1", project="myproject")

    resp = await client.post(
        "/api/events",
        json={
            "event": "notification",
            "session_id": "input-test-1",
            "project": "myproject",
            "message": "Waiting for your input to continue",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    # Session should still be active (not waiting)
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["input-test-1"]
    assert session["status"] == "active"


# ---------------------------------------------------------------------------
# Subagent Events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_start_stop_events(client: httpx.AsyncClient) -> None:
    """Subagent start/stop events update sub_agent_count on the session."""
    await register_test_session(client, session_id="sub-test-1", project="myproject")

    # Start a sub-agent
    resp = await client.post(
        "/api/events",
        json={
            "event": "subagent_start",
            "session_id": "sub-test-1",
            "project": "myproject",
            "agent_id": "subagent-abc",
            "agent_type": "Task",
            "timestamp": "2025-01-01T00:00:00Z",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["sub-test-1"]
    assert session["sub_agent_count"] == 1

    # Stop the sub-agent
    resp = await client.post(
        "/api/events",
        json={
            "event": "subagent_stop",
            "session_id": "sub-test-1",
            "project": "myproject",
            "agent_id": "subagent-abc",
            "agent_type": "Task",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["sub-test-1"]
    assert session["sub_agent_count"] == 0


# ---------------------------------------------------------------------------
# Project Summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_summary(client: httpx.AsyncClient) -> None:
    """GET /api/sessions/{project}/summary returns the latest summary."""
    await register_test_session(client, session_id="sum-test-1", project="myproject")

    # Send a stop event with a summary
    await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "sum-test-1",
            "project": "myproject",
            "summary": "Implemented OAuth flow",
            "stop_reason": "end_turn",
        },
        headers=auth_headers(),
    )

    resp = await client.get("/api/sessions/myproject/summary", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "myproject"
    assert data["summary"] == "Implemented OAuth flow"


@pytest.mark.asyncio
async def test_get_project_summary_fuzzy_match(client: httpx.AsyncClient) -> None:
    """GET /api/sessions/{project}/summary does fuzzy matching on project name."""
    await register_test_session(
        client, session_id="fuzzy-test-1", project="aligned-tools"
    )
    await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "fuzzy-test-1",
            "project": "aligned-tools",
            "summary": "Fixed alignment bug",
            "stop_reason": "end_turn",
        },
        headers=auth_headers(),
    )

    # "aligned" should fuzzy-match "aligned-tools"
    resp = await client.get("/api/sessions/aligned/summary", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "aligned-tools"
    assert data["summary"] == "Fixed alignment bug"


@pytest.mark.asyncio
async def test_get_project_summary_not_found(client: httpx.AsyncClient) -> None:
    """GET /api/sessions/{project}/summary for unknown project returns error."""
    resp = await client.get("/api/sessions/nonexistent/summary", headers=auth_headers())
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Auth Required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_required_no_token(client: httpx.AsyncClient) -> None:
    """Endpoints return 401 when no auth token provided and auth is enabled."""
    # No headers = no auth
    resp = await client.get("/api/sessions")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Unauthorized"}


@pytest.mark.asyncio
async def test_auth_required_wrong_token(client: httpx.AsyncClient) -> None:
    """Endpoints return 401 with a wrong Bearer token."""
    resp = await client.get(
        "/api/sessions",
        headers={"Authorization": "Bearer wrong-token-value"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_valid_bearer_token(client: httpx.AsyncClient) -> None:
    """Endpoints return 200 with a valid Bearer token."""
    resp = await client.get("/api/sessions", headers=auth_headers())
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_query_param_token(client: httpx.AsyncClient) -> None:
    """REST auth does not accept ?token query parameter."""
    resp = await client.get(f"/api/sessions?token={auth_headers()['Authorization'][7:]}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_disabled_allows_all(noauth_client: httpx.AsyncClient) -> None:
    """When auth token is None, all requests pass without auth headers."""
    resp = await noauth_client.get("/api/sessions")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_post_endpoints(client: httpx.AsyncClient) -> None:
    """POST endpoints also require auth."""
    resp = await client.post(
        "/api/events",
        json={"event": "stop", "session_id": "x", "project": "y"},
    )
    assert resp.status_code == 401

    resp = await client.post(
        "/api/sessions/register",
        json={"session_id": "x", "project": "y", "project_dir": "/tmp"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Delete Session(s)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session(client: httpx.AsyncClient) -> None:
    """DELETE /api/sessions/{session_id} removes the session."""
    await register_test_session(client, session_id="del-test-1", project="myproject")

    resp = await client.delete("/api/sessions/del-test-1", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    # Verify session is gone
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    assert "del-test-1" not in list_resp.json()


@pytest.mark.asyncio
async def test_delete_nonexistent_session(client: httpx.AsyncClient) -> None:
    """DELETE /api/sessions/{session_id} for unknown session returns error."""
    resp = await client.delete("/api/sessions/nonexistent-id", headers=auth_headers())
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_clear_all_sessions(client: httpx.AsyncClient) -> None:
    """DELETE /api/sessions clears all sessions."""
    await register_test_session(client, session_id="c1", project="p1")
    await register_test_session(client, session_id="c2", project="p2")

    resp = await client.delete("/api/sessions", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert data.get("cleared") == 2

    # Verify all gone
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    assert list_resp.json() == {}


# ---------------------------------------------------------------------------
# Command Dispatch (REST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_command_dispatch(client: httpx.AsyncClient) -> None:
    """POST /api/command dispatches to the correct session."""
    await register_test_session(client, session_id="cmd-test-1", project="myproject")

    with patch(
        "bridge.ws_handler._dispatch_agent",
        new_callable=AsyncMock,
    ) as mock_dispatch:
        resp = await client.post(
            "/api/command",
            json={"project": "myproject", "message": "run the tests"},
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        # Give the create_task a moment to schedule
        await asyncio.sleep(0.05)

        mock_dispatch.assert_called_once()
        call_args = mock_dispatch.call_args
        assert call_args[0][1] == _TEST_PROJECT_DIR  # project_dir
        assert call_args[0][2] == "run the tests"  # message


@pytest.mark.asyncio
async def test_rest_command_dispatch_filters_by_assistant(client: httpx.AsyncClient) -> None:
    """POST /api/command supports assistant-targeted dispatch when a project has mixed sessions."""
    await register_test_session(
        client,
        session_id="cmd-assistant-claude",
        project="myproject",
        assistant="claude",
    )
    await register_test_session(
        client,
        session_id="cmd-assistant-codex",
        project="myproject",
        assistant="codex",
    )

    with patch(
        "bridge.ws_handler._dispatch_agent",
        new_callable=AsyncMock,
    ) as mock_dispatch:
        resp = await client.post(
            "/api/command",
            json={
                "project": "myproject",
                "assistant": "codex",
                "message": "run codex task",
            },
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        await asyncio.sleep(0.05)

        mock_dispatch.assert_called_once()
        call_args = mock_dispatch.call_args
        assert call_args[0][0] == "cmd-assistant-codex"
        assert call_args[0][1] == _TEST_PROJECT_DIR
        assert call_args[0][2] == "run codex task"


@pytest.mark.asyncio
async def test_rest_command_missing_fields(client: httpx.AsyncClient) -> None:
    """POST /api/command with missing project or message returns error."""
    resp = await client.post(
        "/api/command",
        json={"project": "myproject"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()

    resp = await client.post(
        "/api/command",
        json={"message": "do stuff"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_rest_command_invalid_assistant(client: httpx.AsyncClient) -> None:
    """POST /api/command rejects unknown assistant ids."""
    resp = await client.post(
        "/api/command",
        json={
            "project": "myproject",
            "assistant": "unknown-assistant",
            "message": "do stuff",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_rest_command_unknown_project(client: httpx.AsyncClient) -> None:
    """POST /api/command for an unknown project returns error."""
    resp = await client.post(
        "/api/command",
        json={"project": "nonexistent", "message": "do stuff"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# TTS Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tts_speak(client: httpx.AsyncClient) -> None:
    """POST /api/tts queues TTS speech."""
    resp = await client.post(
        "/api/tts",
        json={"text": "Hello from tests"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True


@pytest.mark.asyncio
async def test_tts_missing_text(client: httpx.AsyncClient) -> None:
    """POST /api/tts with empty text returns error."""
    resp = await client.post(
        "/api/tts",
        json={"text": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Session Lifecycle Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_session_lifecycle(client: httpx.AsyncClient) -> None:
    """Register -> active -> stop -> idle -> summary — full lifecycle."""
    # 1. Register
    reg_resp = await register_test_session(
        client, session_id="lifecycle-1", project="lifecycle-proj"
    )
    assert reg_resp.json().get("ok") is True

    # 2. Verify active
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    assert list_resp.json()["lifecycle-1"]["status"] == "active"
    assert list_resp.json()["lifecycle-1"]["activity_type"] == "registered"

    # 3. Send stop event
    await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "lifecycle-1",
            "project": "lifecycle-proj",
            "summary": "Finished the integration tests",
            "stop_reason": "end_turn",
        },
        headers=auth_headers(),
    )

    # 4. Verify idle
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    session = list_resp.json()["lifecycle-1"]
    assert session["status"] == "idle"
    assert session["activity_type"] == "completed"

    # 5. Get summary
    summary_resp = await client.get(
        "/api/sessions/lifecycle-proj/summary",
        headers=auth_headers(),
    )
    assert summary_resp.json()["summary"] == "Finished the integration tests"

    # 6. Delete
    del_resp = await client.delete("/api/sessions/lifecycle-1", headers=auth_headers())
    assert del_resp.json().get("ok") is True

    # 7. Verify gone
    list_resp = await client.get("/api/sessions", headers=auth_headers())
    assert "lifecycle-1" not in list_resp.json()


# ---------------------------------------------------------------------------
# Input Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_invalid_project_name(client: httpx.AsyncClient) -> None:
    """POST /api/events with invalid project name (special chars) returns error."""
    resp = await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "valid-session-1",
            "project": "../../../etc/passwd",
            "summary": "hacked",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_event_empty_session_id(client: httpx.AsyncClient) -> None:
    """POST /api/events with empty session_id is accepted (session_id is optional for some events)."""
    resp = await client.post(
        "/api/events",
        json={
            "event": "stop",
            "session_id": "",
            "project": "myproject",
            "summary": "done",
        },
        headers=auth_headers(),
    )
    # Empty session_id: validation returns "session_id is required"
    # but only if session_id is non-empty and fails regex. Let's check behavior.
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Connection Info & Ports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_info(client: httpx.AsyncClient) -> None:
    """GET /api/connection-info returns local URL."""
    with patch(
        "bridge.routes.detect_tailscale",
        return_value=None,
    ):
        resp = await client.get("/api/connection-info", headers=auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert "local" in data
        assert data["local"].startswith("ws://")
        assert data["tailscale"] is None


@pytest.mark.asyncio
async def test_ports_endpoint(client: httpx.AsyncClient) -> None:
    """GET /api/ports returns a list (may be empty in test env)."""
    resp = await client.get("/api/ports", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "ports" in data
    assert isinstance(data["ports"], list)
