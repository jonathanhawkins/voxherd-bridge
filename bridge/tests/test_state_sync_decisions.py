"""state_sync carries the authoritative live-decision session sets.

A numbered-choice menu (or multi-question form) resolved directly on the
computer is cleared on the lens by the one-shot ``choice_prompt_cancelled`` /
``question_form_cancelled`` broadcast — which a backgrounded/disconnected iOS
client never receives. So ``state_sync`` (the guaranteed first message on every
reconnect, plus every subsequent sync) carries ``active_choice_sessions`` /
``active_form_sessions``: the exact set of sessions with a live decision on
their Mac terminal. iOS prunes any choice/form card whose session is absent.

Regression for: resolved a 3-option menu on the Mac, put the glasses on, the
stale options were still showing.
"""

from __future__ import annotations

import pytest

from bridge import activity, server_state
from bridge.choice_detector import ChoicePrompt


@pytest.fixture(autouse=True)
def _clear_trackers():
    """The decision trackers are module-level dicts — isolate each test."""
    activity._LAST_CHOICE_FOR_SESSION.clear()
    activity._LAST_FORM_FOR_SESSION.clear()
    yield
    activity._LAST_CHOICE_FOR_SESSION.clear()
    activity._LAST_FORM_FOR_SESSION.clear()


def test_state_sync_lists_session_with_live_choice():
    activity._LAST_CHOICE_FOR_SESSION["sess-a"] = ChoicePrompt(
        title="Proceed?", options=["Yes", "No", "Maybe"]
    )
    msg = server_state._state_sync_msg()
    assert msg["active_choice_sessions"] == ["sess-a"]
    assert msg["active_form_sessions"] == []


def test_state_sync_omits_resolved_choice():
    # Prompt resolved on the computer → falling-edge popped the tracker →
    # empty set → iOS prunes its stale card on the next sync.
    msg = server_state._state_sync_msg()
    assert msg["active_choice_sessions"] == []
    assert msg["active_form_sessions"] == []


def test_state_sync_lists_session_with_live_form():
    # _state_sync_msg reads only the tracker KEYS, so the stored value shape
    # is irrelevant here — a sentinel stands in for a MultiQuestionForm.
    activity._LAST_FORM_FOR_SESSION["sess-form"] = object()
    msg = server_state._state_sync_msg()
    assert msg["active_form_sessions"] == ["sess-form"]
    assert msg["active_choice_sessions"] == []


def test_state_sync_tracks_multiple_live_choices():
    activity._LAST_CHOICE_FOR_SESSION["sess-a"] = ChoicePrompt(
        title="A?", options=["1", "2", "3"]
    )
    activity._LAST_CHOICE_FOR_SESSION["sess-b"] = ChoicePrompt(
        title="B?", options=["1", "2", "3"]
    )
    msg = server_state._state_sync_msg()
    assert set(msg["active_choice_sessions"]) == {"sess-a", "sess-b"}
