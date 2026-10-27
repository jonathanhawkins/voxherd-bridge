"""The speech floor: which device speaks narration.

Narration is spoken on the Mac and broadcast to every client, and every phone
speaks it. A headset speaking through Earshot claims the floor, and while it
holds it the Mac and the phones stay quiet -- one summary, one voice.

Both ways this goes wrong are silent. A floor that is not honoured makes one
summary three voices. A floor that outlives its headset leaves the phone and
the Mac mute for a device nobody is wearing, which looks exactly like nothing
happening. So the floor is a lease that can only lapse, never stick.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from bridge import bridge_server, ws_handler
from bridge import server_state as state
from bridge.narration import NarrationEngine, NarrationEvent
from bridge.tests.conftest import auth_headers, register_test_session


@pytest.fixture(autouse=True)
def _no_floor():
    state.claim_speech_floor(False)
    yield
    state.claim_speech_floor(False)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


# --- the lease ----------------------------------------------------------------


def test_a_claim_holds_the_floor_and_says_it_changed() -> None:
    assert state.claim_speech_floor(True, now=100.0) is True
    assert state.speech_floor_held(now=100.0)


def test_a_renewal_changes_nothing() -> None:
    state.claim_speech_floor(True, now=100.0)
    assert state.claim_speech_floor(True, now=120.0) is False
    # ...but it does push the lapse out.
    assert state.speech_floor_held(now=100.0 + state.SPEECH_FLOOR_TTL_S + 10)


def test_an_unrenewed_floor_lapses_on_its_own() -> None:
    state.claim_speech_floor(True, now=100.0)
    assert state.speech_floor_held(now=100.0 + state.SPEECH_FLOOR_TTL_S - 0.1)
    assert not state.speech_floor_held(now=100.0 + state.SPEECH_FLOOR_TTL_S)


def test_releasing_says_it_changed() -> None:
    state.claim_speech_floor(True)
    assert state.claim_speech_floor(False) is True
    assert not state.speech_floor_held()
    assert state.claim_speech_floor(False) is False


def test_only_its_holder_going_away_releases_it() -> None:
    headset, phone = object(), object()
    state.claim_speech_floor(True, holder=headset)
    assert state.release_speech_floor_of(phone) is False
    assert state.speech_floor_held()
    assert state.release_speech_floor_of(headset) is True
    assert not state.speech_floor_held()


def test_state_sync_carries_the_floor() -> None:
    assert state._state_sync_msg()["speech_floor"] == {"held": False, "expires_in": 0}
    state.claim_speech_floor(True)
    floor = state._state_sync_msg()["speech_floor"]
    assert floor["held"] is True
    assert 0 < floor["expires_in"] <= state.SPEECH_FLOOR_TTL_S


def test_the_bridge_lease_outlasts_the_headsets_renewals() -> None:
    # The headset renews every 20 s (SpeechFloorLease.RENEW_MS); one lost
    # renewal must not hand the floor back mid-sentence.
    assert state.SPEECH_FLOOR_TTL_S >= 2 * 20 + 10


# --- the socket message ----------------------------------------------------------


@pytest.fixture
def listeners():
    """Two connected clients that record every broadcast."""
    a, b = FakeSocket(), FakeSocket()
    saved = list(state.ios_connections)
    state.ios_connections[:] = [a, b]
    yield a, b
    state.ios_connections[:] = saved


def _syncs(sock: FakeSocket) -> list[dict]:
    return [m for m in sock.sent if m.get("type") == "state_sync"]


def test_a_claim_tells_every_client(listeners) -> None:
    headset, phone = listeners
    asyncio.run(ws_handler._handle_speech_floor(headset, {"type": "speech_floor", "claim": True}))
    assert state.speech_floor_held()
    assert _syncs(phone)[-1]["speech_floor"]["held"] is True


def test_a_renewal_tells_nobody(listeners) -> None:
    headset, phone = listeners
    asyncio.run(ws_handler._handle_speech_floor(headset, {"claim": True}))
    phone.sent.clear()
    asyncio.run(ws_handler._handle_speech_floor(headset, {"claim": True}))
    assert phone.sent == []


@pytest.mark.parametrize("claim", ["true", 1, None, "yes"])
def test_only_a_real_boolean_claims(listeners, claim) -> None:
    headset, phone = listeners
    asyncio.run(ws_handler._handle_speech_floor(headset, {"claim": claim}))
    assert not state.speech_floor_held()
    assert [m["type"] for m in headset.sent] == ["error"]
    assert phone.sent == []


def test_the_message_is_allowlisted() -> None:
    assert "speech_floor" in ws_handler._KNOWN_WS_TYPES


# --- what the floor silences ----------------------------------------------------


class FakeTts:
    def __init__(self) -> None:
        self.said: list[str] = []

    def speak(self, text: str, **_kwargs) -> None:
        self.said.append(text)


def _engine(floor: bool):
    tts, sent = FakeTts(), []

    async def broadcast(msg: dict) -> None:
        sent.append(msg)

    engine = NarrationEngine(
        tts=tts, broadcast_fn=broadcast, log_fn=lambda *a: None, sessions=None,
        floor_held=lambda: floor,
    )
    return engine, tts, sent


def _event() -> NarrationEvent:
    return NarrationEvent(event_type="completed", project="voxherd", session_id="s-1",
                          summary="Added retry logic.", tts_text="Added retry logic.")


def test_narration_is_the_headsets_while_it_holds_the_floor() -> None:
    engine, tts, sent = _engine(floor=True)
    asyncio.run(engine._emit(_event()))
    assert tts.said == []
    assert [m for m in sent if m["type"] == "narration"] == []


def test_without_a_headset_the_mac_and_phones_speak_as_before() -> None:
    engine, tts, sent = _engine(floor=False)
    asyncio.run(engine._emit(_event()))
    assert tts.said == ["Added retry logic."]
    assert [m["tts_text"] for m in sent if m["type"] == "narration"] == ["Added retry logic."]


@pytest.mark.asyncio
async def test_no_registered_announcement_over_the_headset(client: httpx.AsyncClient) -> None:
    state.claim_speech_floor(True)
    with patch.object(bridge_server.mac_tts, "speak") as speak:
        await register_test_session(client, session_id="floor-1")
    speak.assert_not_called()
    state.claim_speech_floor(False)
    with patch.object(bridge_server.mac_tts, "speak") as speak:
        await register_test_session(client, session_id="floor-2")
    speak.assert_called_once()


@pytest.mark.asyncio
async def test_no_fallback_speech_over_the_headset(client: httpx.AsyncClient) -> None:
    await register_test_session(client, session_id="floor-3")
    with patch.object(state, "narration", None):
        state.claim_speech_floor(True)
        with patch.object(bridge_server.mac_tts, "speak") as speak:
            await client.post("/api/events", headers=auth_headers(), json={
                "event": "stop", "session_id": "floor-3", "project": "myproject",
                "summary": "Did a thing.", "stop_reason": "end_turn"})
        speak.assert_not_called()
        state.claim_speech_floor(False)
        with patch.object(bridge_server.mac_tts, "speak") as speak:
            await client.post("/api/events", headers=auth_headers(), json={
                "event": "stop", "session_id": "floor-3", "project": "myproject",
                "summary": "Did a thing.", "stop_reason": "end_turn"})
        speak.assert_called_once()
