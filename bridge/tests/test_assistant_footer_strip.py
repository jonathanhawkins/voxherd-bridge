"""Tests for the Claude Code status-footer stripper in ws_handler.

Claude Code paints a 2-3 row status footer at the bottom of every pane:
the branch/model/token line ("🌿 main 🤖 Opus 4.7 ...") and the
bypass-permissions hint ("►► bypass permissions on (shift+tab to cycle)
· ← for agents"). On the glasses lens that's a big chunk of the 22-row
budget gone to static chrome. The stripper runs on the bridge so the
filtered output goes over the wire smaller too.

Conservatism is the key invariant: we ONLY strip from the bottom, and
ONLY rows that unambiguously match the footer shape. A conversation
line that happens to contain "bypass permissions" or starts with "🌿"
mid-scrollback must be preserved.
"""

from __future__ import annotations

from bridge.ws_handler import (
    _count_top_pixel_art_rows,
    _is_feedback_prompt_row,
    _is_task_summary_row,
    _looks_like_assistant_footer_row,
    _strip_assistant_chrome_anywhere,
    _strip_assistant_status_footer,
    _strip_decorative_divider_rows,
    _strip_feedback_prompt,
    _strip_long_decorative_runs,
    _strip_task_summary,
    _strip_tip_rows,
    _is_tip_row,
    _strip_context_indicator_rows,
    _is_context_indicator_row,
    _strip_working_status_rows,
    _is_working_status_row,
)


