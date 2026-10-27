"""What the bridge owes a tmux window it resized, written down where a
restart cannot lose it.

An ``ansi`` subscriber (a headset) sizes a session's tmux window to its panel so
the assistant TUI reflows to the width it is actually being read at. That is a
destructive change to a terminal the developer has open, and the only thing
that undoes it is the ``prev_*`` values on the in-memory ``GeometryLease``.

**Which is why a restart used to strand a pane at panel width, permanently.**
Leases live in memory and are released by the websocket handler's teardown, so
a bridge that is killed -- a crash, a `kill -9`, a rebuilt ``.app`` -- drops
every one of them without putting anything back. The pane keeps the panel size
*and* the ``window-size manual`` option, so it no longer tracks the terminal
either. Then the next client subscribes, ``_acquire_geometry`` finds no lease,
reads the *current* geometry as the pane's original, and the panel size becomes
the thing release restores to. The damage outlives the bridge, the client and
every later release.

So the undo record is persisted here the moment it is created and dropped the
moment it is honoured. Anything left in the file when the bridge starts is a
window a previous run resized and never put back
(:func:`pending`), and the bridge restores it before serving anyone.

### Keyed by window, not by session
``resize-window`` acts on a window; a lease is per session. Two assistant panes
split inside one window would each take a lease and each read the other's size
as "original", so the record is keyed by the window and the **first** writer's
geometry stands -- the same rule ``_acquire_geometry`` already applies to a
handover ("a takeover keeps the previous holder's ``prev_*`` values"), extended
across restarts. ``holders`` is what keeps the record alive while a second pane
still holds a lease; without it, the first pane releasing would drop the record
and a restart during the second lease would strand the window again.

Note the asymmetry this leaves: ``_terminal_geometry`` is keyed by session and
does **not** model two leases on one window, so the in-memory restore for the
second pane still uses the first pane's size as its "original". That is
pre-existing and unchanged here. This file is about surviving a restart.

### Never raises
Geometry is a nicety and this is a side file: a missing, unreadable or corrupt
record degrades to "nothing is owed" rather than taking down a subscription or
a startup. Same posture as the hooks, for the same reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass

#: Bumped if the shape below ever changes; an unknown version reads as empty.
_VERSION = 1


@dataclass(frozen=True)
class PaneRestore:
    """A window a previous run resized, and what to put back.

    ``window_size`` is the raw ``window-size`` window option, ``""`` when the
    window was inheriting the global value. Both halves are load-bearing and
    neither substitutes for the other: a window left on ``manual`` never tracks
    the developer's terminal again, and **clearing the option does not resize
    anything on its own** -- measured against tmux 3.6a, and pinned by
    ``test_terminal_geometry.py``'s module docstring. tmux recomputes a tracking
    window's size on a client event (attach, detach, resize), not when the
    option changes, so until one of those happens the window keeps whatever
    size it was left at. The remembered dimensions are therefore the whole of
    the immediate fix, and the option is what stops the pane going wrong again
    the next time the developer resizes their terminal.
    """

    window: str
    cols: int
    rows: int
    window_size: str


def _path() -> str:
    """Where the record lives. Overridable for tests."""
    override = os.environ.get("VOXHERD_PANE_GEOMETRY_FILE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".voxherd", "pane-geometry.json")


def window_of(target: str) -> str:
    """The window a pane target belongs to: ``sess:0.1`` -> ``sess:0``.

    A target with no pane part is already a window. A ``resize-window`` on
    either spelling hits the same window, which is the point.
    """
    head, sep, _pane = target.partition(".")
    return head if sep else target


def _read() -> dict[str, dict]:
    try:
        with open(_path()) as f:
            doc = json.load(f)
    except Exception:
        return {}
    if not isinstance(doc, dict) or doc.get("version") != _VERSION:
        return {}
    windows = doc.get("windows")
    if not isinstance(windows, dict):
        return {}
    return windows


def _write(windows: dict[str, dict]) -> None:
    """Replace the record atomically, or leave the old one alone.

    Atomic because a half-written record is worse than a stale one: the stale
    one still names a window to put back, and a truncated one names nothing.
    """
    path = _path()
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".pane-geo-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"version": _VERSION, "windows": windows}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception:
        return


def record(target: str, cols: int, rows: int, window_size: str) -> None:
    """Note that the bridge has pinned *target*'s window, and what to put back.

    First writer wins on the geometry: a second pane in the same window would
    otherwise record the first pane's panel size as the original. *target* is
    added to the window's holders either way, so the record outlives the first
    pane's release.
    """
    windows = _read()
    window = window_of(target)
    entry = windows.get(window)
    if isinstance(entry, dict):
        holders = entry.get("holders")
        entry["holders"] = sorted({*(holders if isinstance(holders, list) else []), target})
    else:
        windows[window] = {
            "cols": cols,
            "rows": rows,
            "window_size": window_size,
            "holders": [target],
        }
    _write(windows)


def forget(target: str) -> None:
    """Drop *target* from its window's holders; drop the record when it is the last.

    Called once the bridge has actually restored the window, so what is left in
    the file is only ever what is still owed.
    """
    windows = _read()
    window = window_of(target)
    entry = windows.get(window)
    if not isinstance(entry, dict):
        return
    holders = [h for h in entry.get("holders", []) if h != target]
    if holders:
        entry["holders"] = holders
    else:
        del windows[window]
    _write(windows)


def pending() -> list[PaneRestore]:
    """Windows a previous run resized and never put back.

    Empty after a clean shutdown, because every release calls :func:`forget`.
    """
    out: list[PaneRestore] = []
    for window, entry in sorted(_read().items()):
        if not isinstance(entry, dict):
            continue
        cols, rows = entry.get("cols"), entry.get("rows")
        # Booleans are ints in Python and a 1-column window is real damage.
        if type(cols) is not int or type(rows) is not int or cols < 1 or rows < 1:
            continue
        size = entry.get("window_size")
        out.append(
            PaneRestore(
                window=window,
                cols=cols,
                rows=rows,
                window_size=size if isinstance(size, str) else "",
            )
        )
    return out


def lookup(target: str) -> PaneRestore | None:
    """What is owed on *target*'s window, or ``None`` when nothing is.

    **This outranks the in-memory lease, and release must prefer it.** A
    lease's ``prev_*`` is whatever the window measured at the moment that lease
    was taken -- which is the *stranded* size whenever a previous run was
    killed still holding the window. Restoring to it is then a no-op, and the
    ``forget`` that follows a restore throws away the only copy of the real
    size. Observed 2026-10-25: four windows pinned at a 3-panel preset's 67x35
    with an empty record and nothing left anywhere to put them back.

    The record does not have that problem, because :func:`record` is
    first-writer-wins *across restarts*: the size written when the window was
    first pinned survives every later lease that measured the damage instead of
    the original.
    """
    window = window_of(target)
    for owed in pending():
        if owed.window == window:
            return owed
    return None


def clear() -> None:
    """Forget everything. Used once the pending set has been restored."""
    try:
        os.unlink(_path())
    except Exception:
        return
