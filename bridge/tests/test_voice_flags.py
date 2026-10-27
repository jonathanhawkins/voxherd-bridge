"""The CLI's voice flags must land where the server actually reads them.

`--wake-word` was accepted, printed as "Wake word on", and never started the
listener. Nothing errored, because nothing went wrong: `bridge_server` does
`from bridge.server_state import wake_word_enabled`, which binds a *copy* of
the bool. `cli.py` assigned `bridge_server.wake_word_enabled = True`, rebinding
only that copy, while `_init_voice_loop` reads `_state.wake_word_enabled` and
went on seeing the default `False`.

`mac_stt.enabled` was never affected, which is what made this so hard to see
from the outside: `mac_stt` is an *object*, so the import shares the instance
and mutating an attribute carries. Only plain values alias this way, so
listen-after-TTS worked while the wake word silently did not.

Reported as "I turned it on and said Hey Claude, war room, and nothing".
"""

import argparse

import pytest

import bridge.server_state as server_state
from bridge import cli


@pytest.fixture(autouse=True)
def _restore_flags():
    """These are process-global, so a test that sets them must put them back."""
    before = (
        server_state.server_port,
        server_state.wake_word_enabled,
        server_state.listen_after_enabled,
        server_state.mac_stt.enabled,
        server_state.mac_stt.timeout,
    )
    yield
    (
        server_state.server_port,
        server_state.wake_word_enabled,
        server_state.listen_after_enabled,
        server_state.mac_stt.enabled,
        server_state.mac_stt.timeout,
    ) = before


def _args(**overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        port=7777, listen=False, listen_timeout=8, wake_word=False, no_listen_after=False
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def test_wake_word_reaches_the_module_that_starts_the_listener():
    # `bridge_server._init_voice_loop` reads exactly this attribute to decide
    # whether to call `start_wake_word()`. Asserting on `bridge_server`'s own
    # copy would have passed throughout the bug.
    cli._apply_runtime_flags(_args(listen=True, wake_word=True))
    assert server_state.wake_word_enabled is True


def test_listen_enables_stt_on_the_shared_object():
    cli._apply_runtime_flags(_args(listen=True, listen_timeout=12))
    assert server_state.mac_stt.enabled is True
    assert server_state.mac_stt.timeout == 12


def test_no_listen_after_is_honoured():
    # Same aliasing trap, opposite polarity: the default is True, so a stale
    # binding hides this one by agreeing with it whenever the user wants it on.
    cli._apply_runtime_flags(_args(listen=True, no_listen_after=True))
    assert server_state.listen_after_enabled is False


def test_listen_after_follows_listen():
    cli._apply_runtime_flags(_args(listen=True))
    assert server_state.listen_after_enabled is True


def test_port_reaches_the_shared_module():
    cli._apply_runtime_flags(_args(port=9123))
    assert server_state.server_port == 9123


def test_flags_default_off():
    cli._apply_runtime_flags(_args())
    assert server_state.wake_word_enabled is False
    assert server_state.mac_stt.enabled is False
