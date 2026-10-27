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

import pytest

from bridge.session_manager import SessionManager


_TEST_PROJECT_DIR = os.path.join(os.path.expanduser("~"), ".voxherd-test-projects", "session-mgr")
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


# ---------------------------------------------------------------------------
# Teardown: removing a session forgets everything keyed to its id.
#
# ``activity._forget_session_state`` was written to replace four divergent
# copies of the same list of pops. The CALL to it then became the thing
# repeated, and four of the eight ``remove_session`` sites did not have it, so a
# session removed via ``DELETE /api/sessions/{id}``, the project cleanup in
# routes, the startup prune or ``_handle_dead_pane`` left its state behind.
#
# A leaked ``_LAST_CHOICE_FOR_SESSION`` entry is not inert: it is what the
# choice-commit gate compares an incoming answer against, and it is what
# ``state_sync``'s ``active_choice_sessions`` is built from -- so clients keep
# rendering a decision card for a session that no longer exists.
# ---------------------------------------------------------------------------


def _all_books() -> tuple[dict, ...]:
    """THE book list, imported -- not a copy of it.

    This used to restate the nine dicts, so the property
    ``activity.session_books`` exists for ("a new per-session dict has one place
    to be registered") was asserted nowhere: a tenth dict added to activity.py
    and left out of the teardown passed every test here.
    """
    from bridge.activity import session_books

    return session_books()


@pytest.fixture
def books():
    """Seed sentinels into the live per-session dicts, and always restore.

    These are module-level dicts in bridge/activity.py and bridge/server_state.py
    -- real process state, not fixtures. Without the finally, a failing assert
    left "sentinel" behind for every later test in the session. The blast radius
    is currently zero (``test_state_sync_decisions.py`` has its own autouse
    clear), but "currently zero" is not a property to rely on.
    """
    seeded: list[str] = []

    def seed(sid: str) -> str:
        for book in _all_books():
            book[sid] = "sentinel"
        seeded.append(sid)
        return sid

    try:
        yield seed
    finally:
        for sid in seeded:
            for book in _all_books():
                book.pop(sid, None)


def _books_holding(sid: str) -> list[int]:
    return [i for i, book in enumerate(_all_books()) if sid in book]


