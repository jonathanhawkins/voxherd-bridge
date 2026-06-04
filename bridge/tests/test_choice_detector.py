"""Tests for bridge/choice_detector.py — header-gated numbered-choice detection.

The acceptance criterion is precision over recall: a false positive sends
a stray "1\\n" into the user's conversation when long-press fires. So we
lean heavily on negative tests (what should NOT trip) and a small set of
positive tests covering the shapes Claude Code actually emits.
"""

from __future__ import annotations

from bridge.choice_detector import (
    ChoicePrompt,
    MultiQuestionForm,
    detect_choice_prompt,
    detect_question_form,
    has_select_footer,
    is_form_invalidated,
    is_invalidated,
)


# ---------------------------------------------------------------------------
# Positive — should detect
# ---------------------------------------------------------------------------


class TestDetectsRealChoicePrompts:

    def test_question_header_three_options(self):
        lines = [
            "Looking at the diff, I see two paths.",
            "",
            "Which approach would you prefer?",
            "",
            "1. Use ISO format and migrate existing rows",
            "2. Keep Unix timestamps and add a view",
            "3. Drop the column entirely",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.title == "Which approach would you prefer?"
        assert p.options == [
            "Use ISO format and migrate existing rows",
            "Keep Unix timestamps and add a view",
            "Drop the column entirely",
        ]
        assert p.signature  # auto-computed

    def test_choose_an_option_header(self):
        lines = [
            "Choose an option:",
            "1. Yes, and auto-accept edits",
            "2. Yes, but ask each time",
            "3. No, abort",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert "Choose" in p.title
        assert len(p.options) == 3

    def test_four_options_with_close_paren_form(self):
        lines = [
            "Pick a plan:",
            "1) Refactor the auth layer",
            "2) Add a feature flag",
            "3) Roll back to last release",
            "4) Investigate further",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert len(p.options) == 4

    def test_options_with_trailing_chrome_below(self):
        # Claude Code's bypass/model footer often sits below the options;
        # _is_status_bar_line should strip it before detection.
        lines = [
            "Which would you like?",
            "1. First option",
            "2. Second option",
            "3. Third option",
            "  🌿 main 🤖 Opus 4.7",
            "  ►► bypass permissions on (shift+tab to cycle)",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert len(p.options) == 3

    def test_seven_option_cap(self):
        # 8 options — only the last 7 should be picked up, but detection
        # should still succeed.
        lines = [
            "Which one?",
            "1. a", "2. b", "3. c", "4. d",
            "5. e", "6. f", "7. g", "8. h",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert len(p.options) == 7
        # Walking bottom-up collects the last 7, then reverses — so we
        # get options 2..8 in order.
        assert p.options[0] == "b"
        assert p.options[-1] == "h"


# ---------------------------------------------------------------------------
# Negative — should NOT detect
# ---------------------------------------------------------------------------


class TestRejectsFalsePositives:

    def test_no_header_means_no_detection(self):
        # Three numbered lines but no question-shaped header — must not fire.
        lines = [
            "Here's the summary of what I did:",
            "1. Refactored auth.py",
            "2. Added a test",
            "3. Updated the README",
        ]
        assert detect_choice_prompt(lines) is None

    def test_too_few_options(self):
        lines = [
            "Which one?",
            "1. Only option",
            "2. Another",
        ]
        assert detect_choice_prompt(lines) is None

    def test_inside_code_block(self):
        # A fenced code block containing numbered text — must not fire.
        lines = [
            "Here is the regex pattern:",
            "```",
            "1. ^foo",
            "2. ^bar",
            "3. ^baz",
        ]
        # The fence is recognized as a code-block opener above the options,
        # so detection bails.
        assert detect_choice_prompt(lines) is None

    def test_options_with_blank_line_between(self):
        # Real choice prompts are contiguous. A blank between them breaks
        # the contiguous-block requirement.
        lines = [
            "Which one?",
            "1. First",
            "",
            "2. Second",
            "3. Third",
        ]
        # Only "2/3" remain contiguous; that's below the 3-min threshold.
        assert detect_choice_prompt(lines) is None

    def test_ansi_mid_render(self):
        # An ANSI escape inside an option line means Claude is still
        # streaming — wait for the next tick.
        lines = [
            "Which would you like?",
            "1. First option",
            "2. Second \x1b[31moption\x1b[0m",
            "3. Third option",
        ]
        assert detect_choice_prompt(lines) is None

    def test_prose_list_with_colon_header_NOT_detected(self):
        # Plan recommended: bare colon-ended headers are NOT enough alone.
        # We require either ``?`` or one of the known decision phrases.
        # This protects against "Here's a summary: 1. ..." style output.
        lines = [
            "Here's what I found:",
            "1. The bug is in module A",
            "2. There's a related issue in module B",
            "3. A test would catch both",
        ]
        assert detect_choice_prompt(lines) is None

    def test_short_input_returns_none(self):
        assert detect_choice_prompt([]) is None
        assert detect_choice_prompt(["?"]) is None


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


class TestIsInvalidated:

    def test_same_prompt_not_invalidated(self):
        lines = [
            "Which would you like?",
            "1. A", "2. B", "3. C",
        ]
        first = detect_choice_prompt(lines)
        assert first is not None
        assert not is_invalidated(first, lines)

    def test_new_content_below_invalidates(self):
        lines_v1 = [
            "Which would you like?",
            "1. A", "2. B", "3. C",
        ]
        prev = detect_choice_prompt(lines_v1)
        assert prev is not None

        # Claude appended a response (user said something, model answered)
        # — the option block is no longer at the bottom.
        lines_v2 = lines_v1 + [
            "",
            "OK, going with option 2. Let me apply that change now.",
        ]
        assert is_invalidated(prev, lines_v2)

    def test_different_prompt_invalidates(self):
        lines_v1 = [
            "Which would you like?",
            "1. A", "2. B", "3. C",
        ]
        prev = detect_choice_prompt(lines_v1)
        assert prev is not None

        lines_v2 = [
            "Pick a plan:",
            "1. Quick fix", "2. Proper refactor", "3. Skip it",
        ]
        # Different signature → invalidated.
        assert is_invalidated(prev, lines_v2)

    def test_signature_stable_across_polls(self):
        lines = [
            "Which approach?",
            "1. Alpha", "2. Beta", "3. Gamma",
        ]
        p1 = detect_choice_prompt(lines)
        p2 = detect_choice_prompt(lines)
        assert p1 is not None and p2 is not None
        assert p1.signature == p2.signature


class TestChoicePromptDataclass:

    def test_signature_auto_computed(self):
        p = ChoicePrompt(title="Pick one:", options=["a", "b", "c"])
        assert len(p.signature) == 16  # truncated sha1

    def test_explicit_signature_preserved(self):
        p = ChoicePrompt(title="x", options=["a"], signature="fixed-sig")
        assert p.signature == "fixed-sig"

    def test_input_mode_defaults_to_number(self):
        # Legacy callers don't set input_mode — must default to
        # "number" so the bridge's keystroke synthesis stays
        # backward-compatible.
        p = ChoicePrompt(title="Pick one:", options=["a", "b", "c"])
        assert p.input_mode == "number"
        assert p.focused_index is None

    def test_signature_stable_across_input_mode(self):
        # input_mode is implied by prompt shape (TUI footer present
        # or not) — including it in the signature would create
        # spurious cancellations if a future detector ever revised
        # the mode label. The signature is purely about *which
        # prompt is on screen*, not how it'll be committed.
        a = ChoicePrompt(title="t", options=["a", "b", "c"], input_mode="number")
        b = ChoicePrompt(title="t", options=["a", "b", "c"], input_mode="tui_select")
        assert a.signature == b.signature

    def test_signature_stable_across_focused_index(self):
        # Cursor movement (user pressed arrow on real keyboard, or
        # Claude Code re-rendered the same widget) must NOT invalidate
        # the prompt. Without this, every focus shift would re-broadcast
        # choice_prompt and bounce the iOS lens state.
        a = ChoicePrompt(title="t", options=["a", "b", "c"], focused_index=0)
        b = ChoicePrompt(title="t", options=["a", "b", "c"], focused_index=2)
        assert a.signature == b.signature


# ---------------------------------------------------------------------------
# TUI select-prompt path — footer-anchored detection
# ---------------------------------------------------------------------------


_TUI_FOOTER = "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"


class TestTuiSelectPromptDetection:

    def test_full_screenshot_fixture(self):
        # Reproduces the screenshot the user shared: 6 options with
        # multi-line bodies, blank line before item 6, cursor on item 1.
        lines = [
            "What should we tackle next?",
            "",
            ") 1. A3 — Tripwire offer (Recommended)",
            "     Build a $9-$19 one-time entry between Free and Pro.",
            "  2. A5 — Retargeting pixels first (quick win)",
            "     Install FB Pixel + LinkedIn Insight Tag ...",
            "  3. B1 — CVO Funnel Health Score tool",
            "     Switch tracks. Build a self-audit tool ...",
            "  4. A4 — Trial-onboarding wizard",
            "     Replace the consent-only /onboarding flow ...",
            "  5. Type something.",
            "",
            "  6. Chat about this",
            _TUI_FOOTER,
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"
        assert p.focused_index == 0  # cursor `)` on item 1
        assert len(p.options) == 6
        assert p.options[0].startswith("A3 — Tripwire")
        assert p.options[-1] == "Chat about this"
        assert p.title == "What should we tackle next?"

    def test_arrow_navigation_hint_footer_variant(self):
        # Regression: Claude Code renders the navigation hint as
        # "↑/↓ to navigate" (not "Tab/Arrow keys to navigate") in some
        # builds. The footer anchor must match this variant or the whole
        # widget falls through to the legacy path, fails (multi-line
        # bodies), and the lens shows approve/deny instead of the choice
        # card. Reproduces the "marketing next" screenshot, including the
        # divider rule the widget paints above the "Chat about this"
        # meta-option.
        footer = "Enter to select · ↑/↓ to navigate · Esc to cancel"
        lines = [
            "□ Marketing next",
            "",
            "While the deploy runs, which marketing track do you want to push on first?",
            "",
            "❯ 1. First paid campaign",
            "     Your friend's #1 rec: drive a cold-prospecting ad → homepage → free-trial popup.",
            "     Campaign A: ad copy variants, targeting, budget/structure, and the UTM/landing setup.",
            "  2. Retargeting audiences",
            "     The tracked pending task (#329). Pixels are firing now, so I'll write audience defs.",
            "     Foundational for warm/hot campaigns.",
            "  3. Get the X posts live",
            "     You have launch posts already drafted in the marketing DB. I'll finalize/sequence them.",
            "  4. Full 30-day rollout",
            "     Step back and lay out the whole sequenced plan — which goes live in which week.",
            "  5. Type something.",
            "  ────────────────────────────",
            "  6. Chat about this",
            footer,
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"
        assert p.focused_index == 0  # cursor `❯` on item 1
        assert len(p.options) == 6
        assert p.options[0] == "First paid campaign"
        assert p.options[-1] == "Chat about this"
        assert p.title.startswith("While the deploy runs")

    def test_cursor_glyph_chevron(self):
        # `›` (single right-pointing angle quotation) — another
        # cursor glyph Claude Code has shipped in past builds.
        lines = [
            "Pick a plan:",
            "  1. First",
            "› 2. Second",
            "  3. Third",
            _TUI_FOOTER,
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"
        assert p.focused_index == 1

    def test_cursor_glyph_heavy_angle(self):
        # `❯` (heavy right-pointing angle) — the same glyph used
        # for Claude Code's idle prompt; works here as a cursor too.
        lines = [
            "Pick one:",
            "  1. First",
            "  2. Second",
            "❯ 3. Third",
            _TUI_FOOTER,
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.focused_index == 2

    def test_no_cursor_focused_index_none(self):
        # If we observe the prompt during a re-render where no row
        # has the cursor (or if the build doesn't paint one), don't
        # guess — return None so the commit handler defaults to 0.
        lines = [
            "Choose:",
            "  1. First",
            "  2. Second",
            "  3. Third",
            _TUI_FOOTER,
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.focused_index is None

    def test_footer_with_status_chrome_below(self):
        # Status chrome (model/branch/etc.) under the footer must not
        # block detection — the footer is still the last non-chrome
        # content row.
        lines = [
            "Which approach?",
            ") 1. First",
            "  2. Second",
            "  3. Third",
            _TUI_FOOTER,
            "  \U0001f33f main  \U0001f916 Opus 4.7",
            "  ▶▶ bypass permissions on (shift+tab to cycle)",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"

    def test_title_picked_from_indented_skip(self):
        # The title sits a few lines above the option block; indented
        # continuations of item bodies must NOT be picked as title.
        lines = [
            "What should we do here?",
            "",
            ") 1. First option",
            "     description text for item 1",
            "  2. Second option",
            "     description text for item 2",
            "  3. Third option",
            _TUI_FOOTER,
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.title == "What should we do here?"


class TestTuiSelectPromptFooterWrap:
    """When the terminal is narrow, the footer wraps onto two lines.
    The anchor must still fire so the lens choice card stays up across
    wrap-induced render frames."""

    def test_footer_split_across_two_lines(self):
        # 70-col terminal wraps the footer at the second separator.
        lines = [
            "Pick a plan:",
            ") 1. First option",
            "  2. Second option",
            "  3. Third option",
            "Enter to select · Tab/Arrow keys to navigate ·",
            "Esc to cancel",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"
        assert len(p.options) == 3
        assert p.focused_index == 0

    def test_footer_split_with_trailing_chrome(self):
        # Wrap PLUS a model/branch chrome row below the wrap.
        lines = [
            "Pick a plan:",
            "  1. First option",
            "  2. Second option",
            "  3. Third option",
            "Enter to select · Tab/Arrow keys to navigate ·",
            "Esc to cancel",
            "  \U0001f33f main  \U0001f916 Opus 4.7",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"


class TestTuiSelectPromptRejection:

    def test_prose_with_enter_to_select_substring(self):
        # User prose containing the phrase "Enter to select" — the
        # *full* footer literal is required, so this must not fire.
        lines = [
            "Choose your destiny:",
            "  1. Door A",
            "  2. Door B",
            "  3. Door C",
            "Hit Enter to select the door you want.",
        ]
        # Without the full footer string, TUI path doesn't fire.
        # Legacy path might pick up the header + 3 options — but the
        # trailing "Hit Enter..." line isn't an option, breaks the
        # contiguous-numbered-bottom requirement.
        p = detect_choice_prompt(lines)
        # Either the legacy path bails (preferred) or it caught the
        # 3 options — but it MUST NOT report input_mode=tui_select.
        if p is not None:
            assert p.input_mode != "tui_select"

    def test_partial_footer_mid_render(self):
        # Mid-render footer (Claude streamed "Enter to sele" and the
        # next chunk hasn't landed) — must NOT fire. Wait for the
        # next poll.
        lines = [
            "Pick one:",
            "  1. First",
            "  2. Second",
            "  3. Third",
            "Enter to sele",  # truncated
        ]
        p = detect_choice_prompt(lines)
        # TUI shouldn't fire (footer regex doesn't match). Legacy
        # might fire on the 3 numbered lines + header — that's OK;
        # the TUI path is the one we're testing here.
        if p is not None:
            assert p.input_mode != "tui_select"

    def test_footer_present_but_options_have_ansi(self):
        # Mid-render ANSI inside an option means Claude is still
        # streaming — bail (don't lock in a half-built option set).
        lines = [
            "Pick one:",
            "  1. First",
            "  2. Second \x1b[31mhalf\x1b[0m",
            "  3. Third",
            _TUI_FOOTER,
        ]
        assert detect_choice_prompt(lines) is None

    def test_footer_with_only_two_options(self):
        lines = [
            "Pick:",
            "  1. First",
            "  2. Second",
            _TUI_FOOTER,
        ]
        assert detect_choice_prompt(lines) is None

    def test_footer_present_but_non_contiguous_numbering(self):
        # Detector observed "1. ... 2. ... 4. ..." — option 3 was
        # somehow skipped (render bug, cursor on a wrong row). Bail
        # rather than commit on a misaligned index.
        lines = [
            "Pick:",
            "  1. First",
            "  2. Second",
            "  4. Fourth (3 missing)",
            _TUI_FOOTER,
        ]
        assert detect_choice_prompt(lines) is None

    def test_footer_inside_fenced_code_block_above_options(self):
        # Code fence between the title and options — options are
        # inside a fenced block (not a real prompt). Bail.
        lines = [
            "Pick one:",
            "```",
            "  1. First",
            "  2. Second",
            "  3. Third",
            _TUI_FOOTER,
        ]
        assert detect_choice_prompt(lines) is None


class TestTuiSelectPromptSignatureStability:

    def test_focus_shift_keeps_signature(self):
        # User pressed Down on the real keyboard between polls —
        # cursor moved from row 0 to row 1. Same prompt, same options,
        # focused_index changed. Signature MUST be stable so we don't
        # broadcast choice_prompt again (which would bounce the
        # iOS lens state).
        lines_focus_0 = [
            "Pick:",
            ") 1. A", "  2. B", "  3. C",
            _TUI_FOOTER,
        ]
        lines_focus_1 = [
            "Pick:",
            "  1. A", ") 2. B", "  3. C",
            _TUI_FOOTER,
        ]
        p0 = detect_choice_prompt(lines_focus_0)
        p1 = detect_choice_prompt(lines_focus_1)
        assert p0 is not None and p1 is not None
        assert p0.focused_index == 0
        assert p1.focused_index == 1
        assert p0.signature == p1.signature

    def test_option_change_breaks_signature(self):
        # Claude moved to a new prompt with different options —
        # signature MUST change so the iOS lens replaces the old
        # banner.
        lines_a = [
            "Pick:",
            "  1. A", "  2. B", "  3. C",
            _TUI_FOOTER,
        ]
        lines_b = [
            "Pick:",
            "  1. X", "  2. Y", "  3. Z",
            _TUI_FOOTER,
        ]
        pa = detect_choice_prompt(lines_a)
        pb = detect_choice_prompt(lines_b)
        assert pa is not None and pb is not None
        assert pa.signature != pb.signature


class TestLegacyPathStillUsesNumberMode:
    """Pre-existing test cases must keep input_mode=number so the bridge
    commit path picks the "<N>\\n" literal synthesis (not arrow nav)."""

    def test_legacy_question_header(self):
        lines = [
            "Which approach?",
            "1. Alpha", "2. Beta", "3. Gamma",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "number"
        assert p.focused_index is None


# ---------------------------------------------------------------------------
# Plan-mode (ExitPlanMode) detection — separate footer anchor, same widget
# ---------------------------------------------------------------------------


class TestPlanModeDetection:
    """Claude Code's plan-approval prompt renders the same numbered+cursor
    widget as the standard TUI select, but with a different footer hint
    ("shift+tab to approve with this feedback") and a trailing editor-hint
    line ("ctrl-g to edit in Vim · ~/.claude/plans/..."). Detection must
    surface the same ChoicePrompt shape with input_mode="tui_select" so the
    lens commit path (arrow + Enter) works unchanged."""

    def test_screenshot_fixture(self):
        # Reproduces the screenshot from the user report: 4 plan-approval
        # options, cursor on option 1, plan-mode footer, editor-hint trailer.
        lines = [
            "Claude has written up a plan and is ready to execute. Would you like to proceed?",
            "",
            ") 1. Yes, and bypass permissions",
            "  2. Yes, manually approve edits",
            "  3. No, refine with Ultraplan on Claude Code on the web",
            "  4. Tell Claude what to change",
            "     shift+tab to approve with this feedback",
            "",
            "ctrl-g to edit in  Vim   · ~/.claude/plans/image-14-so-if-async-honey.md",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"
        assert p.focused_index == 0
        assert len(p.options) == 4
        assert p.title.startswith("Claude has written up a plan")
        assert p.options[0] == "Yes, and bypass permissions"
        assert p.options[-1] == "Tell Claude what to change"

    def test_prose_about_shift_tab_not_detected_as_tui_select(self):
        # Plan-mode footer substring inside prose, with no real widget on
        # screen. TUI path must not fire on this; if anything detects, it
        # must be the legacy number-mode path (harmless commit semantics).
        lines = [
            "To approve a plan, you press shift+tab to approve at the footer.",
            "",
            "Here are some options I considered:",
            "1. Refactor",
            "2. Add a test",
        ]
        p = detect_choice_prompt(lines)
        if p is not None:
            assert p.input_mode != "tui_select"

    def test_prose_mid_sentence_shift_tab_at_bottom_not_detected(self):
        # Stronger version of the precision check: numbered list ABOVE
        # and prose mentioning "shift+tab to approve" mid-sentence at
        # the BOTTOM. Without line-start anchoring on _PLAN_FOOTER_RE
        # this would false-fire as input_mode="tui_select". The anchor
        # requires the line to BEGIN with "shift+tab to approve" (after
        # .strip()), which prose-with-leading-word avoids.
        lines = [
            "Here are options about keyboard shortcuts:",
            "  1. Refactor",
            "  2. Add a test",
            "  3. Skip",
            "Note: hit shift+tab to approve via keyboard.",
        ]
        p = detect_choice_prompt(lines)
        if p is not None:
            assert p.input_mode != "tui_select"

    def test_plan_footer_with_too_few_options_returns_none(self):
        # Plan-mode footer with only 2 options — below _MIN_OPTIONS=3.
        lines = [
            "Plan?",
            "  1. Yes",
            "  2. No",
            "     shift+tab to approve with this feedback",
        ]
        assert detect_choice_prompt(lines) is None

    def test_plan_mode_ansi_mid_render_returns_none(self):
        # Mid-render ANSI inside an option — wait for the next poll tick
        # rather than committing on a half-built option set.
        lines = [
            "Claude has written up a plan. Would you like to proceed?",
            "  1. Yes, and bypass permissions",
            "  2. Yes, manually approve \x1b[31medits\x1b[0m",
            "  3. No, refine",
            "  4. Tell Claude what to change",
            "     shift+tab to approve with this feedback",
        ]
        assert detect_choice_prompt(lines) is None

    def test_plan_mode_invalidated_when_content_appears_below(self):
        # User accepted the plan; Claude printed a response line below the
        # widget. The option block is no longer at the bottom — the
        # previously-emitted ChoicePrompt must invalidate.
        lines_v1 = [
            "Claude has written up a plan and is ready to execute. Would you like to proceed?",
            "",
            ") 1. Yes, and bypass permissions",
            "  2. Yes, manually approve edits",
            "  3. No, refine with Ultraplan on Claude Code on the web",
            "  4. Tell Claude what to change",
            "     shift+tab to approve with this feedback",
            "",
            "ctrl-g to edit in  Vim   · ~/.claude/plans/some-file.md",
        ]
        prev = detect_choice_prompt(lines_v1)
        assert prev is not None
        lines_v2 = lines_v1 + ["", "Working on the changes now..."]
        assert is_invalidated(prev, lines_v2)


class TestAskUserQuestionWidget:
    """Claude Code's ``AskUserQuestion`` widget renders a tab header
    (``← □ Variant design  □ Win metric  ✓ Submit  →``), a question, and
    a numbered list where each option carries a multi-line INDENTED
    description, plus meta-options ("Type something." / "Chat about
    this") separated from the main list by a blank row. Footer is the
    standard "Enter to select · Tab/Arrow keys to navigate · Esc to
    cancel". This is the shape from the 2026-05 user bug report where
    the lens showed no options.

    The hard part is the indented description prose: a wrapped line that
    happens to end in ":" or "?" must NOT be mistaken for the title
    boundary — doing so truncated the upward scan and dropped every
    option above it, so the prompt silently failed to surface.
    """

    FOOTER = "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"

    def _widget(
        self,
        *,
        desc1_tail: str = "interrupt-overlay vs passive inline form.",
        gap_before_meta: int = 1,
    ) -> list[str]:
        return [
            "Let me get your calls on the two forks that change the build:",
            "",
            "─────────────────────────────────────────",
            "Planning: /home/user/.claude/plans/ancient-flurry.md",
            "─────────────────────────────────────────",
            "← □ Variant design   □ Win metric   ✓ Submit   →",
            "",
            "How should the two A/B variants differ on the homepage? "
            "(This is what 'popup vs newsletter' means in code.)",
            "",
            "❯ 1. Popup vs inline newsletter",
            "     Variant A: the bottom-right free-trial popup (→ /sign-up). Variant B: NO popup, inline",
            "     email-capture/newsletter block embedded in the page body. Tests two capture",
            "     mechanisms — " + desc1_tail,
            "  2. Same popup, two offers",
            "     Both variants get the bottom-right popup, but A pitches 'Start free trial' and B",
            "     pitches 'Get the free playbook' (→ email capture). Same format, only the offer changes.",
            "  3. Popup vs no popup",
            "     Variant A: free-trial popup shows. Variant B: control, no popup at all.",
            "  4. Type something.",
            *([""] * gap_before_meta),
            "5. Chat about this",
            "",
            self.FOOTER,
        ]

    def test_screenshot_fixture_detects_all_options(self):
        p = detect_choice_prompt(self._widget())
        assert p is not None
        assert p.input_mode == "tui_select"
        assert p.focused_index == 0  # cursor "❯" on option 1
        assert p.options == [
            "Popup vs inline newsletter",
            "Same popup, two offers",
            "Popup vs no popup",
            "Type something.",
            "Chat about this",
        ]
        assert p.title.startswith("How should the two A/B variants")

    def test_description_line_ending_in_colon_does_not_truncate(self):
        # Regression: a wrapped description ending in ":" used to be
        # treated as the title boundary, dropping every option above it.
        p = detect_choice_prompt(self._widget(desc1_tail="now compare against Variant B:"))
        assert p is not None
        assert len(p.options) == 5

    def test_description_line_ending_in_question_does_not_truncate(self):
        # Same regression for a "?"-terminated wrapped description line.
        p = detect_choice_prompt(self._widget(desc1_tail="does this make sense for the test?"))
        assert p is not None
        assert len(p.options) == 5

    def test_two_blank_rows_before_meta_option_tolerated(self):
        # Some renders put two blank rows between the main list and the
        # "Chat about this" meta-option. The footer anchor guarantees a
        # real widget, so a 2-row gap must not bail the scan.
        p = detect_choice_prompt(self._widget(gap_before_meta=2))
        assert p is not None
        assert len(p.options) == 5

    def test_three_blank_rows_bails(self):
        # A 3-row gap is a genuine paragraph break / top of prompt:
        # collection stops, leaving only the meta-option, which is below
        # the 3-option minimum — so no detection.
        assert detect_choice_prompt(self._widget(gap_before_meta=3)) is None


class TestHasSelectFooter:
    """``has_select_footer`` powers the activity-poll diagnostic that
    distinguishes "no widget on screen" from "widget on screen but we
    failed to parse it" (the latter is logged as a warning)."""

    def test_true_when_tui_footer_present(self):
        assert has_select_footer([
            "  1. a", "  2. b", "  3. c",
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
        ])

    def test_true_when_arrow_navigation_hint_footer_present(self):
        # The "↑/↓ to navigate" footer variant must also count as a
        # select-widget footer for the parse-fail diagnostic.
        assert has_select_footer([
            "  1. a", "  2. b", "  3. c",
            "Enter to select · ↑/↓ to navigate · Esc to cancel",
        ])

    def test_true_when_plan_footer_present(self):
        assert has_select_footer([
            "  1. yes", "  2. no",
            "     shift+tab to approve with this feedback",
        ])

    def test_false_on_plain_prose(self):
        assert not has_select_footer([
            "Here is a summary of the work:",
            "1. did a thing", "2. did another",
        ])


# ---------------------------------------------------------------------------
# Multi-question AskUserQuestion form
# ---------------------------------------------------------------------------
#
# Fixtures reproduce screens captured by driving a LIVE Claude Code
# v2.1.148 widget with tmux send-keys (the Phase 0 spike). The structural
# signal is the tab bar (← … ✔ Submit →), NOT the footer — verified that
# the footer text varies across builds.


_SINGLE_SCREEN = [
    "←  ☐ Color  ☐ Features  ☐ Deploy  ✔ Submit  →",
    "Which accent color?",
    "❯ 1. Red",
    "     Use red as the accent color.",
    "  2. Green",
    "     Use green as the accent color.",
    "  3. Blue",
    "     Use blue as the accent color.",
    "  4. Type something.",
    "────────────────────────────",
    "  5. Chat about this",
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
]

_MULTI_SCREEN = [
    "←  ☒ Color  ☒ Features  ☐ Deploy  ✔ Submit  →",
    "Which features should we include?",
    "  1. [✔] Auth",
    "  User authentication and login.",
    "  2. [✔] Search",
    "  Search functionality across content.",
    "  3. [ ] Export",
    "  Export data to external formats.",
    "  4. [ ] Notifications",
    "  Push or in-app notifications.",
    "❯ 5. [ ] Type something",
    "     Next",
    "────────────────",
    "  6. Chat about this",
    "Enter to select · Tab/Arrow keys to navigate · ctrl+g to edit in Vim · Esc to cancel",
]

_REVIEW_SCREEN = [
    "←  ☒ Color  ☒ Features  ☒ Deploy  ✔ Submit  →",
    "Review your answers",
    " ● Which accent color?",
    "   → Green",
    " ● Which features should we include?",
    "   → Auth, Search",
    " ● Deploy after merge?",
    "   → Yes",
    "Ready to submit your answers?",
    "❯ 1. Submit answers",
    "  2. Cancel",
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
]

# The user's bug-report screenshot: a single-select question with a
# side-by-side ASCII preview diagram glued onto the option rows, plus the
# "n to add notes" / "Tab to switch questions" footer variant.
_DIAGRAM_SCREEN = [
    "←  ☐ Motion model  ☐ Label text  ☐ Scope  ✔ Submit  →",
    "How should a tool call animate along the wire?",
    "❯ 1. One chip, direction =        ┌───────────────────────────────────────────────┐",
    "    read/write                    │ WORKER ───[ post ]──▶ SLACK     (write)       │",
    "  2. Round-trip per call          │ WORKER ◀──[ list channels ]─── SLACK   (read) │",
    "                                  └───────────────────────────────────────────────┘",
    "                                  Notes: press n to add notes",
    "────────────────",
    "  Chat about this",
    "Enter to select · ↑/↓ to navigate · n to add notes · Tab to switch questions · Esc to cancel",
]


class TestMultiQuestionFormDetection:

    def test_single_select_screen(self):
        f = detect_question_form(_SINGLE_SCREEN)
        assert f is not None
        assert f.select_mode == "single"
        assert f.title == "Which accent color?"
        assert [(q.label, q.answered) for q in f.questions] == [
            ("Color", False), ("Features", False), ("Deploy", False)
        ]
        assert f.answered_count == 0
        assert f.focused_index == 0  # cursor ❯ on Red
        kinds = [(r.kind, r.text) for r in f.rows]
        assert kinds == [
            ("choice", "Red"),
            ("choice", "Green"),
            ("choice", "Blue"),
            ("free_text", "Type something."),
            ("meta", "Chat about this"),
        ]

    def test_multi_select_screen_checkboxes(self):
        f = detect_question_form(_MULTI_SCREEN)
        assert f is not None
        assert f.select_mode == "multi"
        assert f.title == "Which features should we include?"
        # ☒ on Color + Features (≥1 box checked marks the active tab too).
        assert f.answered_count == 2
        checks = [(r.text, r.checked) for r in f.rows if r.kind == "choice"]
        assert checks == [
            ("Auth", True), ("Search", True),
            ("Export", False), ("Notifications", False),
        ]
        # Free-text + Next advance affordance + meta are all navigable rows.
        assert [r.kind for r in f.rows] == [
            "choice", "choice", "choice", "choice", "free_text", "next", "meta"
        ]
        assert f.focused_index == 4  # cursor on "Type something"

    def test_review_screen(self):
        f = detect_question_form(_REVIEW_SCREEN)
        assert f is not None
        assert f.select_mode == "review"
        assert f.answered_count == 3  # all questions answered
        assert [r.kind for r in f.rows] == ["submit", "cancel"]
        assert f.rows[0].text == "Submit answers"
        assert f.focused_index == 0

    def test_side_by_side_diagram_is_stripped(self):
        # The preview diagram glued onto the option rows must be cut so the
        # lens shows the option label, not box-drawing noise.
        f = detect_question_form(_DIAGRAM_SCREEN)
        assert f is not None
        assert f.select_mode == "single"
        assert f.title == "How should a tool call animate along the wire?"
        choices = [r.text for r in f.rows if r.kind == "choice"]
        assert choices == ["One chip, direction =", "Round-trip per call"]
        # No box-drawing chars leaked into any option label.
        assert all("─" not in r.text and "│" not in r.text for r in f.rows)

    def test_answer_rows_excludes_control_rows(self):
        f = detect_question_form(_MULTI_SCREEN)
        assert f is not None
        # answer_rows = user-facing picks only (choices + free-text), no
        # Next/meta control rows.
        assert [r.text for r in f.answer_rows] == [
            "Auth", "Search", "Export", "Notifications", "Type something"
        ]

    def test_two_options_below_legacy_floor_still_detected(self):
        # The single-prompt path requires ≥3 options; a form question may
        # have only 2. The tab-bar anchor lets the form path accept it.
        lines = [
            "←  ☐ Deploy  ✔ Submit  →",
            "Deploy after merge?",
            "❯ 1. Yes",
            "  2. No",
            "  3. Type something.",
            "  Chat about this",
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
        ]
        f = detect_question_form(lines)
        assert f is not None
        choices = [r.text for r in f.rows if r.kind == "choice"]
        assert choices == ["Yes", "No"]


class TestMultiQuestionFormRejection:

    def test_plain_choice_prompt_is_not_a_form(self):
        # No tab bar → not a form. The single-prompt path handles it.
        assert detect_question_form([
            "Which approach?", "1. Alpha", "2. Beta", "3. Gamma",
        ]) is None

    def test_tab_bar_lookalike_in_prose_without_checkbox(self):
        # A line with arrows + "Submit" but NO checkbox glyph must not fire.
        assert detect_question_form([
            "← Press Submit to continue →",
            "1. A", "2. B", "3. C",
        ]) is None

    def test_form_with_no_rows_returns_none(self):
        # Tab bar present but no option rows parsed (mid-render / empty).
        assert detect_question_form([
            "←  ☐ Color  ✔ Submit  →",
            "Which accent color?",
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
        ]) is None

    def test_ansi_mid_render_bails(self):
        lines = list(_SINGLE_SCREEN)
        lines[2] = "❯ 1. \x1b[31mRed\x1b[0m"
        assert detect_question_form(lines) is None


class TestMultiQuestionFormSignature:

    def test_signature_stable_across_cursor_move(self):
        moved = list(_SINGLE_SCREEN)
        moved[2] = "  1. Red"
        moved[4] = "❯ 2. Green"
        a = detect_question_form(_SINGLE_SCREEN)
        b = detect_question_form(moved)
        assert a.focused_index == 0 and b.focused_index == 1
        assert a.signature == b.signature  # cursor excluded from hash

    def test_signature_changes_on_checkbox_toggle(self):
        toggled = list(_MULTI_SCREEN)
        toggled[6] = "  3. [✔] Export"  # Export now checked
        a = detect_question_form(_MULTI_SCREEN)
        b = detect_question_form(toggled)
        assert a.signature != b.signature  # lens must re-render the check

    def test_form_id_stable_across_screens_of_same_form(self):
        # Same questions (tab labels), different active screen → same
        # form_id (iOS knows it's the same form advancing), different sig.
        a = detect_question_form(_SINGLE_SCREEN)   # Color/Features/Deploy
        b = detect_question_form(_REVIEW_SCREEN)   # Color/Features/Deploy
        assert a.form_id == b.form_id
        assert a.signature != b.signature

    def test_is_form_invalidated(self):
        f = detect_question_form(_SINGLE_SCREEN)
        # Same form, advanced screen → NOT invalidated (normal step).
        assert not is_form_invalidated(f, _REVIEW_SCREEN)
        # Form gone → invalidated.
        assert is_form_invalidated(f, ["just some prose", "nothing here"])
        # Different form (different tabs) → invalidated.
        other = [
            "←  ☐ Alpha  ☐ Beta  ✔ Submit  →",
            "Pick alpha or beta?",
            "❯ 1. Alpha", "  2. Beta", "  Chat about this",
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
        ]
        assert is_form_invalidated(f, other)


class TestMultiQuestionFormDataclass:

    def test_signature_and_form_id_autocomputed(self):
        f = detect_question_form(_SINGLE_SCREEN)
        assert len(f.signature) == 16
        assert len(f.form_id) == 12

    def test_explicit_signature_preserved(self):
        f = MultiQuestionForm(
            questions=[], title="t", select_mode="single", rows=[],
            signature="fixed", form_id="fid",
        )
        assert f.signature == "fixed"
        assert f.form_id == "fid"


# ---------------------------------------------------------------------------
# Body capture for the glasses-lens "read" view
# ---------------------------------------------------------------------------


class TestPromptBodyCapture:
    """``body`` carries the full plan/question preamble (the text above the
    options that the title-only scan drops) so the lens can render a
    full-screen "read" view. It must stay OUT of the signature so a body that
    streams in doesn't re-broadcast the prompt every poll.
    """

    def test_tui_plan_body_captures_preamble(self):
        lines = [
            "Here is the multi-step plan:",
            "Step 1: refactor the parser.",
            "Step 2: add tests.",
            "Step 3: ship it.",
            "",
            ") 1. Yes, proceed",
            "  2. No, keep planning",
            "  3. Tell Claude what to change",
            "     shift+tab to approve with this feedback",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert p.input_mode == "tui_select"
        # Plan body (lines above the options) is captured — not just the title.
        assert "Step 1: refactor the parser." in p.body
        assert "Step 3: ship it." in p.body
        # Option labels are NOT part of the body.
        assert not any("Yes, proceed" in line for line in p.body)

    def test_legacy_body_captures_preamble(self):
        lines = [
            "Looking at the diff, I see two paths.",
            "Both have tradeoffs worth weighing.",
            "Which approach do you want?",
            "1. The safe one",
            "2. The fast one",
            "3. Neither",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert "Looking at the diff, I see two paths." in p.body
        assert "Both have tradeoffs worth weighing." in p.body

    def test_form_body_captures_question_and_descriptions(self):
        f = detect_question_form(_SINGLE_SCREEN)
        assert f is not None
        # The active question text and each option's description line are
        # captured for the read view (the row parse drops the descriptions).
        assert any("Which accent color?" in line for line in f.body)
        assert any("Use red as the accent color." in line for line in f.body)
        assert any("Use blue as the accent color." in line for line in f.body)

    def test_choice_body_excluded_from_signature(self):
        a = ChoicePrompt(title="t", options=["a", "b", "c"])
        b = ChoicePrompt(title="t", options=["a", "b", "c"], body=["totally", "different"])
        assert a.signature == b.signature

    def test_form_body_excluded_from_signature_and_form_id(self):
        from bridge.choice_detector import FormRow, QuestionTab
        base = dict(
            questions=[QuestionTab(label="A", answered=False)],
            title="Q?",
            select_mode="single",
            rows=[FormRow(text="opt1", kind="choice")],
        )
        f1 = MultiQuestionForm(**base)
        f2 = MultiQuestionForm(**base, body=["extra", "description"])
        assert f1.signature == f2.signature
        assert f1.form_id == f2.form_id

    def test_body_is_bounded(self):
        from bridge.choice_detector import _MAX_BODY_LINES
        preamble = [f"plan line {i}" for i in range(_MAX_BODY_LINES + 50)]
        lines = preamble + [
            ") 1. Yes",
            "  2. No",
            "  3. Maybe",
            "     shift+tab to approve with this feedback",
        ]
        p = detect_choice_prompt(lines)
        assert p is not None
        assert len(p.body) <= _MAX_BODY_LINES
