"""Tests for the choice-signature gate inside ``_handle_terminal_send_keys``.

The gate is the server-side defense against a narrow race: bridge
broadcasts ``choice_prompt_cancelled`` because Claude moved on, but the
user's long-press lands on iOS before MainActor processes the dismiss
event. Without the gate the bridge would forward "1\\n" into the
no-longer-numbered prompt and pollute the conversation.

When iOS sends ``terminal_send_keys`` with a ``choice_signature`` field,
the bridge looks up the currently-tracked prompt in
``_LAST_CHOICE_FOR_SESSION`` and refuses the send if the signature has
drifted (or the entry has been popped entirely). Without the field the
gate is a no-op — existing terminal-send callers are unaffected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import ws_handler, activity
from bridge.choice_detector import ChoicePrompt
from bridge.session_manager import Session, SessionManager


@pytest.fixture
def fresh_setup(tmp_path, monkeypatch):
    """Replace the module-level ``sessions`` with a tmpdir-backed
    SessionManager, register a single session with a tmux target, and
    clear the choice tracker. Yields (mgr, session_id, tmux_target)."""
    monkeypatch.setattr(
        "bridge.session_manager._PERSIST_PATH",
        str(tmp_path / "sessions.json"),
    )
    mgr = SessionManager()
    monkeypatch.setattr(ws_handler, "sessions", mgr)
    # Reach into activity to clear the module-level choice tracker so
    # tests don't share state.
    activity._LAST_CHOICE_FOR_SESSION.clear()

    sid = "test-session"
    session = Session(
        session_id=sid,
        project="proj",
        project_dir="/tmp",
        status="active",
        tmux_target="proj:0.0",
    )
    mgr._sessions[sid] = session
    yield mgr, sid, session.tmux_target
    activity._LAST_CHOICE_FOR_SESSION.clear()


def _ws_mock() -> MagicMock:
    """Tiny WebSocket stand-in with awaitable ``send_json``."""
    ws = MagicMock()
    ws.send_json = AsyncMock()
    return ws


_UNSET = object()


async def _call_handler(data: dict, ws, fresh_prompt=_UNSET):
    """Invoke ``_handle_terminal_send_keys`` with a patched
    ``create_subprocess_exec`` so we can observe whether the bridge
    actually shelled out to tmux without touching the real tmux.

    The choice-commit path now re-detects against a fresh tmux capture
    inside the handler (closes the stale-snapshot race). Tests that
    seed ``_LAST_CHOICE_FOR_SESSION`` get the same prompt returned
    from the fake capture by default — so existing happy-path tests
    keep passing. Pass ``fresh_prompt=None`` to simulate a prompt
    that scrolled off, or ``fresh_prompt=ChoicePrompt(...)`` to
    simulate a divergent capture (e.g. cursor drift).
    """
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=None)

    # Default the fake fresh-capture to whatever the test seeded into
    # the activity-side cache. The new commit handler re-detects
    # against this rather than trusting the cache directly.
    if fresh_prompt is _UNSET:
        sid = data.get("session_id", "")
        fresh_prompt = activity._LAST_CHOICE_FOR_SESSION.get(sid)

    async def fake_capture(_target):
        return fresh_prompt

    with patch.object(
        ws_handler, "_capture_and_detect_choice", new=fake_capture
    ), patch.object(
        ws_handler.asyncio, "create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as spawn:
        await ws_handler._handle_terminal_send_keys(data, ws)
    return spawn, proc


class TestChoiceSignatureGate:

    async def test_send_allowed_when_signature_matches(self, fresh_setup):
        _, sid, _ = fresh_setup
        prompt = ChoicePrompt(title="Pick one:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "keys": "1\n",
            "literal": True,
            "choice_signature": prompt.signature,
        }, ws)

        # tmux send-keys subprocess was spawned exactly once.
        assert spawn.await_count == 1
        # No error was sent back to the client.
        ws.send_json.assert_not_awaited()

    async def test_send_refused_when_signature_mismatches(self, fresh_setup):
        _, sid, _ = fresh_setup
        # Server thinks the live prompt is "Pick one:" with [a, b, c].
        live = ChoicePrompt(title="Pick one:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = live

        # iOS sends a commit tagged with a STALE signature (e.g. user
        # was looking at an older prompt that's since been replaced).
        stale_sig = "0000000000000000"
        assert stale_sig != live.signature

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "keys": "1\n",
            "literal": True,
            "choice_signature": stale_sig,
        }, ws)

        # No tmux subprocess was spawned.
        assert spawn.await_count == 0
        # The client got an error message back.
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "no longer live" in payload["message"].lower()

    async def test_send_refused_when_no_live_choice(self, fresh_setup):
        """If iOS sends a choice-commit but the tracker has no entry
        for this session (Claude already moved on, choice_prompt_cancelled
        broadcast happened), refuse the send."""
        _, sid, _ = fresh_setup
        # Tracker is empty (set up via fixture clear).
        assert sid not in activity._LAST_CHOICE_FOR_SESSION

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "keys": "1\n",
            "literal": True,
            "choice_signature": "doesnt-matter",
        }, ws)

        assert spawn.await_count == 0
        ws.send_json.assert_awaited_once()

    async def test_send_allowed_without_signature_field(self, fresh_setup):
        """Backwards compatibility: legacy callers (phone-side terminal
        pager, voice commands) send terminal_send_keys without a
        choice_signature. Those must still go through unchanged —
        the gate is opt-in."""
        _, sid, _ = fresh_setup
        # No signature field on the request.
        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "keys": "ls\n",
            "literal": True,
        }, ws)

        assert spawn.await_count == 1
        ws.send_json.assert_not_awaited()

    async def test_empty_signature_field_is_no_op(self, fresh_setup):
        """An empty string for choice_signature should be treated the
        same as missing — don't gate the send."""
        _, sid, _ = fresh_setup
        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "keys": "echo hello\n",
            "literal": True,
            "choice_signature": "",
        }, ws)

        assert spawn.await_count == 1
        ws.send_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# Server-side keystroke synthesis (choice_index path)
