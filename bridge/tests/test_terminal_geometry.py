"""Tests for terminal geometry ownership.

An ``ansi`` subscriber (a headset) sizes the tmux window to its panel so
the assistant TUI reflows to the width it is actually being read at. Nobody
else does: the phone mirror and the glasses lens render whatever size they are
handed, because resizing the pane out from under the terminal the user has
open on their Mac would be rude.

The tmux semantics here were established empirically against tmux 3.6a, and
two of them are counter-intuitive enough to be worth pinning:

1. ``resize-window -x -y`` sets the ``window-size`` **window option** to
   ``manual`` as a side effect. You do not have to set it yourself -- but you
   do have to put it back, or the window stops tracking the terminal the user
   actually has open, forever.
2. Clearing that option does **not** resize anything on its own. Release has
   to restore the dimensions explicitly, and it has to do that *before*
   restoring the option, because the restoring resize would otherwise set the
   option to ``manual`` all over again.

The tmux-backed tests below are the regression net for both.
"""

from __future__ import annotations

import asyncio
import dataclasses
import contextlib
import os
import shutil
import subprocess
import uuid

import pytest

from bridge import pane_geometry
from bridge.validation import _validate_tmux_window_target
from bridge.server_state import _terminal_geometry
from bridge.session_manager import Session
from bridge.ws_handler import (
    _acquire_geometry,
    _coerce_terminal_geometry,
    _read_window_geometry,
    _release_geometry,
    restore_stranded_panes,
    sessions,

    _GEOMETRY_HANDOVER_COOLDOWN,)

# --- pure validation -------------------------------------------------------


def test_valid_geometry():
    assert _coerce_terminal_geometry({"cols": 80, "rows": 48}) == (80, 48)


def test_missing_geometry_is_none():
    assert _coerce_terminal_geometry({}) is None
    assert _coerce_terminal_geometry({"cols": 80}) is None
    assert _coerce_terminal_geometry({"rows": 48}) is None


def test_booleans_are_rejected():
    """`bool` subclasses `int`; {"cols": true} must not become a 1-column pane."""
    assert _coerce_terminal_geometry({"cols": True, "rows": 48}) is None
    assert _coerce_terminal_geometry({"cols": 80, "rows": False}) is None


def test_non_integers_are_rejected():
    assert _coerce_terminal_geometry({"cols": "80", "rows": "48"}) is None
    assert _coerce_terminal_geometry({"cols": 80.5, "rows": 48}) is None
    assert _coerce_terminal_geometry({"cols": None, "rows": None}) is None


def test_out_of_bounds_is_rejected():
    """These land in tmux argv, and a huge grid is real memory on the dev box."""
    assert _coerce_terminal_geometry({"cols": 0, "rows": 48}) is None
    assert _coerce_terminal_geometry({"cols": -80, "rows": 48}) is None
    assert _coerce_terminal_geometry({"cols": 100_000, "rows": 48}) is None
    assert _coerce_terminal_geometry({"cols": 80, "rows": 100_000}) is None


def test_bounds_are_inclusive():
    assert _coerce_terminal_geometry({"cols": 20, "rows": 5}) == (20, 5)
    assert _coerce_terminal_geometry({"cols": 500, "rows": 200}) == (500, 200)


# --- tmux-backed behaviour -------------------------------------------------

pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or os.environ.get("CI") == "true",
    reason="needs a real tmux binary",
)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=10, check=False
    )


def _size(target: str) -> str:
    return _tmux("display-message", "-t", target, "-p",
                 "#{window_width}x#{window_height}").stdout.strip()


def _window_size_opt(target: str) -> str:
    return _tmux("show-options", "-w", "-t", target, "-v", "window-size").stdout.strip()


@pytest.fixture
def tmux_session():
    """A throwaway detached tmux session, registered with the SessionManager."""
    name = f"vh-geotest-{uuid.uuid4().hex[:8]}"
    _tmux("new-session", "-d", "-s", name, "-x", "120", "-y", "40")
    target = f"{name}:0.0"
    sid = f"geo-{uuid.uuid4().hex[:8]}"
    sessions._sessions[sid] = Session(
        session_id=sid, project="geotest", project_dir="/tmp/geotest",
        status="active", tmux_target=target, assistant="claude",
    )
    try:
        yield sid, target
    finally:
        _terminal_geometry.pop(sid, None)
        sessions._sessions.pop(sid, None)
        _tmux("kill-session", "-t", name)


