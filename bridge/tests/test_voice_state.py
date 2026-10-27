"""The microphone has to be visible to clients that do not have one.

The headset and the glasses use the *Mac's* microphone, often across the room, so
from the headset there is no difference between "nothing is listening",
"listening and hearing nothing", and "heard you but that was not the wake
phrase". Three separate faults shipped at once and every one of them presented
as silence; the user's question after a day of it was, reasonably, "do we have
any UX to show that the mic is on and working?"

`heard` carries the transcript even when it was not a wake phrase on purpose.
Seeing your own words come back proves the microphone, the permission and the
recogniser are all fine and moves the question to the phrasing — which is the
one thing the user can act on.
"""

import asyncio

import pytest

import bridge.server_state as server_state
from bridge.mac_voice_loop import MacVoiceLoop


@pytest.fixture(autouse=True)
def _reset_voice_state():
    before = dict(server_state.voice_state)
    yield
    server_state.voice_state.clear()
    server_state.voice_state.update(before)


def _loop(on_state=None) -> MacVoiceLoop:
    return MacVoiceLoop(
        tts=object(), stt=object(), sessions=object(),
        dispatch=lambda *a: asyncio.sleep(0),
        log=lambda *a: None,
        on_state=on_state,
    )


def test_state_sync_carries_the_mic_state():
    # The authority. A client connecting after the mic started has missed every
    # broadcast and would otherwise render a dead microphone beside a live one.
    server_state.set_voice_state(listening=True, heard="hey claude war room", wake=True)
    msg = server_state._state_sync_msg()
    assert msg["voice_state"]["listening"] is True
    assert msg["voice_state"]["heard"] == "hey claude war room"


def test_set_voice_state_returns_a_broadcastable_message():
    msg = server_state.set_voice_state(listening=True)
    assert msg["type"] == "voice_state"
    assert msg["listening"] is True
    assert msg["at"] is not None, "clients need to know how stale this is"


def test_starting_the_wake_word_announces_that_it_is_listening():
    seen: list[dict] = []
    loop = _loop(on_state=seen.append)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        pass
    asyncio.run(_start_and_stop(loop))
    assert seen, "starting the listener must say so; silence is the bug"
    assert seen[0]["listening"] is True
    assert seen[-1]["listening"] is False


async def _start_and_stop(loop: MacVoiceLoop) -> None:
    loop.start_wake_word()
    loop.stop_wake_word()


def test_a_broadcast_failure_never_stops_the_microphone():
    # Losing the indicator is a bad outcome shown honestly. Losing the
    # microphone because the indicator raised would be absurd.
    def boom(_fields):
        raise RuntimeError("client gone")

    loop = _loop(on_state=boom)
    loop._emit(listening=True)  # must not raise


def test_no_callback_is_a_valid_configuration():
    # Every existing construction of this loop passes no `on_state`.
    _loop(on_state=None)._emit(listening=True)
