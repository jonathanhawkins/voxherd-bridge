"""The persisted undo record for a resized tmux window.

The bug this exists for: a lease lives in memory and is released by the
websocket teardown, so a bridge that is *killed* puts nothing back. The
developer's pane keeps the panel width and ``window-size manual``, and the next
client to subscribe reads that panel width as the pane's original size and
makes it permanent.

These are the pure half -- no tmux, no sockets. ``test_terminal_geometry.py``
has the end-to-end restart walk against a real tmux server.
"""

from __future__ import annotations

import json
import os

import pytest

from bridge import pane_geometry


@pytest.fixture(autouse=True)
def record_file(monkeypatch, tmp_path):
    path = tmp_path / "pane-geometry.json"
    monkeypatch.setenv("VOXHERD_PANE_GEOMETRY_FILE", str(path))
    return path


# --- where it goes ---------------------------------------------------------


def test_the_default_path_is_beside_the_auth_token(monkeypatch):
    """Not in a temp dir: the record has to survive a reboot, like the token."""
    monkeypatch.delenv("VOXHERD_PANE_GEOMETRY_FILE", raising=False)
    assert pane_geometry._path().endswith(os.path.join(".voxherd", "pane-geometry.json"))


def test_the_record_is_not_world_readable(record_file):
    pane_geometry.record("sess:0.0", 120, 40, "")
    assert oct(os.stat(record_file).st_mode)[-3:] == "600"


# --- a window, not a pane --------------------------------------------------


def test_a_pane_target_is_recorded_against_its_window():
    """``resize-window`` acts on the window, so the record has to key on it."""
    assert pane_geometry.window_of("vh-demo:0.1") == "vh-demo:0"
    assert pane_geometry.window_of("vh-demo:0") == "vh-demo:0"
    # A session name with a dot in it would be a window target already.
    assert pane_geometry.window_of("vh:2") == "vh:2"


def test_two_panes_in_one_window_keep_the_first_original():
    """The second pane would otherwise record the first pane's panel size.

    Pane A leases, so the window is already 78x27 when pane B leases and reads
    "the current size". If B's reading won, a restart would restore the window
    to the panel width -- which is the very damage this file prevents.
    """
    pane_geometry.record("vh:0.0", 120, 40, "")
    pane_geometry.record("vh:0.1", 78, 27, "manual")

    owed = pane_geometry.pending()
    assert len(owed) == 1, "one window, one record"
    assert (owed[0].cols, owed[0].rows) == (120, 40)
    assert owed[0].window_size == ""


def test_the_record_outlives_the_first_pane_to_let_go():
    """Otherwise a restart during the second lease strands the window again."""
    pane_geometry.record("vh:0.0", 120, 40, "")
    pane_geometry.record("vh:0.1", 120, 40, "")

    pane_geometry.forget("vh:0.0")
    assert [p.window for p in pane_geometry.pending()] == ["vh:0"], "still owed to 0.1"

    pane_geometry.forget("vh:0.1")
    assert pane_geometry.pending() == []


def test_forgetting_a_window_nobody_recorded_is_a_noop():
    pane_geometry.forget("vh:9.0")
    assert pane_geometry.pending() == []


# --- the lifecycle ---------------------------------------------------------


def test_what_is_owed_is_what_was_recorded_and_not_released():
    pane_geometry.record("a:0.0", 120, 40, "")
    pane_geometry.record("b:1.0", 200, 50, "manual")

    owed = {p.window: p for p in pane_geometry.pending()}
    assert set(owed) == {"a:0", "b:1"}
    assert (owed["b:1"].cols, owed["b:1"].rows, owed["b:1"].window_size) == (200, 50, "manual")

    pane_geometry.forget("a:0.0")
    assert [p.window for p in pane_geometry.pending()] == ["b:1"]


def test_a_clean_shutdown_leaves_nothing_owed():
    """Which is what makes a non-empty record mean "the last run was killed"."""
    pane_geometry.record("a:0.0", 120, 40, "")
    pane_geometry.forget("a:0.0")
    assert pane_geometry.pending() == []


def test_clear_drops_everything(record_file):
    pane_geometry.record("a:0.0", 120, 40, "")
    pane_geometry.clear()
    assert pane_geometry.pending() == []
    assert not record_file.exists()


def test_clearing_nothing_is_not_an_error():
    pane_geometry.clear()
    pane_geometry.clear()


# --- it must never be the thing that breaks --------------------------------


def test_a_corrupt_record_reads_as_nothing_owed(record_file):
    """Degrade to "nothing is owed", never to an exception at startup."""
    record_file.write_text("{ this is not json")
    assert pane_geometry.pending() == []
    # And it recovers: a later record replaces the garbage.
    pane_geometry.record("a:0.0", 120, 40, "")
    assert [p.window for p in pane_geometry.pending()] == ["a:0"]


def test_a_record_from_a_future_version_is_ignored(record_file):
    record_file.write_text(json.dumps({"version": 99, "windows": {"a:0": {"cols": 1, "rows": 1}}}))
    assert pane_geometry.pending() == []


def test_a_missing_record_reads_as_nothing_owed():
    assert pane_geometry.pending() == []


def test_junk_shapes_are_skipped_rather_than_crashing(record_file):
    record_file.write_text(json.dumps({
        "version": 1,
        "windows": {
            "good:0": {"cols": 120, "rows": 40, "window_size": ""},
            "listy:0": ["nope"],
            "stringy:0": {"cols": "120", "rows": "40"},
            "missing:0": {"window_size": "manual"},
            "nullsize:0": {"cols": 80, "rows": 24, "window_size": None},
        },
    }))
    owed = {p.window: p for p in pane_geometry.pending()}
    assert set(owed) == {"good:0", "nullsize:0"}
    # A non-string option falls back to "unset", which is the right default:
    # it is what a window the developer never pinned themselves reads, so the
    # pane goes back to tracking their terminal. It does not resize anything by
    # itself -- the remembered dimensions do that.
    assert owed["nullsize:0"].window_size == ""


def test_a_zero_or_negative_size_is_not_restored(record_file):
    """These land in tmux argv, and a 1-column window is real damage."""
    record_file.write_text(json.dumps({
        "version": 1,
        "windows": {
            "zero:0": {"cols": 0, "rows": 40},
            "neg:0": {"cols": 120, "rows": -1},
            "bool:0": {"cols": True, "rows": True},
        },
    }))
    assert pane_geometry.pending() == []


def test_an_unwritable_location_does_not_raise(monkeypatch, tmp_path):
    """A side file must not be able to fail a subscription."""
    monkeypatch.setenv("VOXHERD_PANE_GEOMETRY_FILE", str(tmp_path / "nope" / "x" / "r.json"))
    monkeypatch.setattr(pane_geometry.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError))
    pane_geometry.record("a:0.0", 120, 40, "")
    assert pane_geometry.pending() == []


def test_a_failed_write_leaves_no_temp_files_behind(monkeypatch, record_file):
    """A crashed write must not litter the directory it shares with the token."""
    monkeypatch.setattr(pane_geometry.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError))
    pane_geometry.record("a:0.0", 120, 40, "")
    assert [p.name for p in record_file.parent.iterdir() if p.name.startswith(".pane-geo-")] == []