class TestRemovalForgetsSessionState:
    def test_removing_a_session_clears_every_book(self, books):
        mgr = _make_session_manager()
        with patch.object(mgr, "_save", return_value=None):
            session, _ = mgr.register_session(
                session_id="sid-teardown", project="proj",
                project_dir=_TEST_PROJECT_DIR, tmux_target="proj:0.0",
            )
        assert session is not None, "project_dir was refused"
        books("sid-teardown")
        assert _books_holding("sid-teardown"), "fixture did not seed"

        with patch.object(mgr, "_save", return_value=None):
            assert mgr.remove_session("sid-teardown") is True
        assert _books_holding("sid-teardown") == []

    def test_it_clears_even_when_the_session_was_already_gone(self, books):
        """Nothing should hold state keyed to an id that names no session.

        The books outlive the Session: a racing coroutine can delete it between
        a caller's check and its removal, so a later ``remove_session``
        returning False must still clean up.
        """
        mgr = _make_session_manager()
        books("sid-never-registered")

        assert mgr.remove_session("sid-never-registered") is False
        assert _books_holding("sid-never-registered") == []

    def test_it_leaves_other_sessions_alone(self, books):
        mgr = _make_session_manager()
        books("sid-a")
        books("sid-b")
        with patch.object(mgr, "_save", return_value=None):
            mgr.remove_session("sid-a")
        assert _books_holding("sid-a") == []
        assert len(_books_holding("sid-b")) == len(_all_books())

    async def test_pruning_a_dead_session_clears_it_too(self, books, monkeypatch):
        """``prune_dead`` does not call ``remove_session``; it drops directly."""
        mgr = _make_session_manager()
        with patch.object(mgr, "_save", return_value=None):
            session, _ = mgr.register_session(
                session_id="sid-pruned", project="proj",
                project_dir=_TEST_PROJECT_DIR, tmux_target="gone:0.0",
            )
        assert session is not None, "project_dir was refused"
        books("sid-pruned")

        async def no_pane(_target):
            return False

        monkeypatch.setattr(mgr, "_tmux_pane_exists", no_pane)
        with patch.object(mgr, "_save", return_value=None):
            removed = await mgr.prune_dead()

        assert "sid-pruned" in [sid for sid, _ in removed]
        assert _books_holding("sid-pruned") == []

    def test_the_tmux_dedup_eviction_clears_it_too(self, books):
        """The path that leaked, and the commonest way a session id changes.

        ``register_session`` evicts whatever already held the pane. That used to
        be a bare ``self._sessions.pop(sid)``, so all nine books kept the dead
        id -- permanently, because the activity loop only visits REGISTERED
        sessions and never revisits an unregistered one. ``_LAST_CHOICE_FOR_SESSION``
        is what ``state_sync``'s ``active_choice_sessions`` is built from and
        what clients prune stale decision cards against, so the lens kept a
        decision card for a session that no longer existed.

        Fires whenever Claude Code restarts or compacts in the same pane.
        """
        mgr = _make_session_manager()
        with patch.object(mgr, "_save", return_value=None):
            first, _ = mgr.register_session(
                session_id="sid-old", project="proj",
                project_dir=_TEST_PROJECT_DIR, tmux_target="samepane:0.0",
            )
            assert first is not None
            books("sid-old")
            second, evicted = mgr.register_session(
                session_id="sid-new", project="proj",
                project_dir=_TEST_PROJECT_DIR, tmux_target="samepane:0.0",
            )

        assert second is not None
        assert evicted == ["sid-old"], "the pane's previous session was not evicted"
        assert "sid-old" not in mgr.get_all_sessions()
        assert _books_holding("sid-old") == [], "the evicted session's books leaked"

    def test_the_evicted_session_is_still_handed_back_as_a_predecessor(self, books):
        """Clearing its books must not cost the model inheritance.

        ``register_session`` reads the evicted session's ``model`` so a pane
        that re-registers without one keeps what it was running.
        """
        mgr = _make_session_manager()
        with patch.object(mgr, "_save", return_value=None):
            mgr.register_session(
                session_id="sid-old", project="proj", project_dir=_TEST_PROJECT_DIR,
                tmux_target="pane2:0.0", model="claude-opus-5-5",
            )
            new, _ = mgr.register_session(
                session_id="sid-new", project="proj", project_dir=_TEST_PROJECT_DIR,
                tmux_target="pane2:0.0",
            )
        assert new is not None
        assert new.model == "claude-opus-5-5", "model inheritance was lost"

    def test_an_evicted_sessions_stream_reader_is_cancelled(self, books):
        """It leaked a coroutine and a subprocess pipe before ``_drop``."""
        cancelled = []

        class FakeTask:
            def cancel(self):
                cancelled.append(True)

        mgr = _make_session_manager()
        with patch.object(mgr, "_save", return_value=None):
            old, _ = mgr.register_session(
                session_id="sid-old", project="proj", project_dir=_TEST_PROJECT_DIR,
                tmux_target="pane3:0.0",
            )
            assert old is not None
            old._stream_json_task = FakeTask()
            mgr.register_session(
                session_id="sid-new", project="proj", project_dir=_TEST_PROJECT_DIR,
                tmux_target="pane3:0.0",
            )
        assert cancelled == [True]


class TestTheBookListIsOneList:
    def test_the_teardown_iterates_the_shared_list(self):
        """A tenth dict registered in ``session_books`` must be forgotten.

        Proves the single-source property rather than the nine current names:
        the test adds a book and checks teardown reaches it.
        """
        from bridge import activity

        extra: dict = {"sid-extra": "sentinel"}
        real = activity.session_books

        def with_extra():
            return (*real(), extra)

        activity.session_books = with_extra
        try:
            activity._forget_session_state("sid-extra")
        finally:
            activity.session_books = real
        assert extra == {}, "_forget_session_state does not use session_books()"