@contextlib.contextmanager
def _live(ws):
    """Register *ws* as a connection the bridge is serving, for the block.

    ``_acquire_geometry`` treats a lease as binding only while its holder is
    live, so a test about contention has to say which clients are.
    """
    import bridge.server_state as _state

    _state.ios_connections.append(ws)
    try:
        yield ws
    finally:
        try:
            _state.ios_connections.remove(ws)
        except ValueError:
            pass


@pytestmark_tmux
def test_acquire_resizes_and_release_restores(tmux_session):
    sid, target = tmux_session
    ws = object()
    assert _size(target) == "120x40"
    assert _window_size_opt(target) == ""

    assert asyncio.run(_acquire_geometry(sid, target, ws, 80, 48)) is True
    assert _size(target) == "80x48"
    # tmux flips this itself -- we never set it.
    assert _window_size_opt(target) == "manual"

    asyncio.run(_release_geometry(sid, ws))
    assert _size(target) == "120x40"
    # ...and it has to come back, or the window never tracks a client again.
    assert _window_size_opt(target) == ""
    assert sid not in _terminal_geometry


@pytestmark_tmux
def test_release_restores_the_real_size_after_a_run_was_killed_holding_it(tmux_session):
    """The chain that pinned four of the developer's windows at 67x35.

    Measured 2026-10-25. Each step is ordinary; it is the sequence that does
    the damage, and the last step is the one that makes it permanent:

      1. a lease is taken -- the window is resized, and the record notes its
         real size;
      2. the bridge (or the client) is killed still holding it. No release
         runs, so the window keeps the panel size. The record survives, which
         is exactly what it is for;
      3. the client comes back and subscribes again. `_acquire_geometry` finds
         no lease and measures the window, so this lease's `prev_*` is the
         *stranded* size. The record is untouched, because `record` is
         first-writer-wins;
      4. a clean release. Restoring from the lease puts back the stranded size
         -- a no-op -- and `forget` then drops the record.

    After (4) the pane is stuck at panel width with nothing anywhere that
    knows better: the startup sweep has nothing to replay, and every later
    lease measures the damage as the original. The record has to outrank the
    lease, which is what `pane_geometry.lookup` is for.
    """
    sid, target = tmux_session
    assert _size(target) == "120x40"

    # 1. a lease, honestly taken.
    assert asyncio.run(_acquire_geometry(sid, target, object(), 67, 35)) is True
    assert _size(target) == "67x35"
    assert [(r.cols, r.rows) for r in pane_geometry.pending()] == [(120, 40)]

    # 2. killed holding it: the lease vanishes, the record does not.
    _terminal_geometry.pop(sid, None)
    assert _size(target) == "67x35", "nothing was put back, which is the premise"
    assert [(r.cols, r.rows) for r in pane_geometry.pending()] == [(120, 40)]

    # 3. the client returns. This lease measures the damage as the original.
    second = object()
    assert asyncio.run(_acquire_geometry(sid, target, second, 67, 35)) is True
    assert _terminal_geometry[sid].prev_cols == 67, "the premise: the lease is wrong"
    assert [(r.cols, r.rows) for r in pane_geometry.pending()] == [(120, 40)], \
        "and the record is still right"

    # 4. a clean release has to use the record, not the lease.
    asyncio.run(_release_geometry(sid, second))

    assert _size(target) == "120x40", "the developer's real terminal size"
    assert _window_size_opt(target) == "", "and tracking again, not pinned manual"
    assert pane_geometry.pending() == [], "the debt is paid, so the record goes"


@pytestmark_tmux
def test_an_ordinary_release_still_uses_what_it_recorded(tmux_session):
    """The record and the lease agree in the common case; this pins that.

    Preferring the record must not change the path that was already right --
    and `lookup` returning None (a release with no record, which is what every
    pre-existing unit test of this function sets up) must still fall back to
    the lease rather than restoring nothing.
    """
    sid, target = tmux_session
    ws = object()
    assert asyncio.run(_acquire_geometry(sid, target, ws, 80, 48)) is True
    assert _size(target) == "80x48"

    # The record removed: release has only the lease to go on.
    pane_geometry.clear()
    assert pane_geometry.lookup(target) is None

    asyncio.run(_release_geometry(sid, ws))
    assert _size(target) == "120x40"
    assert _window_size_opt(target) == ""


