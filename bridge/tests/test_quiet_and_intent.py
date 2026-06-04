"""Regression tests for two server-side gaps found in the 2026-06-03 bug hunt.

#2 — `/api/intent/parse` returned the raw `claude --output-format json`
     envelope ({"type":"result","result":"<action json string>",...}) verbatim.
     iOS reads top-level `action`, which is absent on the envelope, so the
     entire Haiku NLU fallback degraded to "Which project?". The endpoint must
     unwrap+parse the inner `result`.

#3 — VOXHERD_QUIET was half-wired server-side: the register payload's `quiet`
     flag was dropped (never read from the body), and neither the "registered"
     announcement nor the approval narration honored it. A quiet worker must be
     fully silent (no register/approval speech) while still visible on the lens,
     and its stop must stamp skip_tts so the lens summary card auto-dismisses.
"""

import json
from unittest.mock import patch

import pytest
import httpx

from bridge import bridge_server
from bridge.tests.conftest import auth_headers, register_test_session, _TEST_PROJECT_DIR


def _fake_exec_returning(stdout_bytes: bytes):
    """Patch target for asyncio.create_subprocess_exec — a fake proc whose
    communicate() yields the given stdout."""
    async def _fake_exec(*args, **kwargs):
        class _Proc:
            stdout = None
            stderr = None
            returncode = 0

            async def communicate(self):
                return (stdout_bytes, b"")
        return _Proc()
    return _fake_exec


# ---------------------------------------------------------------------------
# #2 — /api/intent/parse envelope unwrapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_intent_parse_unwraps_cli_envelope(client: httpx.AsyncClient) -> None:
    action = {"action": "tell", "project": "aligned-tools", "message": "run the tests"}
    envelope = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": json.dumps(action), "session_id": "x", "total_cost_usd": 0.0001,
    }
    with patch("asyncio.create_subprocess_exec",
               _fake_exec_returning(json.dumps(envelope).encode())):
        resp = await client.post(
            "/api/intent/parse",
            json={"transcription": "have aligned tools run the tests",
                  "known_projects": ["aligned-tools"]},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert resp.json() == action, "endpoint must return the unwrapped action, not the envelope"


@pytest.mark.asyncio
async def test_intent_parse_tolerates_fenced_inner_json(client: httpx.AsyncClient) -> None:
    """Haiku sometimes wraps the JSON in ```json fences — unwrap must still work."""
    action = {"action": "status"}
    envelope = {"type": "result", "is_error": False,
                "result": "```json\n" + json.dumps(action) + "\n```"}
    with patch("asyncio.create_subprocess_exec",
               _fake_exec_returning(json.dumps(envelope).encode())):
        resp = await client.post(
            "/api/intent/parse",
            json={"transcription": "status"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert resp.json() == action


@pytest.mark.asyncio
async def test_intent_parse_passes_through_bare_object(client: httpx.AsyncClient) -> None:
    """A bare action object (no envelope) is returned as-is for forward-compat."""
    action = {"action": "switch", "project": "homeschool"}
    with patch("asyncio.create_subprocess_exec",
               _fake_exec_returning(json.dumps(action).encode())):
        resp = await client.post(
            "/api/intent/parse",
            json={"transcription": "switch to homeschool"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert resp.json() == action


@pytest.mark.asyncio
async def test_intent_parse_reports_error_on_is_error_envelope(client: httpx.AsyncClient) -> None:
    envelope = {"type": "result", "is_error": True, "result": "the model failed"}
    with patch("asyncio.create_subprocess_exec",
               _fake_exec_returning(json.dumps(envelope).encode())):
        resp = await client.post(
            "/api/intent/parse",
            json={"transcription": "whatever"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# #3 — VOXHERD_QUIET enforced server-side
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_stores_quiet_flag(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/sessions/register",
        json={"session_id": "q1", "project": "myproject",
              "project_dir": _TEST_PROJECT_DIR, "quiet": True},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    sessions = (await client.get("/api/sessions", headers=auth_headers())).json()
    assert sessions["q1"]["quiet"] is True, "register must persist the quiet flag from the payload"


@pytest.mark.asyncio
async def test_register_quiet_suppresses_announcement(client: httpx.AsyncClient) -> None:
    with patch.object(bridge_server.mac_tts, "speak") as mock_speak:
        await client.post(
            "/api/sessions/register",
            json={"session_id": "q2", "project": "myproject",
                  "project_dir": _TEST_PROJECT_DIR, "quiet": True},
            headers=auth_headers(),
        )
    mock_speak.assert_not_called()


@pytest.mark.asyncio
async def test_register_nonquiet_still_announces(client: httpx.AsyncClient) -> None:
    with patch.object(bridge_server.mac_tts, "speak") as mock_speak:
        await register_test_session(client, session_id="q3")
    assert mock_speak.called, "a normal (non-quiet) session should still announce 'registered'"


@pytest.mark.asyncio
async def test_quiet_session_approval_not_spoken(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/sessions/register",
        json={"session_id": "q4", "project": "myproject",
              "project_dir": _TEST_PROJECT_DIR, "quiet": True},
        headers=auth_headers(),
    )
    with patch.object(bridge_server.mac_tts, "speak") as mock_speak:
        resp = await client.post(
            "/api/events",
            json={"event": "notification", "session_id": "q4", "project": "myproject",
                  "message": "Claude needs your permission to run rm -rf"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    mock_speak.assert_not_called()


@pytest.mark.asyncio
async def test_quiet_session_stop_stamps_skip_tts_and_is_silent(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/sessions/register",
        json={"session_id": "q6", "project": "myproject",
              "project_dir": _TEST_PROJECT_DIR, "quiet": True},
        headers=auth_headers(),
    )
    captured: list[dict] = []

    async def _capture(msg):
        captured.append(msg)

    with patch("bridge.routes.broadcast_to_ios", new=_capture), \
         patch.object(bridge_server.mac_tts, "speak") as mock_speak:
        resp = await client.post(
            "/api/events",
            json={"event": "stop", "session_id": "q6", "project": "myproject",
                  "summary": "did a thing", "stop_reason": "end_turn"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    mock_speak.assert_not_called()  # quiet → never spoken, even via the fallback path
    stop_events = [m for m in captured
                   if m.get("type") == "agent_event" and m.get("event") == "stop"]
    assert stop_events, "stop must still broadcast an agent_event so the lens shows the card"
    assert stop_events[0].get("skip_tts") is True, \
        "quiet stop must stamp skip_tts:true so the lens summary card auto-dismisses"