# ---------------------------------------------------------------------------


class TestChoiceIndexCommitNumberMode:
    """When the live prompt is `input_mode="number"`, the bridge sends
    a `"<N+1>\\n"` literal via `tmux send-keys -l`. This mirrors the
    legacy iOS path so the on-screen prompt commit behaves the same
    whether iOS pre-built the keystroke or asked the bridge to."""

    async def test_number_mode_sends_literal_digit_plus_newline(self, fresh_setup):
        _, sid, tmux = fresh_setup
        prompt = ChoicePrompt(
            title="Pick:", options=["a", "b", "c"], input_mode="number"
        )
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 2,  # 0-based — should commit "3\n"
            "choice_signature": prompt.signature,
        }, ws)

        assert spawn.await_count == 1
        cmd = spawn.await_args.args
        # tmux send-keys -t <target> -l "3\n"
        assert cmd[:5] == ("tmux", "send-keys", "-t", tmux, "-l")
        assert cmd[5] == "3\n"
        ws.send_json.assert_not_awaited()


class TestChoiceIndexCommitTuiSelectMode:
    """When the live prompt is `input_mode="tui_select"`, bridge sends
    arrow-key named tmux keys (NO -l flag) so tmux interprets them as
    real key events: Down × delta + Enter (or Up × |delta| + Enter)."""

    async def test_tui_forward_navigation(self, fresh_setup):
        # Cursor on item 1 (focused_index=0), user picks item 3
        # → Down Down Enter.
        _, sid, tmux = fresh_setup
        prompt = ChoicePrompt(
            title="Pick:",
            options=["a", "b", "c", "d"],
            input_mode="tui_select",
            focused_index=0,
        )
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 2,
            "choice_signature": prompt.signature,
        }, ws)

        assert spawn.await_count == 1
        cmd = spawn.await_args.args
        assert cmd[:4] == ("tmux", "send-keys", "-t", tmux)
        # No -l flag
        assert "-l" not in cmd
        # Down Down Enter (3 named keys after the target)
        assert cmd[4:] == ("Down", "Down", "Enter")

    async def test_tui_backward_navigation(self, fresh_setup):
        # Cursor on item 4 (focused_index=3), user picks item 1
        # → Up Up Up Enter.
        _, sid, tmux = fresh_setup
        prompt = ChoicePrompt(
            title="Pick:",
            options=["a", "b", "c", "d"],
            input_mode="tui_select",
            focused_index=3,
        )
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 0,
            "choice_signature": prompt.signature,
        }, ws)

        assert spawn.await_count == 1
        cmd = spawn.await_args.args
        assert cmd[4:] == ("Up", "Up", "Up", "Enter")

    async def test_tui_no_navigation_needed(self, fresh_setup):
        # Cursor already on the picked item — just send Enter.
        _, sid, tmux = fresh_setup
        prompt = ChoicePrompt(
            title="Pick:",
            options=["a", "b", "c"],
            input_mode="tui_select",
            focused_index=1,
        )
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 1,
            "choice_signature": prompt.signature,
        }, ws)

        assert spawn.await_count == 1
        cmd = spawn.await_args.args
        assert cmd[4:] == ("Enter",)

    async def test_tui_focused_index_unknown_defaults_to_zero(self, fresh_setup):
        # If we never observed a cursor (focused_index=None on the
        # live prompt — e.g. detected on a transient re-render), the
        # bridge assumes focus is at row 0 and navigates forward.
        _, sid, _ = fresh_setup
        prompt = ChoicePrompt(
            title="Pick:",
            options=["a", "b", "c"],
            input_mode="tui_select",
            focused_index=None,
        )
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 2,
            "choice_signature": prompt.signature,
        }, ws)

        assert spawn.await_count == 1
        cmd = spawn.await_args.args
        # Assumed focus=0, target=2 → Down Down Enter
        assert cmd[4:] == ("Down", "Down", "Enter")


