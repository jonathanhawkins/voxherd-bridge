"""Regression tests for `SessionManager.update_status` activity_type lifecycle.

The bug fixed 2026-05-19: auto-idle from `activity._activity_poll_loop`
called `update_status(sid, "idle")` without an `activity_type` kwarg.
Because of the `if activity_type is not None` guard, `session.activity_type`
kept its previous value (e.g. `"testing"`). The transition broadcast
hardcoded `"sleeping"`, but the next `state_sync` serialized
`session.to_dict()` which surfaced the stale `"testing"` — so iOS saw
`status=idle` but `activity_type=testing` and rendered the session as
"still testing" long after the work had finished.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from bridge.session_manager import SessionManager


_TEST_PROJECT_DIR = os.path.join(os.path.expanduser("~"), ".voxherd", "test-projects", "session-mgr")
os.makedirs(_TEST_PROJECT_DIR, exist_ok=True)


def _make_session_manager() -> SessionManager:
    """Return a fresh SessionManager with disk I/O stubbed out."""
    mgr = SessionManager()
    # SessionManager._load() runs in __init__ and may have hydrated state;
    # clear it for an isolated test.
    mgr._sessions.clear()
    return mgr


def _register(mgr: SessionManager, session_id: str = "s-1", project: str = "p"):
    with patch.object(mgr, "_save", return_value=None):
        session, _ = mgr.register_session(
            session_id=session_id,
            project=project,
            project_dir=_TEST_PROJECT_DIR,
        )
    return session


def test_idle_transition_resets_activity_type_to_sleeping() -> None:
    """When auto-idle calls `update_status(sid, 'idle')` without an
    activity_type kwarg, the manager must reset the field to "sleeping"
    so a subsequent state_sync doesn't surface a stale label."""
    mgr = _make_session_manager()
    session = _register(mgr)

    # Simulate: the session was running tests.
    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "active", activity_type="testing")
    assert session.activity_type == "testing", "Sanity: 'testing' label applied"

    # Activity poller auto-idles the session — note no activity_type kwarg,
    # exactly matching `bridge/activity.py` line 509.
    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "idle")

    assert session.status == "idle"
    assert session.activity_type == "sleeping", (
        "After idling, activity_type must be reset to match the 'sleeping' "
        "value that activity._activity_poll_loop hardcodes in its "
        "activity_update broadcast. Otherwise state_sync surfaces the stale "
        "prior label (e.g. 'testing') and iOS keeps rendering 'still testing'."
    )


def test_idle_transition_clears_activity_snippet() -> None:
    """Companion check: snippet and activity_type are both cleared on idle.
    Locks in that they're treated consistently — if one is reset, both are."""
    mgr = _make_session_manager()
    session = _register(mgr)

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "active", activity_type="building")
        session.activity_snippet = "Compiling foo.swift…"

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "idle")

    assert session.activity_snippet == "", "Snippet must clear on idle"
    assert session.activity_type == "sleeping", "Type must clear on idle"


def test_to_dict_after_idle_reports_sleeping() -> None:
    """End-to-end check: the serialized snapshot (what state_sync sends to
    iOS) must show activity_type=sleeping after an idle transition.
    Without this, iOS's session row keeps the old label."""
    mgr = _make_session_manager()
    session = _register(mgr)

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "active", activity_type="testing")
        mgr.update_status(session.session_id, "idle")

    serialized = session.to_dict()
    assert serialized["status"] == "idle"
    assert serialized["activity_type"] == "sleeping", (
        "state_sync wire payload must agree with the activity_update broadcast — "
        "both should report 'sleeping' for an idle session"
    )


def test_explicit_activity_type_on_idle_is_honored() -> None:
    """The Stop hook calls `update_status(sid, 'idle', activity_type='completed')`
    (or 'errored'/'stopped' depending on stop_reason — see routes.py around
    line 149). Those values carry semantic info about how the turn ended
    and must survive the idle transition. Only the auto-idle path (which
    passes no activity_type kwarg) should fall back to 'sleeping'."""
    mgr = _make_session_manager()
    session = _register(mgr)

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "active", activity_type="testing")

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "idle", activity_type="errored")

    assert session.activity_type == "errored", (
        "Explicit activity_type from the Stop-hook path must survive — "
        "only auto-idle (no activity_type kwarg) falls back to 'sleeping'"
    )


def test_explicit_completed_on_idle_is_honored() -> None:
    """The most common Stop-hook case: stop_reason='end_turn' →
    activity_type='completed'. This is what `test_send_stop_event` and
    `test_full_session_lifecycle` in test_bridge.py rely on."""
    mgr = _make_session_manager()
    session = _register(mgr)

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "active", activity_type="writing")

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "idle", activity_type="completed")

    assert session.activity_type == "completed"


def test_active_to_active_preserves_activity_type_when_omitted() -> None:
    """Sanity: the `is not None` guard still works for non-idle transitions
    — calling update_status without an activity_type kwarg on an active
    session shouldn't wipe the label."""
    mgr = _make_session_manager()
    session = _register(mgr)

    with patch.object(mgr, "_save", return_value=None):
        mgr.update_status(session.session_id, "active", activity_type="writing")
        mgr.update_status(session.session_id, "active")  # no activity_type

    assert session.status == "active"
    assert session.activity_type == "writing", (
        "Non-idle transitions without an activity_type kwarg must preserve "
        "the existing label — only the idle branch should reset it"
    )


def test_idle_transition_resets_each_known_activity_type() -> None:
    """Parametric coverage: every activity_type label the system can land on
    must be reset by an idle transition. Locks in the policy 'idle clears
    all live work labels' across the full vocabulary."""
    labels = ["working", "thinking", "writing", "searching",
              "running", "building", "testing", "completed"]
    for label in labels:
        mgr = _make_session_manager()
        session = _register(mgr, session_id=f"s-{label}", project="p")
        with patch.object(mgr, "_save", return_value=None):
            mgr.update_status(session.session_id, "active", activity_type=label)
            mgr.update_status(session.session_id, "idle")
        assert session.activity_type == "sleeping", (
            f"Idle transition must reset activity_type from {label!r} to 'sleeping'"
        )
