"""Tests for ``_handle_form_action`` — multi-question form keystroke synth.

iOS sends a SEMANTIC action ("select option 2", "toggle", "advance",
"submit", "note: …"); the bridge re-detects the live form, gates on
(form_id + active-question title), and synthesizes arrow-deltas + Enter
against the freshly-observed cursor. These tests mock the fresh capture
and ``create_subprocess_exec`` to assert the EXACT tmux key sequences,
never touching a real tmux pane.

The keystroke model is the one verified in the Phase 0 spike against a
live Claude Code v2.1.148 widget:
  - single-select: arrow to option + Enter (auto-advances)
  - multi-select:  arrow to option + Enter (toggles); "Next" row advances
  - free text:     arrow to "Type something" + type inline + Enter
  - review:        arrow to "Submit answers" + Enter
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import ws_handler
from bridge.choice_detector import FormRow, MultiQuestionForm, QuestionTab
from bridge.session_manager import Session, SessionManager


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bridge.session_manager._PERSIST_PATH", str(tmp_path / "sessions.json")
    )
    mgr = SessionManager()
    monkeypatch.setattr(ws_handler, "sessions", mgr)
    sid = "test-session"
    mgr._sessions[sid] = Session(
        session_id=sid, project="proj", project_dir="/tmp",
        status="active", tmux_target="proj:0.0",
    )
    return mgr, sid


def _ws() -> MagicMock:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    return ws


def _single_form(focused: int = 0) -> MultiQuestionForm:
    return MultiQuestionForm(
        questions=[
            QuestionTab("Color", False),
            QuestionTab("Features", False),
            QuestionTab("Deploy", False),
        ],
        title="Which accent color?",
        select_mode="single",
        rows=[
            FormRow("Red", "choice", None, focused == 0),
            FormRow("Green", "choice", None, focused == 1),
            FormRow("Blue", "choice", None, focused == 2),
            FormRow("Type something.", "free_text", None, focused == 3),
            FormRow("Chat about this", "meta", None, focused == 4),
        ],
    )


def _multi_form(focused: int = 4) -> MultiQuestionForm:
    return MultiQuestionForm(
        questions=[
            QuestionTab("Color", True),
            QuestionTab("Features", True),
            QuestionTab("Deploy", False),
        ],
        title="Which features should we include?",
        select_mode="multi",
        rows=[
            FormRow("Auth", "choice", True, focused == 0),
            FormRow("Search", "choice", True, focused == 1),
            FormRow("Export", "choice", False, focused == 2),
            FormRow("Notifications", "choice", False, focused == 3),
            FormRow("Type something", "free_text", False, focused == 4),
            FormRow("Next", "next", None, focused == 5),
            FormRow("Chat about this", "meta", None, focused == 6),
        ],
    )


def _review_form(focused: int = 0) -> MultiQuestionForm:
    return MultiQuestionForm(
        questions=[
            QuestionTab("Color", True),
            QuestionTab("Features", True),
            QuestionTab("Deploy", True),
        ],
        title="Review your answers",
        select_mode="review",
        rows=[
            FormRow("Submit answers", "submit", None, focused == 0),
            FormRow("Cancel", "cancel", None, focused == 1),
        ],
    )


async def _run(data: dict, fresh_form) -> tuple[list, MagicMock]:
    """Invoke the handler with a mocked fresh capture + subprocess.
    Returns (key_sequences, ws). Each key sequence is ``(literal, [keys])``."""
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=None)
    calls: list[list[str]] = []

    async def fake_exec(*argv, **kw):
        calls.append(list(argv))
        return proc

    async def fake_capture(_target):
        return fresh_form

    ws = _ws()
    with patch.object(ws_handler, "_capture_and_detect_form", new=fake_capture), \
         patch.object(ws_handler.asyncio, "create_subprocess_exec", new=fake_exec):
        await ws_handler._handle_terminal_send_keys(data, ws)

    seqs: list[tuple[bool, list[str]]] = []
    for argv in calls:
        if argv[:2] != ["tmux", "send-keys"]:
            continue
        rest = argv[4:]  # after tmux send-keys -t <target>
        literal = bool(rest) and rest[0] == "-l"
        keys = rest[1:] if literal else rest
        seqs.append((literal, keys))
    return seqs, ws


def _commit(form, action: str, **extra) -> dict:
    return {
        "type": "terminal_send_keys",
        "session_id": "test-session",
        "form_action": action,
        "form_id": form.form_id,
        "title": form.title,
        **extra,
    }


# ---------------------------------------------------------------------------
# Single-select
# ---------------------------------------------------------------------------


class TestSingleSelect:

    async def test_select_moves_down_then_enter(self, setup):
        f = _single_form(focused=0)
        seqs, ws = await _run(_commit(f, "select", row_index=1), f)  # pick Green
        assert seqs == [(False, ["Down"]), (False, ["Enter"])]
        ws.send_json.assert_not_called()  # no error

    async def test_select_moves_up_when_target_above_cursor(self, setup):
        f = _single_form(focused=2)  # cursor on Blue
        seqs, _ = await _run(_commit(f, "select", row_index=0), f)  # pick Red
        assert seqs == [(False, ["Up", "Up"]), (False, ["Enter"])]

    async def test_select_same_row_just_enter(self, setup):
        f = _single_form(focused=1)
        seqs, _ = await _run(_commit(f, "select", row_index=1), f)  # already focused
        assert seqs == [(False, ["Enter"])]


# ---------------------------------------------------------------------------
# Multi-select
# ---------------------------------------------------------------------------


class TestMultiSelect:

    async def test_toggle_moves_to_box_and_enter(self, setup):
        f = _multi_form(focused=4)  # cursor on "Type something"
        seqs, _ = await _run(_commit(f, "toggle", row_index=2), f)  # toggle Export
        # delta = 2 - 4 = -2 → Up Up
        assert seqs == [(False, ["Up", "Up"]), (False, ["Enter"])]

    async def test_advance_targets_next_row(self, setup):
        f = _multi_form(focused=0)  # cursor on Auth (row 0); Next is row 5
        seqs, _ = await _run(_commit(f, "advance"), f)
        assert seqs == [(False, ["Down"] * 5), (False, ["Enter"])]


# ---------------------------------------------------------------------------
# Free-text / voice note
# ---------------------------------------------------------------------------


class TestFreeTextNote:

    async def test_note_navigates_types_and_confirms(self, setup):
        f = _single_form(focused=0)  # free_text is row 3
        seqs, _ = await _run(_commit(f, "note", text="use turbo mode"), f)
        assert seqs == [
            (False, ["Down", "Down", "Down"]),  # to "Type something."
            (True, ["use turbo mode"]),          # typed literally
            (False, ["Enter"]),                  # confirm
        ]

    async def test_empty_note_bails_with_no_keystrokes(self, setup):
        # Enter on an empty "Type something" row DECLINES the whole form
        # (verified). So an empty note must send NO keys and return an
        # error — never a bare Enter.
        f = _single_form(focused=3)  # already on free_text
        seqs, ws = await _run(_commit(f, "note", text="   "), f)
        assert seqs == []
        ws.send_json.assert_called()


# ---------------------------------------------------------------------------
# Review screen
# ---------------------------------------------------------------------------


class TestReview:

    async def test_submit_enter_when_focused(self, setup):
        f = _review_form(focused=0)
        seqs, _ = await _run(_commit(f, "submit"), f)
        assert seqs == [(False, ["Enter"])]

    async def test_cancel_navigates_to_cancel_row(self, setup):
        f = _review_form(focused=0)
        seqs, _ = await _run(_commit(f, "cancel"), f)
        assert seqs == [(False, ["Down"]), (False, ["Enter"])]


# ---------------------------------------------------------------------------
# Gating / rejection — no keystrokes, error sent
# ---------------------------------------------------------------------------


class TestGate:

    async def test_form_gone_rejects(self, setup):
        f = _single_form()
        seqs, ws = await _run(_commit(f, "select", row_index=1), None)  # fresh=None
        assert seqs == []
        ws.send_json.assert_called()  # error returned

    async def test_form_id_mismatch_rejects(self, setup):
        f = _single_form()
        data = _commit(f, "select", row_index=1)
        data["form_id"] = "different-form"
        seqs, ws = await _run(data, f)
        assert seqs == []
        ws.send_json.assert_called()

    async def test_title_mismatch_rejects(self, setup):
        # Form advanced to a different question between broadcast and commit.
        f = _single_form()
        data = _commit(f, "select", row_index=1)
        data["title"] = "Which features should we include?"
        seqs, ws = await _run(data, f)
        assert seqs == []
        ws.send_json.assert_called()

    async def test_unknown_action_rejects(self, setup):
        f = _single_form()
        seqs, ws = await _run(_commit(f, "frobnicate"), f)
        assert seqs == []
        ws.send_json.assert_called()

    async def test_row_index_out_of_range_rejects(self, setup):
        f = _single_form()
        seqs, ws = await _run(_commit(f, "select", row_index=99), f)
        assert seqs == []
        ws.send_json.assert_called()

    async def test_row_index_excludes_meta_row(self, setup):
        # The pickable list excludes "Chat about this" (meta). row_index=3
        # is "Type something" (free_text), NOT the meta row — selecting it
        # must target row 3, not the meta row 4.
        f = _single_form(focused=0)
        seqs, _ = await _run(_commit(f, "select", row_index=3), f)
        assert seqs == [(False, ["Down", "Down", "Down"]), (False, ["Enter"])]

    async def test_missing_form_id_rejects(self, setup):
        f = _single_form()
        data = _commit(f, "select", row_index=1)
        del data["form_id"]
        seqs, ws = await _run(data, f)
        assert seqs == []
        ws.send_json.assert_called()


class TestConnectReplay:
    """A client connecting AFTER a form's rising-edge broadcast must still
    learn about it — ``_connect_replay_decision_messages`` replays the
    cached decision on connect."""

    def test_replays_active_form(self, setup):
        from bridge import activity, ws_handler
        mgr, sid = setup
        form = _multi_form()
        activity._LAST_FORM_FOR_SESSION[sid] = form
        try:
            msgs = ws_handler._connect_replay_decision_messages(mgr.get_all_sessions())
        finally:
            activity._LAST_FORM_FOR_SESSION.pop(sid, None)
        qf = [m for m in msgs if m.get("type") == "question_form" and m["session_id"] == sid]
        assert len(qf) == 1
        assert qf[0]["select_mode"] == "multi"
        assert qf[0]["form_id"] == form.form_id
        assert any(o["text"] == "Auth" for o in qf[0]["options"])

    def test_no_messages_when_no_active_decisions(self, setup):
        from bridge import activity, ws_handler
        mgr, sid = setup
        activity._LAST_FORM_FOR_SESSION.pop(sid, None)
        activity._LAST_CHOICE_FOR_SESSION.pop(sid, None)
        msgs = ws_handler._connect_replay_decision_messages(mgr.get_all_sessions())
        assert [m for m in msgs if m["session_id"] == sid] == []