@pytestmark_tmux
def test_a_live_second_client_cannot_steal_the_lease(tmux_session):
    """Two clients trading resizes would reflow the TUI on every poll.

    The holder has to be registered as a live connection, because that is what
    liveness means now: a lease is only binding while the bridge is still
    serving the client that took it.
    """
    sid, target = tmux_session
    first, second = object(), object()

    with _live(first):
        assert asyncio.run(_acquire_geometry(sid, target, first, 80, 48)) is True
        assert asyncio.run(_acquire_geometry(sid, target, second, 200, 60)) is False
        assert _size(target) == "80x48"

        # And the non-owner cannot release it either.
        asyncio.run(_release_geometry(sid, second))
        assert _size(target) == "80x48"
        assert sid in _terminal_geometry

        asyncio.run(_release_geometry(sid, first))
        assert _size(target) == "120x40"


@pytestmark_tmux
def test_a_lease_whose_holder_is_gone_is_reclaimed(tmux_session):
    """The bug this file exists to prevent recurring.

    A client killed without closing its socket leaves the server blocked in
    ``receive`` until TCP gives up, so its ``finally`` never runs and the lease
    stays on the books. Every later subscriber was then refused *in silence*:
    the pane kept the dead client's width while the new panel drew narrower,
    so content arrived 156 columns wide for a panel rendering 78, cut or
    wrapped, with nothing anywhere to explain it.
    """
    sid, target = tmux_session
    gone, fresh = object(), object()

    with _live(gone):
        assert asyncio.run(_acquire_geometry(sid, target, gone, 156, 38)) is True
    # `gone` is now off the connection list -- exactly what a force-stopped
    # client looks like from here.
    assert asyncio.run(_acquire_geometry(sid, target, fresh, 78, 38)) is True
    assert _size(target) == "78x38"

    # And the restore must return the pane to its *real* original size, not to
    # the width the dead client had left it at.
    with _live(fresh):
        asyncio.run(_release_geometry(sid, fresh))
    assert _size(target) == "120x40"
    assert sid not in _terminal_geometry


@pytestmark_tmux
def test_the_same_client_resizing_is_not_a_steal(tmux_session):
    """A second panel joining narrows the first: same client, new size."""
    sid, target = tmux_session
    ws = object()

    with _live(ws):
        assert asyncio.run(_acquire_geometry(sid, target, ws, 156, 38)) is True
        assert asyncio.run(_acquire_geometry(sid, target, ws, 78, 38)) is True
        assert _size(target) == "78x38"
        asyncio.run(_release_geometry(sid, ws))
    assert _size(target) == "120x40"


@pytestmark_tmux
def test_owner_can_resize_without_losing_the_original(tmux_session):
    """A panel resize must not make the *new* size the thing we restore to."""
    sid, target = tmux_session
    ws = object()

    asyncio.run(_acquire_geometry(sid, target, ws, 80, 48))
    asyncio.run(_acquire_geometry(sid, target, ws, 100, 30))
    assert _size(target) == "100x30"

    asyncio.run(_release_geometry(sid, ws))
    assert _size(target) == "120x40"


@pytestmark_tmux
def test_read_window_geometry(tmux_session):
    _sid, target = tmux_session
    assert asyncio.run(_read_window_geometry(target)) == (120, 40, "")


def test_read_window_geometry_on_a_dead_pane_is_none():
    assert asyncio.run(_read_window_geometry("vh-no-such-session-xyz:0.0")) is None


def test_release_without_a_lease_is_a_noop():
    asyncio.run(_release_geometry("never-leased", object()))


@pytestmark_tmux
def test_a_half_open_holder_yields_once_its_claim_is_old(tmux_session):
    """The real shape of the bug, which the liveness check alone did not fix.

    A force-stopped client leaves a *half-open* socket: the server stays
    blocked in ``receive``, the connection is still in ``ios_connections``, and
    ``client_state`` still reads CONNECTED. Every signal the bridge has says
    the dead client is fine, so the first attempt at this fix -- refuse unless
    the holder looks gone -- changed nothing on the device. Handover is gated
    on the lease's *age* for exactly that reason.
    """
    sid, target = tmux_session
    zombie, fresh = object(), object()

    # `zombie` stays on the connection list for the whole test: that is what
    # makes this different from the holder-gone case.
    with _live(zombie):
        assert asyncio.run(_acquire_geometry(sid, target, zombie, 156, 38)) is True

        # Still within the cooldown, a second client must not barge in.
        assert asyncio.run(_acquire_geometry(sid, target, fresh, 78, 38)) is False
        assert _size(target) == "156x38"

        # Age the claim past the cooldown, as a relaunch would.
        lease = _terminal_geometry[sid]
        _terminal_geometry[sid] = dataclasses.replace(
            lease, acquired_at=lease.acquired_at - _GEOMETRY_HANDOVER_COOLDOWN - 1
        )

        assert asyncio.run(_acquire_geometry(sid, target, fresh, 78, 38)) is True
        assert _size(target) == "78x38"

    # The restore has to reach the pane's real original size, not the width the
    # zombie left behind -- otherwise the damage outlives every client.
    with _live(fresh):
        asyncio.run(_release_geometry(sid, fresh))
    assert _size(target) == "120x40"
    assert sid not in _terminal_geometry


