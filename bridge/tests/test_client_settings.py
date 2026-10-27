"""Client-directed display settings, and why the bridge holds them.

A voice command is heard by whatever is listening -- a phone, the Mac -- but
"go light" is *about the glasses*, which are a different device. So the setting
lives on the bridge and is pushed out, rather than the device that heard the
words trying to act on something it is not.

The other half is convergence: a client that was disconnected when the theme
changed must not come back on the wrong one and stay there. ``state_sync``
carries the current values for exactly that reason.
"""

import json

import pytest

from bridge import server_state
from bridge.mac_voice_loop import parse_intent


@pytest.fixture(autouse=True)
def _restore_defaults():
    original = dict(server_state.client_settings)
    yield
    server_state.client_settings.clear()
    server_state.client_settings.update(original)


# --- the store ----------------------------------------------------------


def test_a_known_setting_is_accepted():
    assert server_state.set_client_setting("theme", "light") is True
    assert server_state.client_settings["theme"] == "light"


def test_an_unknown_value_is_rejected_and_changes_nothing():
    # These are broadcast verbatim to every client; accepting an arbitrary
    # string here would put an arbitrary string on four devices.
    server_state.set_client_setting("theme", "dark")
    assert server_state.set_client_setting("theme", "chartreuse") is False
    assert server_state.client_settings["theme"] == "dark"


def test_an_unknown_setting_name_is_rejected():
    assert server_state.set_client_setting("wallpaper", "light") is False
    assert "wallpaper" not in server_state.client_settings


@pytest.mark.parametrize("value", ["light", "dark"])
def test_both_themes_are_allowed(value):
    assert server_state.set_client_setting("theme", value) is True


# --- convergence --------------------------------------------------------


def test_state_sync_carries_the_current_settings():
    # The rule that makes this survive a reconnect. Without it the broadcast
    # is fire-and-forget and a client that was away never learns.
    server_state.set_client_setting("theme", "light")
    assert server_state._state_sync_msg()["client_settings"]["theme"] == "light"


def test_state_sync_settings_are_a_copy_not_the_live_dict():
    # A caller mutating the payload must not silently rewrite the bridge's
    # state -- these payloads get handed around and serialised.
    msg = server_state._state_sync_msg()
    msg["client_settings"]["theme"] = "tampered"
    assert server_state.client_settings["theme"] != "tampered"


def test_there_is_always_a_theme_so_a_client_never_sees_absent():
    # An older client can ignore the key, but a newer one should never have to
    # cope with it missing on a bridge that has simply never been told.
    assert "theme" in server_state._state_sync_msg()["client_settings"]


# --- the voice phrasing --------------------------------------------------


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("go light", "light"),
        ("go dark", "dark"),
        ("light mode", "light"),
        ("dark mode", "dark"),
        ("light theme", "light"),
        ("switch to light", "light"),
        ("use dark", "dark"),
        ("LIGHT", "light"),
        ("  go   dark  ", "dark"),
    ],
)
def test_display_phrasings_parse(phrase, expected):
    # Phrased loosely on purpose: this is said while squinting at a washed-out
    # panel in daylight, not typed.
    intent = parse_intent(phrase, ["homeschool", "quant"], None, None)
    assert intent.action == "display"
    assert intent.message == expected


def test_display_does_not_shadow_project_commands():
    known = ["homeschool", "quant", "lighthouse"]
    assert parse_intent("switch to homeschool", known, None, None).action == "switch"
    assert parse_intent("tell quant to run tests", known, None, None).action == "tell"
    assert parse_intent("status", known, None, None).action == "status"
    # A project whose name merely *contains* a mode word is still a project.
    assert parse_intent("switch to lighthouse", known, None, None).action == "switch"


def test_the_message_type_is_on_the_inbound_allowlist():
    """The guard that silently ate this once.

    ``_KNOWN_WS_TYPES`` drops unrecognised types *before* the dispatch chain —
    deliberately, so a client-supplied string cannot mint a stats key. A new
    handler that is not also allowlisted is therefore unreachable, and fails by
    doing nothing at all: no dispatch, no error, no reply. Adding a branch is
    only half of adding a message.
    """
    from bridge.ws_handler import _KNOWN_WS_TYPES

    assert "set_client_setting" in _KNOWN_WS_TYPES


def test_every_dispatched_type_is_allowlisted():
    """Catches the next one too, not just this one."""
    import inspect
    import re

    from bridge import ws_handler

    source = inspect.getsource(ws_handler.ios_websocket)
    dispatched = set(re.findall(r'msg_type == "([a-z_]+)"', source))
    assert dispatched, "the dispatch chain moved; this test needs updating"
    missing = dispatched - set(ws_handler._KNOWN_WS_TYPES)
    assert not missing, f"handled but unreachable, not on the allowlist: {sorted(missing)}"


# --- Deck slot presets ---------------------------------------------------


@pytest.mark.parametrize("value", ["auto", "focus", "pair", "war-room", "survey"])
def test_every_deck_preset_is_accepted(value):
    assert server_state.set_client_setting("deck", value) is True