def test_strips_bypass_permissions_hint_at_bottom() -> None:
    lines = [
        "$ ls",
        "file.txt",
        "$ ",
        "►► bypass permissions on (shift+tab to cycle) · ← for agents",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == ["$ ls", "file.txt", "$ "]


def test_strips_branch_model_status_line() -> None:
    lines = [
        "Doing work",
        "  🌿 main 🤖 Opus 4.7 (1M context) ⚡high ✏️ +0 -0 $0.00",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == ["Doing work"]


def test_strips_multiple_consecutive_footer_rows() -> None:
    """Both the status line AND the hint line — the real-world case."""
    lines = [
        "your prompt: hello",
        "›",
        "  🌿 main 🤖 Opus 4.7 (1M context) ⚡high ✏️ +16 -1 $0.00 0/1000k ○○○○",
        "  ►► bypass permissions on (shift+tab to cycle) · ← for agents",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == ["your prompt: hello", "›"], (
        f"Expected both footer rows stripped, got: {out!r}"
    )


def test_strips_footer_with_interleaved_blank_rows() -> None:
    """Real pane captures sometimes have a blank row between the input
    prompt and the status line. The stripper must walk past those too."""
    lines = [
        "user typed something",
        "›",
        "",
        "  🌿 main 🤖 Opus 4.7",
        "",
        "  ►► bypass permissions",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == ["user typed something", "›"]


def test_preserves_interior_match_of_footer_text() -> None:
    """A conversation line containing 'bypass permissions' in the middle
    of scrollback (not at the bottom) must NOT be stripped."""
    lines = [
        "user: I want to bypass permissions for this command",
        "claude: I understand, here's how to do that",
        "more output",
        "›",
        "  🌿 main 🤖 Opus 4.7",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == [
        "user: I want to bypass permissions for this command",
        "claude: I understand, here's how to do that",
        "more output",
        "›",
    ], (
        f"Interior footer-marker match must NOT be stripped — only trailing. Got: {out!r}"
    )


def test_does_not_strip_when_no_footer_present() -> None:
    lines = [
        "$ git status",
        "On branch main",
        "$ ",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == lines, "Without a footer pattern at the bottom, nothing should change."


def test_does_not_strip_real_content_at_bottom() -> None:
    """Defense against an over-eager regex: a normal line like 'fixed
    bug in foo.py' at the bottom must NOT be misidentified as footer."""
    lines = [
        "starting work",
        "fixed bug in foo.py",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == lines


def test_empty_input_returns_empty() -> None:
    assert _strip_assistant_status_footer([]) == []


def test_all_footer_returns_empty() -> None:
    """Edge case: the entire snapshot is just the footer (rare but possible
    immediately after a refresh before content rendered)."""
    lines = [
        "  🌿 main 🤖 Opus 4.7",
        "  ►► bypass permissions",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == []


def test_recognizer_is_conservative() -> None:
    """The row-shape recognizer must NOT match plain content."""
    assert not _looks_like_assistant_footer_row("$ ls -la")
    assert not _looks_like_assistant_footer_row("the result was 42")
    assert not _looks_like_assistant_footer_row("")
    assert not _looks_like_assistant_footer_row("   ")
    # But it SHOULD match the real footer shapes:
    assert _looks_like_assistant_footer_row("  🌿 main 🤖 Opus 4.7")
    assert _looks_like_assistant_footer_row("►► bypass permissions on (shift+tab to cycle)")
    assert _looks_like_assistant_footer_row("⏵⏵ plan mode (shift+tab)")


# ---------------------------------------------------------------------------
# Real-world capture: Claude Code wraps the chrome rows in ANSI color
# codes, so the pattern match has to happen AFTER stripping escapes.
# These tests pin that behavior — without them the user saw the footer
# leak through to the lens because the line started with `\x1b[32m`
# rather than `🌿`.
# ---------------------------------------------------------------------------


def test_recognizes_status_row_wrapped_in_ansi_color_codes() -> None:
    """A real tmux capture wraps `🌿 main…` in ANSI color codes."""
    line = "[32m🌿 main[0m [35m🪐 main[0m [36m🤖 Opus 4.7[0m"
    assert _looks_like_assistant_footer_row(line), (
        "Without ANSI stripping inside the recognizer, the line appears to start "
        "with the escape character (not 🌿) and the strip silently fails — that's "
        "the actual leak the user saw on the lens."
    )


def test_recognizes_bypass_hint_wrapped_in_ansi_color_codes() -> None:
    line = "[2m[38;5;245m►► bypass permissions on (shift+tab to cycle) · ← for agents[0m"
    assert _looks_like_assistant_footer_row(line)


def test_strips_real_world_ansi_wrapped_footer() -> None:
    """End-to-end: a snapshot mirroring what the user actually sees on
    aligned-tools — ANSI-wrapped status + hint at the bottom. The strip
    must reach AND remove both."""
    lines = [
        "assistant: here is the answer",
        "more conversation",
        "›",
        "[32m🌿 main[0m [35m🪐 main[0m [36m🤖 Opus 4.7 (1M context) ⚡high ✏️ +427 -94 $1.20 49.6k/1000k ○○○○[0m",
        "[2m►► bypass permissions on (shift+tab to cycle) · ← for agents[0m",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == [
        "assistant: here is the answer",
        "more conversation",
        "›",
    ], f"Expected ANSI-wrapped chrome stripped, got: {out!r}"


# ---------------------------------------------------------------------------
# Decorative-only rows — Claude Code's empty input-box border.
# ---------------------------------------------------------------------------


def test_strips_underscore_input_box_rows() -> None:
    """Claude Code paints an empty textbox as ~3 rows of underscores."""
    lines = [
        "your prompt",
        "›",
        "__________________________________",
        "__________________________________",
        "__________________________________",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == ["your prompt", "›"]


def test_strips_box_drawing_input_box() -> None:
    """Some assistants use Unicode box-drawing characters for the input frame."""
    lines = [
        "conversation row",
        "›",
        "╭─────────────────────────────╮",
        "│                             │",
        "╰─────────────────────────────╯",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == ["conversation row", "›"]


def test_strips_decorative_rows_above_status_footer() -> None:
    """Real combined case: decorative input border above the status line.

    Without the decorative-row stripper the walker would stop at the
    first non-empty non-pattern row (the underscores), leaving the
    bypass hint and status line still attached at the bottom."""
    lines = [
        "user message",
        "claude: response",
        "›",
        "__________________________________",
        "__________________________________",
        "[32m🌿 main 🤖 Opus 4.7[0m",
        "[2m►► bypass permissions on[0m",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == [
        "user message",
        "claude: response",
        "›",
    ], f"Mixed decorative + ANSI-wrapped chrome must all strip. Got: {out!r}"


def test_footer_strip_leaves_interior_decoratives_for_divider_pass() -> None:
    """`_strip_assistant_status_footer` itself only touches the trailing edge.
    Interior decorative rows are the divider pass's responsibility, not this
    function's. So called directly, the interior divider survives here — it
    gets removed in the pipeline by `_strip_decorative_divider_rows` running
    first."""
    lines = [
        "section one",
        "─────────────────",
        "section two with content",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == lines, (
        "footer strip is bottom-only; interior dividers are the divider "
        "pass's job — see test_strips_plan_mode_section_divider."
    )


# ---------------------------------------------------------------------------
# Cooked-for status row (Claude Code shows this between input and footer).
# ---------------------------------------------------------------------------


def test_strips_cooked_for_status_row() -> None:
    """`✻ Cooked for 1m 32s · 1 shell still running` is Claude Code's
    compaction/runtime status. Pure chrome; user sees it as noise."""
    lines = [
        "response content",
        "✻ Cooked for 1m 32s · 1 shell still running",
        "🌿 main",
        "►► bypass permissions",
    ]
    out = _strip_assistant_status_footer(lines)
    assert out == ["response content"]


# ---------------------------------------------------------------------------
# Interior `─` section-divider strip — the regression class the user hit on
# plan-mode interviews. Claude Code emits long runs of U+2500 between
# conversation blocks; on iOS GoogleSansCode and the lens SDK font those
# render as prominent strokes (Ghostty renders them as thin baseline rules).
# Real captures from `tmux capture-pane -p -t patina-0` had 3 rows of 215
# `─` characters each interleaved through the snapshot. The bottom-only
# footer walker stopped at them and they passed through to iOS verbatim.
# ---------------------------------------------------------------------------


def test_strips_plan_mode_section_divider() -> None:
    """The headline regression: `─`*N dividers between sections must go."""
    lines = [
        "your prompt",
        "─" * 80,
        "Planning: /Users/.../plan.md",
        "─" * 80,
        "How deep should the long-term fix go?",
    ]
    out = _strip_decorative_divider_rows(lines)
    assert out == [
        "your prompt",
        "Planning: /Users/.../plan.md",
        "How deep should the long-term fix go?",
    ], f"Expected interior dividers removed, got: {out!r}"


def test_strips_underscore_dividers_too() -> None:
    """Some plan-mode renderings use ASCII `_` rather than U+2500."""
    lines = [
        "first",
        "_" * 80,
        "second",
        "_" * 80,
        "third",
    ]
    out = _strip_decorative_divider_rows(lines)
    assert out == ["first", "second", "third"]


def test_preserves_decorative_mixed_with_text() -> None:
    """Rows that mix decorative chars with REAL TEXT carry information
    and must survive — the divider strip is a chrome filter, not a
    "remove anything pretty" filter."""
    lines = ["section one", "── important note ──", "section two"]
    out = _strip_decorative_divider_rows(lines)
    assert out == lines, (
        f"rows that mix decoratives with real text must be preserved: {out!r}"
    )


def test_strips_unicode_box_drawing_only_rows() -> None:
    """A bare `╭──╮` row is pure chrome. A `╭── label ──╮` row is content."""
    lines = [
        "before",
        "╭──────────────╮",
        "│              │",
        "╰──────────────╯",
        "after",
        "╭── named box ──╮",
    ]
    out = _strip_decorative_divider_rows(lines)
    assert out == ["before", "after", "╭── named box ──╮"], (
        f"Pure box-drawing rows must be stripped; labeled box-drawing rows preserved. Got: {out!r}"
    )


def test_handles_consecutive_dividers() -> None:
    """Three dividers in a row collapse to nothing — no spurious blank
    line left behind, no off-by-one that preserves one of the three."""
    lines = [
        "content above",
        "─" * 80,
        "─" * 80,
        "─" * 80,
        "content below",
    ]
    out = _strip_decorative_divider_rows(lines)
    assert out == ["content above", "content below"]


def test_combines_cleanly_with_footer_strip() -> None:
    """End-to-end realistic snapshot: interior dividers + bottom chrome.
    Pipeline order (matches the production order in `_terminal_poll_loop`):
    divider strip first, then footer strip. Both passes leave only real
    conversation content."""
    ESC = "\x1b"
    lines = [
        "user: hello",
        "─" * 80,
        "claude: here is my response",
        "─" * 80,
        "Planning: /tmp/plan.md",
        "─" * 80,
        "›",
        f"{ESC}[32m🌿 main 🤖 Opus 4.7{ESC}[0m",
        f"{ESC}[2m►► bypass permissions on{ESC}[0m",
    ]
    out = _strip_assistant_status_footer(_strip_decorative_divider_rows(lines))
    assert out == [
        "user: hello",
        "claude: here is my response",
        "Planning: /tmp/plan.md",
        "›",
    ], f"Combined strip should yield clean conversation + input prompt only. Got: {out!r}"


def test_strips_ansi_wrapped_divider() -> None:
    """tmux capture-pane often paints dividers wrapped in color codes,
    e.g. `\\x1b[36m──────\\x1b[0m`. `_is_decorative_only_row` strips
    ANSI internally before checking the char-set, so this case works
    without any extra logic — this test pins that behavior."""
    ESC = "\x1b"
    lines = [
        "before",
        f"{ESC}[36m" + ("─" * 80) + f"{ESC}[0m",
        "after",
    ]
    out = _strip_decorative_divider_rows(lines)
    assert out == ["before", "after"]


def test_divider_strip_empty_input_returns_empty() -> None:
    assert _strip_decorative_divider_rows([]) == []


def test_divider_strip_all_dividers_returns_empty() -> None:
    """Snapshot of nothing but dividers (rare, but possible right after a
    `clear`): we return empty. The user sees an empty terminal, which is
    correct — there's no real content yet."""
    lines = ["─" * 80, "─" * 80, "─" * 80]
    out = _strip_decorative_divider_rows(lines)
    assert out == []


def test_strips_mid_dot_separator_rows() -> None:
    """Some CLI chrome uses `·` (U+00B7) as a divider — e.g. a row of
    centered dots between sections. Stripping these matches the same
    intent as `─` rows."""
    lines = ["content", "· · · · · · · ·", "more content"]
    out = _strip_decorative_divider_rows(lines)
    assert out == ["content", "more content"]


def test_strips_bullet_separator_rows() -> None:
    lines = ["content", "• • • • • • • •", "more content"]
    out = _strip_decorative_divider_rows(lines)
    assert out == ["content", "more content"]


def test_strips_block_element_progress_chrome() -> None:
    """Codex / Gemini sometimes render a vertical-bar divider with block
    elements (e.g. `▌▐░▒▓█`). A row of nothing but block chars is chrome."""
    lines = ["content", "░▒▓█▓▒░▒▓█▓▒░▒▓█▓▒░", "more content"]
    out = _strip_decorative_divider_rows(lines)
    assert out == ["content", "more content"]


def test_preserves_mid_dot_inside_real_text() -> None:
    """A row like `Cooked for 1m · 1 shell running` has `·` mixed with real
    text and must be preserved by the divider pass. The footer pass owns
    stripping that line via its `shell still running` marker — they don't
    overlap. This test pins that the mid-dot extension to the decorative
    set didn't accidentally widen the divider strip to swallow real
    content rows."""
    lines = [
        "before",
        "✻ Cooked for 1m · 1 shell still running",
        "after",
    ]
    out = _strip_decorative_divider_rows(lines)
    assert out == lines, (
        f"Mid-dot in a row with text must be preserved by the divider "
        f"pass. Got: {out!r}"
    )


# ---------------------------------------------------------------------------
# Long leading/trailing decorative runs on mixed-content lines.
#
# The pure-divider pass only handles rows where the ENTIRE non-whitespace
# content is decorative. A row like `---------------- mem0 integration`
# has real text after the dashes so it survives that pass, but the long
# leading dash run is still pure visual chrome. `_strip_long_decorative_runs`
# is the per-line cleanup for that mixed case.
# ---------------------------------------------------------------------------


def test_long_run_strip_leading_dashes_before_heading() -> None:
    """The headline case from the user report: section header with a long
    leading dash run."""
    result = _strip_long_decorative_runs("---------------- mem0 integration")
    assert result == " mem0 integration", (
        f"Expected leading 16 dashes removed (keeping the space-padded "
        f"heading), got: {result!r}"
    )


def test_long_run_strip_trailing_dashes() -> None:
    result = _strip_long_decorative_runs("section title ----------------")
    assert result == "section title ", f"Got: {result!r}"


def test_long_run_strip_both_ends() -> None:
    result = _strip_long_decorative_runs("------- title -------")
    assert result == " title ", f"Got: {result!r}"


def test_long_run_strip_preserves_short_runs() -> None:
    """3-char runs are under the threshold — preserve constructs like
    `---` (YAML front matter) and `-- comment`."""
    assert _strip_long_decorative_runs("--no-verify") == "--no-verify"
    assert _strip_long_decorative_runs("-- comment line") == "-- comment line"
    assert _strip_long_decorative_runs("--- yaml frontmatter") == "--- yaml frontmatter"
    assert _strip_long_decorative_runs("─── header ───") == "─── header ───", (
        "2- or 3-char box-drawing borders carry intentional visual rhythm "
        "and must be preserved."
    )


def test_long_run_strip_preserves_at_threshold() -> None:
    """Exactly 4 chars hits the threshold — strip."""
    assert _strip_long_decorative_runs("---- ok ----") == " ok "


def test_long_run_strip_handles_unicode_horizontals() -> None:
    """U+2500 `─` runs are the canonical Claude Code section-divider char."""
    result = _strip_long_decorative_runs("──────── plan-mode section ────────")
    assert result == " plan-mode section ", f"Got: {result!r}"


def test_long_run_strip_preserves_leading_whitespace() -> None:
    """Indented section headers keep their indent — only the run is gone."""
    result = _strip_long_decorative_runs("    ---------------- subhead")
    assert result == "     subhead", (
        f"Leading 4 spaces (indent) preserved + space after stripped run = "
        f"5 leading spaces. Got: {result!r}"
    )


def test_long_run_strip_no_op_on_plain_content() -> None:
    """A line with no leading/trailing decorative run returns unchanged
    (and as the same object — no extra allocation)."""
    line = "user typed something and pressed enter"
    assert _strip_long_decorative_runs(line) == line


def test_long_run_strip_no_op_on_empty() -> None:
    assert _strip_long_decorative_runs("") == ""
    assert _strip_long_decorative_runs("   ") == "   "


def test_long_run_strip_does_not_swallow_decorated_word_boundary() -> None:
    """A row like `text-with-many-dashes` has dashes but they're inside the
    word, not a leading/trailing run. Must be preserved verbatim."""
    line = "function-name-with-many-internal-dashes()"
    assert _strip_long_decorative_runs(line) == line


def test_long_run_strip_handles_block_element_dividers() -> None:
    """Codex / Gemini sometimes use block elements as a divider."""
    assert _strip_long_decorative_runs("░░░░░░ progress 50%") == " progress 50%"
    assert _strip_long_decorative_runs("section ▓▓▓▓▓▓▓▓") == "section "


def test_long_run_strip_preserves_box_corners() -> None:
    """Box-drawing CORNERS (╭ ╮ ╰ ╯ ┌ …) are decorative but never form a
    horizontal run; they're not in `_STRIP_RUN_CHARS`. A line starting
    with a corner is preserved entirely."""
    line = "╭── named box ──╮"
    assert _strip_long_decorative_runs(line) == line


# ---------------------------------------------------------------------------
# Top-of-snapshot pixel-art detection.
#
# Claude Code paints a multi-row pink mascot icon at the top of every fresh
# session, drawn with block-element chars (e.g. ░▒▓█▌▐). Without the carve-
# out the long-run / divider passes would strip those leading characters and
# the user would see header text with no icon — losing the "cute character"
# the user asked to keep visible on the lens.
# ---------------------------------------------------------------------------


def test_counts_claude_mascot_at_top() -> None:
    """Three rows of leading block chars at the top → recognized as art."""
    lines = [
        "▒▒▓▓   Claude Code v2.1.145",
        "▓▓██▒  Opus 4.7 (1M context) · Claude Max",
        " ▒▒▒   ~/dev/web-apps/voxherd",
        "",
        "> /clear",
    ]
    assert _count_top_pixel_art_rows(lines) == 3


def test_skips_single_block_row_at_top() -> None:
    """One row of leading blocks isn't art — it's a progress bar / divider.
    Must NOT be preserved (the other passes still need to strip it)."""
    lines = [
        "░░░░░░░ progress 50%",
        "regular text",
        "more text",
    ]
    assert _count_top_pixel_art_rows(lines) == 0


def test_ignores_interior_block_rows() -> None:
    """The art carve-out is anchored to the TOP only. Interior blocks
    (e.g. a mid-snapshot progress bar) are not preserved by this helper."""
    lines = [
        "regular header",
        "▓▓▓▓▓▓ progress",
        "▒▒▒▒▒▒ more progress",
        "trailing text",
    ]
    assert _count_top_pixel_art_rows(lines) == 0


def test_empty_input_returns_zero() -> None:
    assert _count_top_pixel_art_rows([]) == 0
    assert _count_top_pixel_art_rows([""]) == 0
    assert _count_top_pixel_art_rows(["", "", ""]) == 0


def test_stops_counting_after_first_non_art_row() -> None:
    """A non-art row in the middle of an art block ends the count there —
    we only preserve a contiguous run from the top."""
    lines = [
        "▒▒▒▒ row1",
        "▓▓▓▓ row2",
        "plain text row3",
        "▒▒▒▒ row4 (already past the art block)",
    ]
    assert _count_top_pixel_art_rows(lines) == 2


def test_art_block_handles_ansi_color_codes() -> None:
    """Claude Code wraps the mascot in ANSI color codes (the pink fg).
    The detector strips ANSI before checking leading chars."""
    lines = [
        "\x1b[38;5;204m▒▒▓▓\x1b[0m   Claude Code v2.1.145",
        "\x1b[38;5;204m▓▓██▒\x1b[0m  Opus 4.7",
        "\x1b[38;5;204m ▒▒▒\x1b[0m   ~/dev/voxherd",
    ]
    assert _count_top_pixel_art_rows(lines) == 3


def test_three_char_threshold_excludes_single_corner() -> None:
    """A row whose leading non-whitespace is just `█` (1 char) doesn't
    qualify — needs ≥3 leading block chars to count toward art."""
    lines = [
        "█ heading 1",
        "█ heading 2",
        "█ heading 3",
    ]
    assert _count_top_pixel_art_rows(lines) == 0


# ---------------------------------------------------------------------------
# _strip_assistant_chrome_anywhere — handles chrome stranded mid-snapshot
# by transient UI (agent-spawn menus, etc.) painted below it.
# ---------------------------------------------------------------------------


def test_chrome_anywhere_strips_interior_chrome_below_agent_menu() -> None:
    """Real bug reported 2026-05-20: when the agent-spawn selection
    menu is painted BELOW the bypass-permissions footer, the bottom-
    walking strip stops at the menu and leaves the chrome stranded
    mid-snapshot. This pass catches it."""
    lines = [
        "user typed something",
        "❯ ",
        " 🌿 main 🌳 main 🤖 Opus 4.7 (1M context) ⚡max ✏️ +5737 -564 $31.70",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        "",
        "  ⏺ main",
        "  ◯ Explore  Trace session status sync bug",
    ]
    out = _strip_assistant_chrome_anywhere(lines)
    # Chrome lines gone, menu rows preserved.
    assert " 🌿 main 🌳 main 🤖 Opus 4.7 (1M context) ⚡max ✏️ +5737 -564 $31.70" not in out
    assert "  ⏵⏵ bypass permissions on (shift+tab to cycle)" not in out
    assert "  ⏺ main" in out
    assert "  ◯ Explore  Trace session status sync bug" in out


def test_chrome_anywhere_preserves_real_text() -> None:
    """Real conversation rows must NOT be stripped just because they
    appear among chrome-shaped rows. The patterns we match are too
    specific to false-positive on prose, but verify the safety net."""
    lines = [
        "I want to discuss the bypass permissions feature.",
        "Let me explain how ⏵⏵ works in shells.",  # uses chrome prefix but inline
        "real content",
    ]
    out = _strip_assistant_chrome_anywhere(lines)
    assert lines[0] in out
    # The second line starts with prose; the chrome heuristic matches
    # only when ⏵⏵ is the LEADING token after whitespace, which it is
    # here — so this IS treated as chrome. That's the documented
    # tradeoff (per the function's docstring).
    # Just confirm prose without leading chrome markers survives.
    assert "real content" in out


def test_chrome_anywhere_strips_decorative_rule_adjacent_to_chrome() -> None:
    """A `────` rule directly above the model-status chrome should
    also be dropped — leaving an orphan rule in place looks worse
    than removing the whole footer block."""
    lines = [
        "real conversation",
        "──────────────────────────────────────────────",
        " 🌿 main 🤖 Opus 4.7",
        " ⏵⏵ bypass permissions",
    ]
    out = _strip_assistant_chrome_anywhere(lines)
    assert "real conversation" in out
    # The divider above the chrome is also dropped.
    assert all("────" not in ln for ln in out)
    assert all("🌿" not in ln for ln in out)
    assert all("⏵⏵" not in ln for ln in out)


def test_chrome_anywhere_preserves_lone_decorative_rule() -> None:
    """A `────` rule that does NOT adjoin chrome (e.g., a section
    divider mid-conversation) must survive. The decorative-row strip
    is a separate pass; this function only strips decoratives that
    touch a chrome row."""
    lines = [
        "before content",
        "──────────────────────────────────────────────",
        "after content",
    ]
    out = _strip_assistant_chrome_anywhere(lines)
    assert "before content" in out
    assert "──────────────────────────────────────────────" in out
    assert "after content" in out


def test_chrome_anywhere_handles_empty_input() -> None:
    assert _strip_assistant_chrome_anywhere([]) == []
    assert _strip_assistant_chrome_anywhere([""]) == [""]


# ---------------------------------------------------------------------------
# Task-summary widget strip
# ---------------------------------------------------------------------------
#
# Claude Code's TodoWrite widget paints a multi-row task summary inside
# the captured pane. On the lens (23-row budget) this block eats 6-8
# rows for content that is not actionable — voice TTS already announces
# task transitions, the phone-side pager doesn't need it either. The
# strip pass drops all three components of the widget regardless of
# where they sit in the snapshot.


def test_task_summary_header_recognized() -> None:
    assert _is_task_summary_row("175 tasks (165 done, 10 open)") is True
    assert _is_task_summary_row("  1 task (0 done, 1 open)") is True
    # Plural vs singular both accepted.
    assert _is_task_summary_row("1 tasks (0 done, 1 open)") is True


def test_task_summary_checkbox_rows_recognized() -> None:
    # Outline checkboxes (Claude Code uses ◻ U+25FB for open tasks).
    assert _is_task_summary_row("◻ Phase 6 — Future") is True
    assert _is_task_summary_row("  ◻ Backlog: Save team feature") is True
    assert _is_task_summary_row("☑ Implement the thing") is True
    assert _is_task_summary_row("✓ Done") is True
    # Filled checkboxes (Claude Code uses ■ U+25A0 for in-progress).
    # This was missing from the first version of the filter — user
    # reported the in-progress rows leaking through to the lens.
    assert _is_task_summary_row("■ Backend: Stream OpenAI Responses API") is True
    assert _is_task_summary_row("  ■ Monitor CI on PR #988 and fix failures") is True
    assert _is_task_summary_row("◼ Black medium variant") is True


def test_task_summary_three_state_header() -> None:
    # Claude Code shows "(N done, M in progress, K open)" when any
    # rows are in progress. Header regex matches on the prefix so
    # this shape is covered — pin it with a test.
    assert _is_task_summary_row("148 tasks (134 done, 6 in progress, 8 open)") is True


def test_task_summary_three_state_tail() -> None:
    # Truncation tail can lead with "+N in progress" before "pending".
    # The original regex only matched "+N pending" right after the
    # ellipsis, leaving the three-state tail visible on the lens.
    assert _is_task_summary_row("… +1 in progress, 8 pending, 134 completed") is True
    assert _is_task_summary_row("  … +1 in progress") is True
    # Old shape (no in-progress) still matches.
    assert _is_task_summary_row("… +5 pending, 165 completed") is True


def test_task_summary_truncation_tail_recognized() -> None:
    # The "… +5 pending, 165 completed" tail with both ellipsis forms.
    assert _is_task_summary_row("… +5 pending, 165 completed") is True
    assert _is_task_summary_row("... +5 pending, 165 completed") is True
    assert _is_task_summary_row("  … +12 pending") is True


def test_task_summary_does_not_match_prose() -> None:
    # Header pattern is specific (digit count + "tasks" + paren) — prose
    # mentioning "tasks" without the count shape must not match.
    assert _is_task_summary_row("I added 5 tasks to the list") is False
    assert _is_task_summary_row("The 175 tasks above are stale") is False
    # Tail pattern needs the leading ellipsis — prose with "+5 pending"
    # in the middle must not match.
    assert _is_task_summary_row("There are +5 pending requests") is False
    # Empty + whitespace.
    assert _is_task_summary_row("") is False
    assert _is_task_summary_row("   ") is False


def test_strips_full_task_widget_block() -> None:
    # Reproduces the user's screenshot: assistant content followed by
    # the task widget. Pass should remove the widget rows, leave content.
    lines = [
        "Sautéed for 38s",
        "",
        "175 tasks (165 done, 10 open)",
        "◻ Phase 6 — Future (post-PMF backlog, do not start)",
        "◻ Backlog: Save team feature",
        "◻ Backlog: /app skill-first refactor",
        "◻ Validate Composio sync end-to-end for all 4 PR2 providers",
        "◻ Get correct Composio auth-config ids for LinkedIn/Calendar/Sheets/Docs",
        "… +5 pending, 165 completed",
    ]
    out = _strip_task_summary(lines)
    assert out == [
        "Sautéed for 38s",
        "",
    ]


def test_strips_three_state_task_widget_block() -> None:
    # User's second screenshot: widget with in-progress rows (■ glyph)
    # and a three-state header + tail. All widget rows must drop.
    lines = [
        "Worked for 16m 43s",
        "",
        "148 tasks (134 done, 6 in progress, 8 open)",
        "■ Add assigneeSource provenance to SprintIssue type",
        "■ Backend: Stream OpenAI Responses API in _call_once",
        "■ Backend: Emit 5 stage progress markers in FastSprintPlanner.run",
        "■ Monitor CI on PR #988 and fix failures",
        "■ Monitor CI, merge, then trigger auto-staging-to-main",
        "  … +1 in progress, 8 pending, 134 completed",
    ]
    out = _strip_task_summary(lines)
    assert out == [
        "Worked for 16m 43s",
        "",
    ]


def test_strips_task_widget_anywhere_in_snapshot() -> None:
    # Widget can appear mid-snapshot when Claude rendered it earlier in
    # the conversation and scrolled. Filter is per-line so position
    # doesn't matter.
    lines = [
        "Real conversation content above",
        "175 tasks (165 done, 10 open)",
        "◻ One",
        "More real content below",
        "◻ Two stranded checkbox row",
        "End of snapshot",
    ]
    out = _strip_task_summary(lines)
    assert out == [
        "Real conversation content above",
        "More real content below",
        "End of snapshot",
    ]


def test_strips_task_widget_with_ansi_color() -> None:
    # Claude Code wraps the widget in ANSI color codes; the per-line
    # ANSI strip inside the matcher means colored rows still match.
    lines = [
        "\x1b[2m175 tasks (165 done, 10 open)\x1b[0m",
        "\x1b[2m◻ Item 1\x1b[0m",
    ]
    out = _strip_task_summary(lines)
    assert out == []


def test_task_summary_with_tree_corner_prefix_recognized() -> None:
    # When the TodoWrite widget appears inside a folded thinking block,
    # Claude Code prefixes the first task row with `└ ` (U+2514 box-
    # drawing light up-and-right) as the tree connector. After
    # `.strip()`, `stripped[0]` is the corner glyph — not the checkbox —
    # so the naive `stripped[0] in _TASK_CHECKBOX_CHARS` check misses
    # it and the parent row leaks through to the lens. Filter must
    # look past leading box-drawing connectors to find the checkbox.
    assert _is_task_summary_row("  └ □ Phase 6 — Future (post-PMF backlog, do not start)") is True
    assert _is_task_summary_row("└ ◻ Phase 6 — Future") is True
    assert _is_task_summary_row("├ ◻ Item under a non-terminal corner") is True
    assert _is_task_summary_row("  └─ ■ In-progress under a flat connector") is True


def test_thinking_fold_header_recognized() -> None:
    # Claude Code's extended-thinking "max effort" mode emits a folded
    # spinner line above the TodoWrite widget:
    #
    #   + Finagling… (2m 7s · ↓ 4.9k tokens · almost done thinking with max effort)
    #
    # The `+` is a fold/expand marker; the parenthetical always contains
    # the literal word "thinking". This line is not task content but
    # routes to the lens above the widget. Match it so the whole block
    # disappears.
    assert _is_task_summary_row(
        "+ Finagling… (2m 7s · ↓ 4.9k tokens · almost done thinking with max effort)"
    ) is True
    assert _is_task_summary_row(
        "+ Sautéing… (8s · ↑ 1.2k tokens · thinking)"
    ) is True
    # ASCII-only ellipsis form is also seen on some terminals.
    assert _is_task_summary_row(
        "+ Whisking... (1m · ↓ 800 tokens · still thinking with max effort)"
    ) is True


def test_thinking_fold_active_spinner_header_recognized() -> None:
    # While the model is still thinking the fold header leads with a
    # rotating spinner glyph instead of the `+` fold marker:
    #
    #   · Scurrying… (1m 53s · ↓ 3.2k tokens · almost done thinking with max effort)
    #
    # The 2026-05-27 hack-day report: the `+`-only anchor let this
    # active-spinner header (and the TodoWrite widget folded under it)
    # leak onto the lens. Match the active form across the spinner
    # frames Claude Code cycles through (middle dot, asterisk/star-burst,
    # braille).
    assert _is_task_summary_row(
        "· Scurrying… (1m 53s · ↓ 3.2k tokens · almost done thinking with max effort)"
    ) is True
    assert _is_task_summary_row(
        "* Cogitating… (45s · ↓ 2.1k tokens · still thinking)"
    ) is True
    assert _is_task_summary_row(
        "✻ Pondering… (12s · thinking with max effort)"
    ) is True
    assert _is_task_summary_row(
        "⠹ Deliberating… (3m · ↓ 9k tokens · almost done thinking)"
    ) is True


def test_thinking_fold_does_not_match_prose() -> None:
    # The header is specific: fold/spinner-glyph prefix, parenthetical, and
    # the literal word `thinking` inside the parens. Prose mentioning
    # "thinking", or `+`/`·` used in other contexts, must not match.
    assert _is_task_summary_row("+ added a thinking comment") is False
    assert _is_task_summary_row("I was thinking about this (later)") is False
    assert _is_task_summary_row("+ Finagling… (no parens token here)") is False
    assert _is_task_summary_row("Finagling… (2m · ↓ 4.9k · thinking)") is False  # missing glyph
    # An ordinary working line carries no "thinking" inside the parens
    # (the verb sits OUTSIDE the parens), so the spinner-glyph prefix
    # alone must not drag it into the task-widget filter — it's real
    # status the lens footer derives from.
    assert _is_task_summary_row("* Blanching… (54s · ↓ 1.3k tokens)") is False
    assert _is_task_summary_row("· Thinking… (12s)") is False  # verb out of parens
    assert _is_task_summary_row("✻ Cogitating… (45s · ↓ 2.1k tokens · esc to interrupt)") is False


def test_strips_full_thinking_fold_active_spinner_with_nested_widget() -> None:
    # Verbatim reproduction of the user's 2026-05-27 hack-day screenshot:
    # the TodoWrite widget folded under an ACTIVE-spinner thinking header
    # (`· Scurrying…`) rather than the folded `+` form. All three parts —
    # the spinner header, the `└ ☐ Phase 6 …` tree-corner parent row, the
    # indented checkbox children, and the truncation tail — must drop,
    # leaving only the conversational content above the widget.
    lines = [
        "User answered Claude's questions:",
        "",
        "· Scurrying… (1m 53s · ↓ 3.2k tokens · almost done thinking with max effort)",
        "  └ ☐ Phase 6 — Future (post-PMF backlog, do not start)",
        "    ☐ Backlog: Save team feature",
        "    ☐ Backlog: /app skill-first refactor",
        "    ☐ Validate Composio sync end-to-end for all 4 PR2 providers",
        "    ☐ Get correct Composio auth-config ids for LinkedIn/Calendar/Sheets/Docs",
        "     … +4 pending, 191 completed",
    ]
    out = _strip_task_summary(lines)
    assert out == [
        "User answered Claude's questions:",
        "",
    ]


def test_strips_full_thinking_fold_with_nested_widget() -> None:
    # Verbatim reproduction of the user's 2026-05-21 screenshot: the
    # TodoWrite widget appears inside a folded thinking block. Three
    # things must be stripped together —
    #   (1) the `+ Finagling…` fold header
    #   (2) the `└ □ Phase 6 …` parent row with tree-corner prefix
    #   (3) all indented checkbox children + the truncation tail
    # — leaving only the conversational content before the widget.
    lines = [
        "Good intel — two arrows are supposed to already render.",
        "",
        "+ Finagling… (2m 7s · ↓ 4.9k tokens · almost done thinking with max effort)",
        "  └ □ Phase 6 — Future (post-PMF backlog, do not start)",
        "     □ Backlog: Save team feature",
        "     □ Backlog: /app skill-first refactor",
        "     □ Validate Composio sync end-to-end for all 4 PR2 providers",
        "     □ Get correct Composio auth-config ids for LinkedIn/Calendar/Sheets/Docs",
        "    … +5 pending, 173 completed",
    ]
    out = _strip_task_summary(lines)
    assert out == [
        "Good intel — two arrows are supposed to already render.",
        "",
    ]


# ---------------------------------------------------------------------------
# Session-feedback prompt strip
# ---------------------------------------------------------------------------


def test_feedback_prompt_header_recognized() -> None:
    assert _is_feedback_prompt_row("How is Claude doing this session? (optional)") is True
    assert _is_feedback_prompt_row("  How is Claude doing this session?") is True


def test_feedback_prompt_options_row_recognized() -> None:
    # Options row carries the unique "0: Dismiss" token.
    assert _is_feedback_prompt_row("  1: Bad    2: Fine   3: Good   0: Dismiss") is True


def test_feedback_prompt_does_not_match_prose() -> None:
    # Header literal — prose mentioning "Claude" generally must not
    # match.
    assert _is_feedback_prompt_row("Claude is doing great this morning") is False
    # Options token: "0: Dismiss" anywhere triggers the match, but the
    # token is specific enough that user content is unlikely to
    # contain it. Quick sanity check that ordinary numbered prose
    # doesn't match.
    assert _is_feedback_prompt_row("Step 0: figure out the bug") is False
    assert _is_feedback_prompt_row("") is False


def test_strips_feedback_prompt_block() -> None:
    lines = [
        "real conversation content",
        "",
        "How is Claude doing this session? (optional)",
        "  1: Bad    2: Fine   3: Good   0: Dismiss",
        "more content below",
    ]
    out = _strip_feedback_prompt(lines)
    assert out == [
        "real conversation content",
        "",
        "more content below",
    ]


def test_strips_feedback_prompt_with_ansi() -> None:
    lines = [
        "\x1b[2mHow is Claude doing this session? (optional)\x1b[0m",
        "\x1b[2m  1: Bad    2: Fine   3: Good   0: Dismiss\x1b[0m",
    ]
    out = _strip_feedback_prompt(lines)
    assert out == []


# ---------------------------------------------------------------------------
# "Tip:" onboarding-hint strip — the headline regression for this change.
# Claude Code paints a rotating hint row below the input box, e.g.
#   ※ Tip: Use /config to change your default permission mode (incl Plan Mode)
# On the lens it's pure chrome eating a row every refresh.
# ---------------------------------------------------------------------------


def test_tip_row_recognized_plain() -> None:
    assert _is_tip_row(
        "Tip: Use /config to change your default permission mode (including Plan Mode)"
    ) is True
    assert _is_tip_row("Tip: Use Plan Mode to scope work before editing") is True


def test_tip_row_recognized_with_decoration_glyph() -> None:
    # Claude Code prefixes the hint with a rotating decoration glyph.
    assert _is_tip_row("※ Tip: Use /config to change your default permission mode") is True
    assert _is_tip_row("💡 Tip: Press ctrl+r to search history") is True
    assert _is_tip_row("  ✢ Tip: indented hint with a star prefix") is True


def test_tip_row_case_insensitive() -> None:
    assert _is_tip_row("tip: lowercase still chrome") is True


def test_tip_row_does_not_match_prose() -> None:
    # "Tip" mid-sentence, or as a word without the trailing colon, is real
    # content and must survive.
    assert _is_tip_row("The tip of the iceberg is small") is False
    assert _is_tip_row("Here's a tip for you") is False
    # A colon later in the line (not anchored at the token) must not match.
    assert _is_tip_row("I have one tip: it appears mid-sentence") is False
    assert _is_tip_row("") is False
    assert _is_tip_row("   ") is False


def test_strips_tip_row_in_pipeline_position() -> None:
    lines = [
        "assistant: here is the answer",
        "›",
        "※ Tip: Use /config to change your default permission mode (including Plan Mode)",
    ]
    out = _strip_tip_rows(lines)
    assert out == ["assistant: here is the answer", "›"]


def test_strips_tip_row_with_ansi() -> None:
    lines = [
        "\x1b[2m※ Tip: Use /config to change your default permission mode\x1b[0m",
    ]
    out = _strip_tip_rows(lines)
    assert out == []


def test_tip_strip_preserves_other_content() -> None:
    lines = ["real one", "Tip: chrome line", "real two"]
    out = _strip_tip_rows(lines)
    assert out == ["real one", "real two"]


# --- Context-window indicator strip (footer shows the live % now) -----------

def test_strips_percent_context_used_row() -> None:
    lines = ["real output line", "61% context used", "more output"]
    out = _strip_context_indicator_rows(lines)
    assert out == ["real output line", "more output"]


def test_strips_context_left_and_remaining_variants() -> None:
    lines = ["a", "42% context left", "b", "10% context remaining", "c"]
    out = _strip_context_indicator_rows(lines)
    assert out == ["a", "b", "c"]


def test_strips_auto_compact_row() -> None:
    lines = ["work", "Context left until auto-compact: 9%", "done"]
    out = _strip_context_indicator_rows(lines)
    assert out == ["work", "done"]


def test_strips_context_row_wrapped_in_ansi() -> None:
    lines = ["\x1b[2m61% context used\x1b[0m"]
    assert _is_context_indicator_row(lines[0])
    assert _strip_context_indicator_rows(lines) == []


def test_preserves_prose_without_the_indicator_shape() -> None:
    # Conservative: real conversation mentioning "context" must survive.
    lines = [
        "Let me add more context to the prompt.",
        "the context window is large",
        "we used a lot of memory",
    ]
    assert _strip_context_indicator_rows(lines) == lines


def test_context_strip_empty_input_returns_empty() -> None:
    assert _strip_context_indicator_rows([]) == []


# --- Working-status strip (footer carries verb+elapsed; kills the bounce) ----

def test_strips_working_status_minutes_form() -> None:
    lines = ["real output", "✻ Cogitating… (5m 42s)", "more output"]
    out = _strip_working_status_rows(lines)
    assert out == ["real output", "more output"]


def test_strips_working_status_seconds_and_tokens() -> None:
    lines = ["a", "* Thinking… (12s · ↓ 1.3k tokens)", "b"]
    out = _strip_working_status_rows(lines)
    assert out == ["a", "b"]


def test_strips_working_status_braille_spinner() -> None:
    lines = ["⠹ Drafting (3s)"]
    assert _is_working_status_row(lines[0])
    assert _strip_working_status_rows(lines) == []


def test_strips_working_status_with_interrupt_before_elapsed() -> None:
    # The interrupt hint can sit before the elapsed inside the parens.
    lines = ["✶ Reviewing… (esc to interrupt · 5m 42s)"]
    assert _strip_working_status_rows(lines) == []


def test_strips_working_status_wrapped_in_ansi() -> None:
    lines = ["\x1b[2m✻ Working… (8s)\x1b[0m"]
    assert _is_working_status_row(lines[0])
    assert _strip_working_status_rows(lines) == []


def test_preserves_gerund_prose_without_spinner() -> None:
    # No leading spinner glyph → must NOT be treated as the working line,
    # even if it contains an "-ing (Ns)"-ish phrase.
    lines = [
        "Thinking about the timeout (5s seemed too short)",
        "we are exploring options",
        "running (2s) tests passed",
    ]
    assert _strip_working_status_rows(lines) == lines


def test_preserves_bulleted_line_that_is_not_a_status() -> None:
    # A "·" bullet with a gerund but a non-elapsed parenthetical stays.
    lines = ["· Reviewing the changes (see notes below)"]
    assert _strip_working_status_rows(lines) == lines


def test_working_status_strip_empty_input_returns_empty() -> None:
    assert _strip_working_status_rows([]) == []


# --- Just-finished status form ("✻ Crunched for 1m 7s") ----------------------

def test_strips_finished_status_minutes_seconds() -> None:
    lines = ["did the work", "✻ Crunched for 1m 7s", "next"]
    assert _strip_working_status_rows(lines) == ["did the work", "next"]


def test_strips_finished_status_with_shells_suffix() -> None:
    lines = ["✻ Sautéed for 14m 37s · 6 shells still running"]
    assert _is_working_status_row(lines[0])
    assert _strip_working_status_rows(lines) == []


def test_strips_finished_status_accented_verb_and_ms() -> None:
    # "Sautéed" (accented) and a millisecond duration both must match.
    assert _is_working_status_row("✻ Sautéed for 11ms")
    assert _is_working_status_row("✻ Cogitated for 8m 35s · 2 shells still running")


def test_preserves_for_phrase_without_spinner_glyph() -> None:
    # No leading sparkle glyph → real prose, must survive.
    lines = [
        "Reserved for 3 users in the pool",
        "This ran for 5s before failing",
    ]
    assert _strip_working_status_rows(lines) == lines


def test_preserves_spinner_line_without_a_time_unit() -> None:
    # Sparkle prefix but the "for" target is a noun, not a duration → keep.
    lines = ["✻ Optimized for readability and speed"]
    assert _strip_working_status_rows(lines) == lines