# --- surviving a kill ------------------------------------------------------
#
# Everything above is in-process: a lease is taken and released by the same
# bridge. These are about the bridge not being there any more.
#
# A lease lives in memory and is released by the websocket handler's teardown,
# so a bridge that is killed -- a crash, `kill -9`, a rebuilt `.app` -- drops
# every lease without putting anything back. What that left behind was not just
# a pane at panel width: `window-size` stays `manual`, so the window no longer
# tracks the terminal the developer has open, and the *next* client to
# subscribe finds no lease, reads the panel width as the pane's original size,
# and makes it the thing release restores to. Permanent, and invisible --
# nothing on screen says the pane is 27 rows because a bridge died last week.


def _kill_the_bridge(sid: str) -> None:
    """What a `kill -9` does to a lease: drops it, restores nothing.

    Deliberately *not* `_release_geometry` -- the whole point is that the
    teardown never ran.
    """
    _terminal_geometry.pop(sid, None)


@pytestmark_tmux
def test_a_killed_bridge_puts_the_pane_back_on_its_next_start(tmux_session):
    sid, target = tmux_session
    ws = object()

    assert asyncio.run(_acquire_geometry(sid, target, ws, 78, 27)) is True
    assert _size(target) == "78x27"
    _kill_the_bridge(sid)

    # The pane is stranded and the record is what knows about it.
    assert [p.window for p in pane_geometry.pending()] == [pane_geometry.window_of(target)]

    assert asyncio.run(restore_stranded_panes()) == 1
    assert _size(target) == "120x40"
    # The option matters at least as much as the numbers: left on `manual` the
    # window never tracks the developer's terminal again.
    assert _window_size_opt(target) == ""
    assert pane_geometry.pending() == [], "and it is not owed twice"


@pytestmark_tmux
def test_the_sweep_runs_before_a_new_client_can_make_it_permanent(tmux_session):
    """The whole bug, walked: kill, restart, reconnect, release.

    Without the sweep, the acquire on the restarted bridge reads 78x27 as the
    pane's original size, and the release at the end lands on 78x27 instead of
    120x40 -- the damage outliving the bridge that did it.
    """
    sid, target = tmux_session
    before, after = object(), object()

    assert asyncio.run(_acquire_geometry(sid, target, before, 78, 27)) is True
    _kill_the_bridge(sid)

    # Startup, in the order `_lifespan` does it: the sweep, then serving.
    asyncio.run(restore_stranded_panes())

    with _live(after):
        assert asyncio.run(_acquire_geometry(sid, target, after, 78, 27)) is True
        assert _size(target) == "78x27"
        asyncio.run(_release_geometry(sid, after))

    assert _size(target) == "120x40", "the pane's real size, not the panel's"
    assert _window_size_opt(target) == ""


@pytestmark_tmux
def test_a_clean_release_leaves_the_next_start_nothing_to_do(tmux_session):
    """Which is what makes a non-empty record mean "the last run was killed"."""
    sid, target = tmux_session
    ws = object()

    asyncio.run(_acquire_geometry(sid, target, ws, 78, 27))
    asyncio.run(_release_geometry(sid, ws))

    assert pane_geometry.pending() == []
    assert asyncio.run(restore_stranded_panes()) == 0


@pytestmark_tmux
def test_a_resize_by_the_owner_does_not_rewrite_what_is_owed(tmux_session):
    """Same rule as the in-memory lease: the *first* reading is the original."""
    sid, target = tmux_session
    ws = object()

    asyncio.run(_acquire_geometry(sid, target, ws, 156, 38))
    asyncio.run(_acquire_geometry(sid, target, ws, 78, 27))
    _kill_the_bridge(sid)

    assert asyncio.run(restore_stranded_panes()) == 1
    assert _size(target) == "120x40"


