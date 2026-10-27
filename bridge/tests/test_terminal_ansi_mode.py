"""Tests for the ``ansi`` terminal render mode.

Background: the terminal mirror was built for small screens -- the phone
pager and the 22-row glasses lens -- so it strips Claude Code's static
chrome (status footer, tips, task widget, decorative dividers) before the
snapshot goes over the wire. On those surfaces that chrome is a third of
the row budget spent on something the user can't act on.

A headset panel inverts that: ~48 rows per panel and the
whole point is that the TUI looks like the TUI. So ``terminal_subscribe``
grew a ``mode``: ``filtered`` (default, unchanged behaviour for every
existing client) and ``ansi`` (verbatim pane).

The invariant these tests pin down: **SGR escapes survive both paths.**
capture-pane already runs with ``-e`` and nothing in the filter chain
strips escapes -- ``_strip_ansi`` only ever builds a plain-text comparison
key inside the predicates. A regression that started stripping escapes
would silently flatten colour on every client, so it gets its own test.
"""

from __future__ import annotations

from bridge.ws_handler import (
    _DEFAULT_TERMINAL_MODE,
    _TERMINAL_MODES,
    _apply_chrome_filters,
    _coerce_terminal_mode,
)

# A real Claude Code tip row -- the headline case the filtered path strips.
TIP_ROW = "Tip: Use /config to change your default permission mode (including Plan Mode)"
# SGR-bearing conversation rows: bold then reset, and a 256-colour run.
SGR_ROW = "\x1b[1mExpected \x1b[32m'2026-W39'\x1b[0m"
SGR_ROW_256 = "\x1b[38;5;208mFAIL\x1b[0m week.spec.ts:118"
# A passing-test row. `✓` is in _TASK_CHECKBOX_CHARS; it used to be treated
# as a TodoWrite checkbox context-free and dropped -- see the test below.
PASS_ROW = "\x1b[1;32m✓ 41 passed\x1b[0m"
FAIL_ROW = "\x1b[1;31m✗ 2 failed\x1b[0m"


# --- mode coercion ---------------------------------------------------------

def test_default_mode_is_filtered():
    """Every client that predates the headset sends no mode at all."""
    assert _coerce_terminal_mode(None) == "filtered"
    assert _DEFAULT_TERMINAL_MODE == "filtered"


def test_known_modes():
    assert _coerce_terminal_mode("ansi") == "ansi"
    assert _coerce_terminal_mode("filtered") == "filtered"
    assert _TERMINAL_MODES == {"filtered", "ansi"}


def test_mode_is_case_and_whitespace_tolerant():
    assert _coerce_terminal_mode("  ANSI ") == "ansi"
    assert _coerce_terminal_mode("Ansi") == "ansi"


def test_unknown_mode_degrades_to_filtered_rather_than_erroring():
    """A client newer than the bridge must get a working terminal, not a dead one."""
    assert _coerce_terminal_mode("cell-grid") == "filtered"
    assert _coerce_terminal_mode("") == "filtered"
    assert _coerce_terminal_mode(17) == "filtered"
    assert _coerce_terminal_mode({"mode": "ansi"}) == "filtered"


# --- the behavioural difference -------------------------------------------

def test_filtered_strips_chrome():
    lines = ["● Claude: done.", TIP_ROW]
    assert _apply_chrome_filters(lines) == ["● Claude: done."]


def test_ansi_mode_keeps_what_filtered_strips():
    """The whole reason the mode exists: the headset gets the pane verbatim."""
    lines = ["● Claude: done.", TIP_ROW]
    filtered = _apply_chrome_filters(lines)
    assert TIP_ROW not in filtered
    # `ansi` bypasses the chain entirely -- the poll loop passes `lines` through.
    assert TIP_ROW in lines


def test_filtered_path_keeps_test_runner_output_symmetric():
    """Regression: the filtered path used to swallow passes but keep failures.

    `✓` is in `_TASK_CHECKBOX_CHARS` and `✗` is not, so when a bare check row
    was treated as a TodoWrite row context-free, a pane reading

        ✓ 41 passed
        ✗ 2 failed

    reached the phone and the lens as just "✗ 2 failed" -- which reads as if
    nothing passed at all. `_strip_task_summary` now requires widget context
    for bare check rows; see `test_assistant_footer_strip` for the full
    matrix. `ansi` mode was never affected either way.
    """
    assert _apply_chrome_filters([PASS_ROW, FAIL_ROW]) == [PASS_ROW, FAIL_ROW]


# --- the invariant that must hold on both paths ---------------------------

def test_sgr_escapes_survive_the_filter_chain():
    """_strip_ansi is a comparison helper, NOT an output transform.

    If this ever fails, colour has silently died on the phone and the lens
    too -- not just on the headset.
    """
    out = _apply_chrome_filters([SGR_ROW, SGR_ROW_256])
    assert out == [SGR_ROW, SGR_ROW_256]
    assert "\x1b[" in "".join(out)


def test_chrome_detection_sees_through_sgr():
    """A tip row wrapped in escapes is still a tip row."""
    assert _apply_chrome_filters([f"\x1b[2m{TIP_ROW}\x1b[0m"]) == []


def test_filters_are_pure():
    """The chain must not mutate the caller's list -- the poll loop reuses it."""
    lines = ["● Claude: done.", TIP_ROW]
    before = list(lines)
    _apply_chrome_filters(lines)
    assert lines == before


def test_empty_input():
    assert _apply_chrome_filters([]) == []
