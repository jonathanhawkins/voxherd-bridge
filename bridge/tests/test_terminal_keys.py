"""Tests for the terminal key protocol.

Two things here, both needed before anyone can type on the headset through a
Bluetooth keyboard:

1. **`key_sequence`** -- a validated list of tmux key names delivered in one
   `send-keys` call. A keyboard produces ``["C-c", "Enter"]`` far more often
   than it produces a lone keystroke, and one message beats N round trips.

2. **The return code is checked.** `tmux send-keys` against a dead pane exits
   non-zero *without raising*. The raw-keys path used to await the process and
   throw the result away, so typing into a dead pane did nothing and said
   nothing. That is the same silent-failure class that once wedged a session
   in "Sending to <project>" (see test_dispatch_dead_pane).

Every key name in `_TMUX_NAMED_KEYS` was probed against a real tmux 3.6a
binary when this landed; all 34 were accepted. If tmux ever renames one, the
tmux-backed test below is what catches it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid

import pytest

from bridge.ws_handler import (
    _MAX_KEY_SEQUENCE,
    _TMUX_NAMED_KEYS,
    _coerce_key_sequence,
    _is_valid_tmux_key,
    _send_tmux_keys,
)

# A shell-command-shaped string. Key names are argv, never shell, so this is
# not an injection risk -- it is simply not a key, and must be refused rather
# than handed to tmux to reject silently.
NOT_A_KEY = "echo hello world"


# --- key-name validation ---------------------------------------------------


@pytest.mark.parametrize(
    "key", ["Enter", "Escape", "Tab", "BSpace", "Up", "F1", "F12", "PageUp"]
)
def test_named_keys_accepted(key):
    assert _is_valid_tmux_key(key) is True


def test_named_keys_are_case_insensitive():
    assert _is_valid_tmux_key("enter") is True
    assert _is_valid_tmux_key("ENTER") is True


@pytest.mark.parametrize("key", ["C-c", "C-d", "M-x", "S-Tab", "C-M-a", "M-Enter"])
def test_modifier_forms_accepted(key):
    assert _is_valid_tmux_key(key) is True


@pytest.mark.parametrize("key", ["a", "Z", "1", "/", "-", "$"])
def test_single_printable_characters_accepted(key):
    assert _is_valid_tmux_key(key) is True


@pytest.mark.parametrize(
    "key",
    [
        NOT_A_KEY,     # a command, not a key
        "$(whoami)",   # shell-looking input
        "Entr",        # typo of a real key name
        "",
        "x" * 64,      # over the length cap
    ],
)
def test_junk_rejected(key):
    assert _is_valid_tmux_key(key) is False


def test_non_strings_rejected():
    assert _is_valid_tmux_key(None) is False
    assert _is_valid_tmux_key(7) is False
    assert _is_valid_tmux_key(["Enter"]) is False


# --- sequence coercion -----------------------------------------------------


def test_valid_sequence():
    assert _coerce_key_sequence(["C-c", "Enter"]) == ["C-c", "Enter"]


def test_sequence_must_be_a_non_empty_list():
    assert _coerce_key_sequence(None) is None
    assert _coerce_key_sequence([]) is None
    assert _coerce_key_sequence("Enter") is None
    assert _coerce_key_sequence({"0": "Enter"}) is None


def test_one_bad_member_rejects_the_whole_sequence():
    """Partial delivery is worse than none -- half a keystroke run is garbage."""
    assert _coerce_key_sequence(["Enter", NOT_A_KEY]) is None
    assert _coerce_key_sequence(["Enter", None]) is None


def test_sequence_length_is_capped():
    assert _coerce_key_sequence(["a"] * _MAX_KEY_SEQUENCE) is not None
    assert _coerce_key_sequence(["a"] * (_MAX_KEY_SEQUENCE + 1)) is None


def test_returned_sequence_is_a_copy():
    src = ["Enter"]
    out = _coerce_key_sequence(src)
    assert out is not src


# --- tmux-backed -----------------------------------------------------------

needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or os.environ.get("CI") == "true",
    reason="needs a real tmux binary",
)


@pytest.fixture
def tmux_pane():
    name = f"vh-keytest-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-x", "80", "-y", "24"],
        capture_output=True, check=False, timeout=10,
    )
    try:
        yield f"{name}:0.0"
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True, check=False, timeout=10,
        )


@needs_tmux
def test_every_named_key_is_one_tmux_accepts(tmux_pane):
    """The allowlist is only useful if tmux agrees with it."""
    rejected = []
    for name in sorted(_TMUX_NAMED_KEYS):
        ok, err = asyncio.run(_send_tmux_keys(tmux_pane, [name], literal=False))
        if not ok:
            rejected.append((name, err))
    assert rejected == []


@needs_tmux
def test_sequence_delivers_in_one_call(tmux_pane):
    ok, err = asyncio.run(_send_tmux_keys(tmux_pane, ["C-c", "Enter"], literal=False))
    assert (ok, err) == (True, "")


@needs_tmux
def test_literal_text_delivers(tmux_pane):
    ok, _err = asyncio.run(_send_tmux_keys(tmux_pane, ["ls -la"], literal=True))
    assert ok is True


def test_dead_pane_reports_failure_instead_of_silence():
    """The regression this whole helper exists for."""
    ok, err = asyncio.run(
        _send_tmux_keys("vh-no-such-session-xyz:0.0", ["Enter"], literal=False)
    )
    assert ok is False
    assert err  # a real reason, not an empty string