@pytestmark_tmux
def test_a_session_pruned_under_its_lease_is_still_owed(tmux_session):
    """Release cannot reach the pane; the record can, on the next start.

    ``server_state`` tears subscriptions down for pruned sessions, and the
    session is where the tmux target lives -- so once it is gone,
    ``_release_geometry`` has nothing to resize. The record is keyed by the
    window, not the session, so the startup sweep still puts the pane back.
    Dropping the record on that early return would be tidier and would strand
    the pane.
    """
    sid, target = tmux_session
    ws = object()

    assert asyncio.run(_acquire_geometry(sid, target, ws, 78, 27)) is True
    sessions._sessions.pop(sid)  # what pruning does

    asyncio.run(_release_geometry(sid, ws))
    assert _size(target) == "78x27", "release had no target to resize"
    assert [p.window for p in pane_geometry.pending()] == [pane_geometry.window_of(target)]

    assert asyncio.run(restore_stranded_panes()) == 1
    assert _size(target) == "120x40"
    assert _window_size_opt(target) == ""


def test_a_window_that_is_gone_is_forgotten_rather_than_retried(monkeypatch, tmp_path):
    """A killed tmux leaves a record for a pane that no longer exists.

    Nothing to put back, and keeping it would mean one failed restore on every
    start from now on.
    """
    monkeypatch.setenv("VOXHERD_PANE_GEOMETRY_FILE", str(tmp_path / "pane-geometry.json"))
    pane_geometry.record("vh-no-such-session-xyz:0.0", 120, 40, "")

    assert asyncio.run(restore_stranded_panes()) == 0
    assert pane_geometry.pending() == []


def test_nothing_owed_does_not_shell_out_at_all(monkeypatch, tmp_path):
    """Startup is the hot path everyone waits on, and the usual case is clean."""
    monkeypatch.setenv("VOXHERD_PANE_GEOMETRY_FILE", str(tmp_path / "pane-geometry.json"))
    import bridge.ws_handler as wsh

    async def _boom(*args: str):
        raise AssertionError("ran tmux with nothing to restore")

    monkeypatch.setattr(wsh, "_tmux", _boom)
    assert asyncio.run(restore_stranded_panes()) == 0


# --- the target the restore gets from disk ----------------------------------
#
# Every other tmux target in `ws_handler` is validated. This one arrives from
# `~/.voxherd/pane-geometry.json`, the weakest provenance of any of them, and
# is handed to tmux unattended at startup before anything is served.


def test_a_window_target_is_not_a_pane_target():
    """`resize-window` takes `session:0`, and the pane validator rejects that.

    Which is why there are two of them: reusing `_validate_tmux_pane_target`
    here would refuse every record ever written, and the restore would then
    silently do nothing at all -- a guard that disables the feature it guards.
    """
    assert _validate_tmux_window_target("kora-02:0") == ("kora-02:0", None)
    assert _validate_tmux_window_target("vh-demo-a:12") == ("vh-demo-a:12", None)
    # A pane target is not a window target; the record never holds one.
    assert _validate_tmux_window_target("kora-02:0.0")[0] is None


def test_a_junk_target_is_refused_rather_than_handed_to_tmux():
    for bad in ['bad;echo x:0', 'kora 02:0', 'kora-02:', ':0', 'kora-02:0|x', 'kora-02:0 --flag', 'kora-02:99999', 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:0']:
        target, err = _validate_tmux_window_target(bad)
        assert target is None, bad
        assert err, bad


def test_the_bridge_session_is_protected():
    """The bridge's own window is not something a record on disk may resize."""
    assert _validate_tmux_window_target("bridge:0")[0] is None
    assert _validate_tmux_window_target("vh-bridge:0")[0] is None


def test_nothing_and_blank_are_not_errors():
    assert _validate_tmux_window_target(None) == (None, None)
    assert _validate_tmux_window_target("   ") == (None, None)


def test_the_restore_skips_a_record_it_cannot_validate(monkeypatch, tmp_path):
    """A corrupt record must not reach tmux, and must not be retried forever."""
    monkeypatch.setenv("VOXHERD_PANE_GEOMETRY_FILE", str(tmp_path / "pane-geometry.json"))
    import bridge.ws_handler as wsh

    pane_geometry.record("bad;echo x:0", 120, 40, "")
    asked = []

    async def _spy(*args):
        asked.append(args)
        return 0, ""

    monkeypatch.setattr(wsh, "_tmux", _spy)
    assert asyncio.run(wsh.restore_stranded_panes()) == 0
    assert asked == [], "a junk target reached tmux: %s" % (asked,)
    assert pane_geometry.pending() == [], "and it is not retried on every start"