class TestChoiceIndexCommitGate:
    """The signature gate applies to the choice_index path too — same
    race protection as the legacy keys-string path."""

    async def test_refused_without_signature(self, fresh_setup):
        # A choice_index with no signature would bypass the gate.
        # Must refuse — otherwise a malformed client could commit
        # arbitrary picks.
        _, sid, _ = fresh_setup
        prompt = ChoicePrompt(title="Pick:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 1,
        }, ws)

        assert spawn.await_count == 0
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "signature" in payload["message"].lower()

    async def test_refused_when_signature_stale(self, fresh_setup):
        _, sid, _ = fresh_setup
        live = ChoicePrompt(title="Pick:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = live

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 0,
            "choice_signature": "0" * 16,  # bogus signature
        }, ws)

        assert spawn.await_count == 0
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "no longer live" in payload["message"].lower()

    async def test_refused_when_no_live_prompt(self, fresh_setup):
        # iOS held the prompt open while the bridge already cleared
        # _LAST_CHOICE_FOR_SESSION (choice_prompt_cancelled broadcast).
        # Refuse — Claude has moved on.
        _, sid, _ = fresh_setup

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 0,
            "choice_signature": "anything",
        }, ws)

        assert spawn.await_count == 0
        ws.send_json.assert_awaited_once()

    async def test_choice_index_clamped_to_options_count(self, fresh_setup):
        # iOS sent an out-of-range index. Belt-and-suspenders clamp
        # (the signature gate would already reject if option count
        # had shrunk between observation and commit).
        _, sid, tmux = fresh_setup
        prompt = ChoicePrompt(
            title="Pick:", options=["a", "b", "c"], input_mode="number"
        )
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 99,  # way past the end
            "choice_signature": prompt.signature,
        }, ws)

        # Subprocess still ran (clamped, not refused) and committed
        # the last option (index 2 → "3\n").
        assert spawn.await_count == 1
        cmd = spawn.await_args.args
        assert cmd[5] == "3\n"

    async def test_invalid_choice_index_type_refused(self, fresh_setup):
        # Type confusion defense — string instead of int.
        _, sid, _ = fresh_setup
        prompt = ChoicePrompt(title="Pick:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": "2",
            "choice_signature": prompt.signature,
        }, ws)

        assert spawn.await_count == 0
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "invalid" in payload["message"].lower()

    async def test_commit_refused_when_fresh_capture_has_no_prompt(self, fresh_setup):
        # The cached prompt is still in `_LAST_CHOICE_FOR_SESSION` (the
        # hysteresis miss-counter hasn't tripped yet) but the prompt
        # actually scrolled off — a fresh capture finds no choice on
        # screen. The handler MUST refuse rather than commit against
        # whatever's there now (e.g. an idle `❯` shell prompt where
        # Down/Enter would recall+resubmit a prior command).
        _, sid, _ = fresh_setup
        cached = ChoicePrompt(title="Pick:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = cached

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 0,
            "choice_signature": cached.signature,
        }, ws, fresh_prompt=None)

        assert spawn.await_count == 0
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "no longer live" in payload["message"].lower()

    async def test_commit_refused_when_fresh_capture_signature_diverges(self, fresh_setup):
        # Race: between iOS observation and commit, Claude moved on to a
        # different choice prompt with the same session — fresh capture
        # sees a NEW signature. Refuse, don't commit against the new
        # prompt with the old index.
        _, sid, _ = fresh_setup
        cached = ChoicePrompt(title="Pick:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = cached
        diverged = ChoicePrompt(title="Different:", options=["x", "y", "z"])

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 0,
            "choice_signature": cached.signature,
        }, ws, fresh_prompt=diverged)

        assert spawn.await_count == 0
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "no longer live" in payload["message"].lower()

    async def test_tui_commit_uses_fresh_focused_index_not_cached(self, fresh_setup):
        # Cursor drift fix: user pressed Down on the real keyboard
        # between the activity poll (focused_index=0 cached) and the
        # commit (focused_index=2 on a fresh capture). Signature is
        # unchanged (focused_index excluded from hash). The handler
        # MUST use the FRESH focused_index for delta math — using
        # the stale cache would send the wrong number of arrows.
        # Target=2, fresh focus=2 → just Enter (no arrows).
        _, sid, tmux = fresh_setup
        cached = ChoicePrompt(
            title="Pick:",
            options=["a", "b", "c", "d"],
            input_mode="tui_select",
            focused_index=0,  # stale: poll caught cursor on row 1
        )
        activity._LAST_CHOICE_FOR_SESSION[sid] = cached
        # Fresh capture: user just pressed Down twice — cursor is on row 3.
        fresh = ChoicePrompt(
            title="Pick:",
            options=["a", "b", "c", "d"],
            input_mode="tui_select",
            focused_index=2,
        )
        assert cached.signature == fresh.signature  # focused_index excluded

        ws = _ws_mock()
        spawn, _ = await _call_handler({
            "session_id": sid,
            "choice_index": 2,
            "choice_signature": cached.signature,
        }, ws, fresh_prompt=fresh)

        # delta = 2 - 2 = 0 → Enter only. The stale cached focus would
        # have produced Down Down Enter (wrong row).
        assert spawn.await_count == 1
        cmd = spawn.await_args.args
        assert cmd[4:] == ("Enter",), \
            f"expected fresh-focused-index delta=0 (Enter only), got {cmd[4:]}"

    async def test_bool_choice_index_refused(self, fresh_setup):
        # Critical: Python's ``bool`` is a subclass of ``int``, so
        # ``isinstance(True, int)`` is True. Without an explicit
        # bool reject, ``{"choice_index": true}`` would commit
        # option 2 (True + 1 == 2). Both True and False must be
        # refused.
        _, sid, _ = fresh_setup
        prompt = ChoicePrompt(title="Pick:", options=["a", "b", "c"])
        activity._LAST_CHOICE_FOR_SESSION[sid] = prompt

        for bogus in (True, False):
            ws = _ws_mock()
            spawn, _ = await _call_handler({
                "session_id": sid,
                "choice_index": bogus,
                "choice_signature": prompt.signature,
            }, ws)
            assert spawn.await_count == 0, f"bool {bogus} should refuse"
            ws.send_json.assert_awaited_once()
            payload = ws.send_json.await_args.args[0]
            assert payload["type"] == "error"
            assert "invalid" in payload["message"].lower()