def test_an_invented_deck_preset_is_rejected():
    # Broadcast verbatim to every client; a value nobody models rearranges
    # nothing and looks like the command was ignored.
    server_state.set_client_setting("deck", "pair")
    assert server_state.set_client_setting("deck", "warzone") is False
    assert server_state.client_settings["deck"] == "pair"


def test_the_deck_defaults_to_auto_not_a_fixed_layout():
    # auto lets the client follow the session count: one session fills the arc,
    # a second splits it. Pinning `pair` by default would leave a lone panel at
    # half width with the other half of the field of view empty.
    #
    # Asserted against the *defaults*, not the live dict: settings persist
    # across restarts now, so the live value is whatever this machine last
    # chose. An earlier version read the live dict and started failing the
    # moment the feature it was testing began working.
    assert server_state._CLIENT_SETTING_DEFAULTS["deck"] == "auto"


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("war room", "war-room"),
        ("war-room", "war-room"),
        ("focus", "focus"),
        ("pair", "pair"),
        ("survey", "survey"),
        ("auto", "auto"),
        ("go to focus", "focus"),
        ("show me the survey", "survey"),
        ("show the war room", "war-room"),
        ("switch to pair", "pair"),
        ("use survey", "survey"),
        ("focus view", "focus"),
        ("  WAR ROOM  ", "war-room"),
    ],
)
def test_deck_phrasings_parse(phrase, expected):
    intent = parse_intent(phrase, ["homeschool", "quant"], None, None)
    assert intent.action == "deck"
    assert intent.message == expected


def test_deck_presets_do_not_shadow_project_commands():
    # "focus" is a plausible project name, and a project called focus-group
    # must still be switchable.
    known = ["homeschool", "focus-group", "survey-tool"]
    assert parse_intent("switch to homeschool", known, None, None).action == "switch"
    assert parse_intent("switch to focus-group", known, None, None).action == "switch"
    assert parse_intent("tell survey-tool to run", known, None, None).action == "tell"
    # A bare preset word is still the layout, which is the intended precedence.
    assert parse_intent("survey", known, None, None).action == "deck"


def test_the_allowed_deck_values_match_what_the_client_models():
    # The bridge and the Android client have to agree on the vocabulary, or a
    # command the bridge accepts is silently dropped on arrival.
    assert server_state.ALLOWED_CLIENT_SETTINGS["deck"] == frozenset(
        {"auto", "focus", "pair", "war-room", "survey"}
    )


# --- surviving a restart -------------------------------------------------


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point the store at a scratch file so tests never touch the real one."""
    path = tmp_path / "client_settings.json"
    monkeypatch.setattr(server_state, "_CLIENT_SETTINGS_FILE", str(path))
    return path


def test_a_setting_is_written_to_disk(settings_file):
    """The bug this exists for was invisible and looked like a crash.

    Settings lived only in memory, so rebuilding the macOS bundle reset the
    theme to dark. ``state_sync`` is authoritative, so it pushed dark to the
    glasses — and in daylight a dark panel on an additive display is an
    unreadable grey rectangle. It was reported as "the same bug, I just see the
    gray screen", and nothing in any log said the theme had moved.
    """
    server_state.set_client_setting("theme", "light")
    assert json.loads(settings_file.read_text())["theme"] == "light"


def test_settings_are_restored_on_startup(settings_file):
    settings_file.write_text(json.dumps({"theme": "light", "deck": "war-room"}))
    restored = server_state._load_client_settings()
    assert restored["theme"] == "light"
    assert restored["deck"] == "war-room"


def test_a_missing_file_yields_the_defaults(settings_file):
    # First run. Must not raise: the bridge has to start.
    restored = server_state._load_client_settings()
    assert restored == server_state._CLIENT_SETTING_DEFAULTS


def test_a_corrupt_file_degrades_to_defaults_rather_than_failing(settings_file):
    settings_file.write_text("{not json at all")
    assert server_state._load_client_settings() == server_state._CLIENT_SETTING_DEFAULTS


def test_a_hand_edited_file_cannot_smuggle_an_arbitrary_value(settings_file):
    # These are broadcast verbatim to every client, so the file is validated on
    # the way *in* as well as on the way out.
    settings_file.write_text(json.dumps({"theme": "chartreuse", "deck": "war-room"}))
    restored = server_state._load_client_settings()
    assert restored["theme"] == server_state._CLIENT_SETTING_DEFAULTS["theme"]
    assert restored["deck"] == "war-room"


def test_an_unknown_key_in_the_file_is_ignored(settings_file):
    settings_file.write_text(json.dumps({"wallpaper": "blue", "theme": "light"}))
    restored = server_state._load_client_settings()
    assert "wallpaper" not in restored
    assert restored["theme"] == "light"


def test_saving_never_raises_even_when_the_path_is_unwritable(monkeypatch, tmp_path):
    # A failed save must not break the command the user just gave.
    monkeypatch.setattr(
        server_state, "_CLIENT_SETTINGS_FILE", str(tmp_path / "nope" / "x" / "s.json")
    )
    monkeypatch.setattr(server_state.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert server_state.set_client_setting("theme", "light") is True
