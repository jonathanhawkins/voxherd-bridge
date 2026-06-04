"""Header-gated numbered-choice detection for Claude Code transcripts.

When Claude prints a question and then a numbered list of options at the
bottom of its pane, surface it to the lens as an interactive choice card.
The user picks via tap-cycle + long-press-confirm on the glasses; we send
the selected number back through ``terminal_send_keys`` to the tmux pane.

Two detection paths run in sequence:

  1. ``_detect_tui_select_prompt`` — anchored on Claude Code's stateful
     select-prompt widget footer literal ("Enter to select · … to
     navigate · Esc to cancel"; the middle hint is "Tab/Arrow keys" or
     "↑/↓" depending on build) OR the ExitPlanMode footer hint
     ("shift+tab to approve with this feedback"). High precision for the
     first anchor because the footer string never appears in user prose;
     the plan-mode anchor is weaker but still gated on the same widget
     shape (numbered options + cursor glyph). Handles multi-line option
     bodies and the blank-line separators the widget renders. Selected
     by ``input_mode == "tui_select"`` — commit goes through arrow-key
     navigation, not number quick-pick.
  2. ``_detect_legacy_choice_prompt`` — the original inline ``1. 2. 3.``
     numbered-text path, header-gated by recognized phrases. Commit goes
     through ``"<N>\\n"`` literal text.

The hard problem in both paths is false positives. Numbered lists are
also how Claude formats pros/cons, summaries, plans, and step-by-step
breakdowns — long-press in those contexts would send "1\\n" into a
regular conversation, polluting it. We therefore *only* fire on a
recognized prompt shape:

  - The numbered block must be the LAST non-chrome content in the pane
    (TUI path tolerates the footer line below it).
  - For the legacy path: the line(s) immediately above must end with
    ``?`` OR match a known decision-phrase pattern.
  - For the TUI path: the literal footer must match exactly (partial
    renders bail).
  - None of the numbered lines may contain ANSI escape sequences that
    indicate the model is still streaming.

These rules trade recall for precision — better to miss a real prompt
the user can still respond to verbally than to commit a spurious "1"
into their conversation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from bridge.activity import _is_status_bar_line


# A numbered option row: "1. ...", "2) ...", " 3. ..."
_OPTION_RE = re.compile(r"^\s*(?P<num>\d+)[.)]\s+(?P<text>.+?)\s*$")

# TUI select-prompt anchor: Claude Code prints this footer literally
# beneath its select widget. The string never appears in user prose,
# tool output, or Markdown — using it as the primary signal is the
# precision win that lets us tolerate multi-line option bodies and
# blank-line separators inside the option block.
#
# The middle navigation hint VARIES across Claude Code builds/widgets:
#   - "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"
#   - "Enter to select · ↑/↓ to navigate · Esc to cancel"
# Both share the "to navigate" segment, so we anchor on the stable
# bookends ("Enter to select" … "Esc to cancel") and only require a
# generic "to navigate" between them. Requiring all three phrases on a
# SINGLE line keeps precision high — prose never strings them together.
_TUI_FOOTER_RE = re.compile(
    r"Enter to select.*to navigate.*Esc to cancel",
    re.IGNORECASE,
)

# Plan-mode footer: Claude Code's ExitPlanMode prompt prints this hint
# instead of the standard "Enter to select" string. Same widget shape
# (numbered options + cursor glyph), different footer literal.
#
# Anchored to line start (after .strip()) so prose like "Press shift+tab
# to approve" mid-sentence does NOT match — the plan-mode footer always
# begins with "shift+tab" on its own line. Kept as a separate constant
# from _TUI_FOOTER_RE because the precision-over-prose argument in the
# docstring next to _TUI_FOOTER_RE is weaker for this anchor (shorter
# substring, more reproducible in keybinding prose).
_PLAN_FOOTER_RE = re.compile(
    r"^shift\+tab to approve",
    re.IGNORECASE,
)

# Cursor glyphs the TUI widget paints at column 0 of the focused row.
# Different Claude Code builds have shipped different chars — support
# all of them so a font-substitution quirk on one machine doesn't
# break focus extraction.
_TUI_CURSOR_GLYPHS = ")›▶❯"  # ')', '›', '▶', '❯'

# A TUI option row: leading whitespace, optional cursor glyph, more
# whitespace, ``N.`` (TUI mode uses ``.`` not ``)``), space, text.
_TUI_OPTION_RE = re.compile(
    r"^\s*(?P<cursor>[" + _TUI_CURSOR_GLYPHS + r"])?\s*"
    r"(?P<num>\d+)\.\s+(?P<text>.+?)\s*$"
)

# Decision-phrase headers. Case-insensitive substring match. These are the
# unambiguous "I want you to pick one of the following" cues — anything
# else has to lean on a trailing ``?`` to be considered a real prompt.
_HEADER_PHRASES = (
    "choose an option",
    "choose one",
    "which would you",
    "which one",
    "which approach",
    "which do you",
    "pick a plan",
    "pick an option",
    "pick one",
    "how should i proceed",
    "how should we proceed",
    "how would you like",
    "select an option",
    "select one",
)

# A single ANSI CSI escape sequence. If we see one mid-option, the option
# is still being rendered — treat as unstable and don't fire yet.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Minimum + maximum option counts we'll surface to the lens. 3 is the
# threshold below which a numbered list is too sparse to obviously be a
# choice prompt (lots of prose has "1. foo, 2. bar"). 7 caps the lens
# render — beyond that we'd have to scroll, which we don't support yet.
_MIN_OPTIONS = 3
_MAX_OPTIONS = 7

# Cap on how many preamble lines we carry as the prompt ``body`` (the
# plan/question text the glasses "read" view renders). Bounds the WS
# payload; we keep the LAST N lines (nearest the options) since a deep
# tmux capture already returns at most its scrollback tail.
_MAX_BODY_LINES = 200


def _extract_body(lines: list[str], before_idx: int) -> list[str]:
    """Collect non-chrome content lines above the option block — the
    prompt's full body (plan / question preamble) for the lens read view.

    Drops blanks, status-bar chrome, and mid-render ANSI rows. Bounded to
    the most recent ``_MAX_BODY_LINES`` so a long scrollback can't bloat
    the broadcast.
    """
    body: list[str] = []
    for k in range(0, max(0, before_idx)):
        raw = lines[k]
        stripped = raw.strip()
        if not stripped:
            continue
        if _is_status_bar_line(stripped):
            continue
        if _ANSI_CSI_RE.search(raw):
            continue
        body.append(stripped)
    return body[-_MAX_BODY_LINES:]


@dataclass
class ChoicePrompt:
    """A detected numbered-choice prompt at the bottom of a tmux capture."""

    title: str
    options: list[str]
    # How the user commits a selection back to the TTY.
    #   - "number": the legacy inline prompt accepts ``"N\n"`` literally
    #     (Claude Code's stock "Choose an option:" path).
    #   - "tui_select": the stateful select-prompt widget needs arrow
    #     navigation (``Down`` × delta + ``Enter``) because the footer
    #     advertises Tab/Arrow + Enter and digit keys may no-op.
    # Excluded from ``signature`` — input_mode is implied by the prompt
    # shape, and including it would re-trigger broadcasts on any future
    # mode rename.
    input_mode: str = "number"
    # For TUI prompts, the 0-based index of the row the on-screen cursor
    # currently sits on. Used server-side at commit time to compute the
    # arrow-key delta. None for legacy prompts (no cursor to read).
    # Excluded from ``signature`` so cursor movement (the user pressing
    # arrows on the real keyboard, or Claude Code re-rendering the same
    # widget) does NOT invalidate the prompt or re-broadcast.
    focused_index: int | None = None
    # Stable identifier for this prompt instance — used to deduplicate
    # repeat detections of the same on-screen prompt and to detect when
    # the prompt has been replaced by a new one. Computed from title +
    # options ONLY; see field comments above.
    signature: str = field(default="")
    # Full preamble text (the plan / question body above the options) for
    # the glasses "read" view. EXCLUDED from ``signature`` on purpose: the
    # body can stream in / change while the same prompt stays on screen, and
    # re-keying the signature on it would re-broadcast the prompt every poll.
    body: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.signature:
            joined = f"{self.title}\n" + "\n".join(self.options)
            self.signature = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def detect_choice_prompt(lines: list[str]) -> ChoicePrompt | None:
    """Return a ChoicePrompt when the tail of ``lines`` matches our shape.

    Tries the TUI select-prompt path first (anchored on the footer
    literal — the highest-precision signal we have). Falls back to the
    legacy header-gated path when the footer isn't present.

    Returns ``None`` when neither path matches — bridge falls back to
    no-op (lens shows no choice banner). Caller is responsible for
    deduplicating repeated detections via ``ChoicePrompt.signature``.
    """
    if len(lines) < _MIN_OPTIONS + 1:
        return None
    tui = _detect_tui_select_prompt(lines)
    if tui is not None:
        return tui
    return _detect_legacy_choice_prompt(lines)


def has_select_footer(lines: list[str]) -> bool:
    """True when a select-widget footer literal is visible near the
    bottom of ``lines``.

    Used by the activity poll for diagnostics: it distinguishes "no
    widget on screen" (``detect_choice_prompt`` returned ``None``
    because there's nothing to detect) from "a widget IS on screen but
    we failed to parse its options" (returned ``None`` despite the
    footer being present). The latter is a real bug worth logging; the
    former is the common no-op case.
    """
    for line in reversed(lines[-15:]):
        stripped = line.strip()
        if not stripped:
            continue
        if _TUI_FOOTER_RE.search(stripped) or _PLAN_FOOTER_RE.search(stripped):
            return True
    return False


def _detect_tui_select_prompt(lines: list[str]) -> ChoicePrompt | None:
    """Detect the stateful select-prompt widget anchored on its footer.

    Algorithm:
      1. Walk bottom-up past blanks and ``_is_status_bar_line`` chrome
         to find the first non-chrome content row. If that row matches
         ``_TUI_FOOTER_RE``, we have a TUI prompt; otherwise bail.
      2. Walk up from the row above the footer collecting option rows
         that match ``_TUI_OPTION_RE``. Indented continuation lines (the
         description body under each option) are skipped regardless of
         trailing punctuation; only a NON-indented "?"/":"-terminated
         line is treated as the title boundary. Up to two consecutive
         blank rows are tolerated (the widget's meta-option separator);
         three consecutive blanks bail (paragraph break / top of prompt).
      3. Verify the collected options number sequentially from 1; if
         numbering is non-contiguous or the count is below
         ``_MIN_OPTIONS``, bail.
      4. Extract the focused index from the cursor glyph (if present)
         on any option row.
      5. Look 1–4 non-blank lines above the topmost option for a title
         (any text — TUI footer is the anchor, title is optional).
    """
    # 1. Locate the footer. Two strategies, in order of confidence:
    #    a) Full footer literal on one line — the high-precision case.
    #    b) "Enter to select" prefix on one line AND "Esc to cancel"
    #       within the next 2 lines below it — covers the wrap-to-two-
    #       lines case in narrow terminals (Claude Code wraps the
    #       footer when the pane width is below ~70 cols).
    footer_idx = None
    # Strategy (a): scan up from the bottom past blanks + chrome, check
    # the first non-chrome row.
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if _is_status_bar_line(stripped):
            continue
        if _TUI_FOOTER_RE.search(stripped) or _PLAN_FOOTER_RE.search(stripped):
            footer_idx = i
            break
        # Strategy (b): the bottom row contains only the tail "Esc to
        # cancel"; look upward 1–2 lines for the "Enter to select"
        # prefix.
        if re.search(r"Esc to cancel", stripped, re.IGNORECASE):
            for j in (i - 1, i - 2):
                if j < 0:
                    break
                head = lines[j].strip()
                if re.search(r"Enter to select", head, re.IGNORECASE):
                    # Treat the PREFIX row as the anchor — option scan
                    # walks up from there, leaving the wrapped tail
                    # safely below.
                    footer_idx = j
                    break
        # Whether or not we matched, the first non-chrome row at the
        # bottom is the decision point — if it isn't a footer (in
        # either form), this isn't a TUI prompt.
        break
    if footer_idx is None:
        return None

    # 2. Walk up from the line above the footer collecting options.
    options: list[tuple[int, int, str, bool]] = []  # (idx, num, text, has_cursor)
    blank_streak = 0
    i = footer_idx - 1
    while i >= 0 and len(options) < _MAX_OPTIONS:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            blank_streak += 1
            # The widget separates its meta-options ("Type something."
            # / "Chat about this") from the main list with one — and on
            # some renders two — blank rows. The footer literal already
            # proved we're inside a real select widget (prose can't
            # reach this loop), so tolerating a 2-row gap costs no
            # precision. Bail only on a 3-row gap (the real paragraph
            # break / top-of-prompt signal).
            if blank_streak >= 3:
                break
            i -= 1
            continue
        blank_streak = 0

        # Code-fence opener with options below means we're inside a
        # fenced block — not a real prompt.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            return None

        # Mid-render — wait for the next poll tick rather than emit
        # a half-built option list.
        if _ANSI_CSI_RE.search(line):
            return None

        m = _TUI_OPTION_RE.match(line)
        if m:
            num = int(m.group("num"))
            text = m.group("text").strip()
            has_cursor = bool(m.group("cursor"))
            options.append((i, num, text, has_cursor))
            i -= 1
            continue

        # Non-numbered, non-blank line above an option. It's either an
        # indented continuation (the description body Claude renders
        # under each option) or the title/question above the block.
        #
        # Only a NON-indented "?"/":"-terminated line is the title
        # boundary. Option descriptions are free prose that routinely
        # end a wrapped line in ":" ("...Variant B:") or "?" — treating
        # that as the boundary truncates the scan and drops every option
        # above it, frequently dipping below _MIN_OPTIONS so the whole
        # prompt silently fails to surface. Skip indented continuations
        # regardless of trailing punctuation; the indent test mirrors
        # the title scan below (5-space / tab alignment under "N. ").
        is_continuation = line.startswith(("     ", "\t"))
        if (
            options
            and not is_continuation
            and (stripped.endswith("?") or stripped.endswith(":"))
        ):
            break
        if options:
            i -= 1
            continue
        # No options collected yet AND non-numbered content directly
        # above the footer — not a select prompt.
        return None

    if len(options) < _MIN_OPTIONS:
        return None

    options.reverse()  # bottom-up → chronological

    # 3. Verify contiguous numbering starting at 1. A 1-2-3-5 sequence
    #    (cursor skipped a row, OR detector got confused) is a stronger
    #    bail signal than a low option count.
    expected = list(range(1, len(options) + 1))
    actual = [num for _, num, _, _ in options]
    if actual != expected:
        return None

    # 4. Cursor → focused_index (None if no cursor visible on any row).
    focused_index: int | None = None
    for idx, (_, _, _, has_cursor) in enumerate(options):
        if has_cursor:
            focused_index = idx
            break

    # 5. Title from 1–4 non-blank lines above the topmost option.
    first_opt_idx = options[0][0]
    title = ""
    for back in range(1, 5):
        tidx = first_opt_idx - back
        if tidx < 0:
            break
        candidate = lines[tidx].strip()
        if not candidate:
            continue
        # Indented continuation of an option body above? Skip; keep
        # looking for the actual title.
        if lines[tidx].startswith(("     ", "\t")):
            continue
        title = candidate
        break

    option_texts = [text for _, _, text, _ in options]
    return ChoicePrompt(
        title=title,
        options=option_texts,
        input_mode="tui_select",
        focused_index=focused_index,
        body=_extract_body(lines, first_opt_idx),
    )


def _detect_legacy_choice_prompt(lines: list[str]) -> ChoicePrompt | None:
    """The original inline numbered-text detector.

    Looks for 3+ contiguous numbered rows at the bottom of the pane
    under a recognized header (decision phrase or trailing ``?``). No
    cursor, no multi-line bodies — Claude Code's stock prompt.
    """
    # 1. Strip trailing blank + chrome rows so the option block is at the
    #    "bottom" even when Claude Code's model/cost footer sits below it.
    end = len(lines)
    while end > 0:
        stripped = lines[end - 1].strip()
        if not stripped or _is_status_bar_line(stripped):
            end -= 1
            continue
        break
    if end == 0:
        return None

    # 2. Walk upward collecting contiguous numbered rows.
    option_lines: list[tuple[int, str]] = []  # (line_index, option_text)
    i = end - 1
    while i >= 0 and len(option_lines) < _MAX_OPTIONS:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            # Allow ONE blank between options? No — choice prompts in
            # Claude Code print options on consecutive lines. A blank
            # break means we left the option block.
            break
        m = _OPTION_RE.match(stripped)
        if not m:
            break
        if _ANSI_CSI_RE.search(line):
            # Mid-render — abort; we'll try again next poll tick.
            return None
        option_lines.append((i, m.group("text")))
        i -= 1

    if len(option_lines) < _MIN_OPTIONS:
        return None

    # Options collected bottom-up; reverse to chronological order.
    option_lines.reverse()
    first_option_index = option_lines[0][0]

    # 3. Look upward 1–4 lines for a header. Header must either end in
    #    ``?`` or match one of the recognized decision phrases.
    header = _find_header(lines, first_option_index)
    if header is None:
        return None

    options = [text for _, text in option_lines]
    return ChoicePrompt(
        title=header,
        options=options,
        body=_extract_body(lines, first_option_index),
    )


def _find_header(lines: list[str], first_option_index: int) -> str | None:
    """Search above the first option line for a decision-shaped header.
    Returns the trimmed header text, or None if no header found.

    Walks upward in two stages:

      1. Skip up to two blank rows immediately above the option block
         (Claude often separates the question from the list with a
         blank line, sometimes two).
      2. From the first non-blank line, consider up to four "back"
         steps. At each step we also skip additional blank/numbered
         rows without consuming the back budget — meaning the practical
         reach is "the first 1–4 *meaningful* lines above the option
         block, modulo small gaps". Stops early on a code-fence opener
         (the options are inside a code block, not a real prompt).
    """
    # Skip immediate blank rows above the option block — Claude often
    # separates the question from the list with a blank line.
    j = first_option_index - 1
    skipped_blanks = 0
    while j >= 0 and skipped_blanks < 2:
        stripped = lines[j].strip()
        if not stripped:
            j -= 1
            skipped_blanks += 1
            continue
        break
    if j < 0:
        return None

    # Walk up to 4 more meaningful lines looking for a recognized header.
    for back in range(0, 4):
        idx = j - back
        if idx < 0:
            break
        candidate = lines[idx].strip()
        if not candidate:
            continue
        # Code-fence opener with no closer between it and the option
        # block — the numbered lines are INSIDE a code block, not a
        # real prompt. Bail.
        if candidate.startswith("```") or candidate.startswith("~~~"):
            return None
        if _is_decision_header(candidate):
            return candidate
        # If the candidate is itself a numbered line, keep walking up —
        # we may have miscounted options.
        if _OPTION_RE.match(candidate):
            continue
        # Some prose between the prompt and the options is OK, but only
        # for one step — beyond that we lose the header anchoring.
        if back == 0:
            continue
        break
    return None


def _is_decision_header(line: str) -> bool:
    """True when ``line`` looks like a 'pick one of the following' prompt."""
    if not line:
        return False
    lower = line.lower()
    if any(phrase in lower for phrase in _HEADER_PHRASES):
        return True
    # Question mark anywhere near the end (allow trailing whitespace or
    # closing punctuation like ``)`` after).
    return line.rstrip(" )]\"'").endswith("?")


def is_invalidated(prev: ChoicePrompt, current_lines: list[str]) -> bool:
    """Return True when ``prev`` is no longer the live choice on screen.

    Two invalidation paths:
      - The option block is no longer at the bottom (new content
        streamed in below it).
      - The current capture detects a *different* choice prompt
        (signature mismatch).
    """
    current = detect_choice_prompt(current_lines)
    if current is None:
        return True
    return current.signature != prev.signature


# ===========================================================================
# Multi-question AskUserQuestion form
# ===========================================================================
#
# A different, richer widget than the single choice prompt above. Claude
# Code renders it when the AskUserQuestion tool is called with multiple
# questions. Shape (verified against Claude Code v2.1.148 by driving a
# live widget with tmux send-keys):
#
#   ←  ☐ Motion model  ☐ Label text  ☐ Scope  ✔ Submit  →   ← tab bar
#   How should a tool call animate along the wire?           ← active question
#   ❯ 1. One chip, direction =        ┌─ preview box ─┐      ← option + side
#       read/write                    │ ...           │        preview on the
#     2. Round-trip per call          └───────────────┘        SAME rows
#     3. Type something.                                       ← free-text option
#     Chat about this                                          ← meta (unnumbered)
#   Enter to select · ↑/↓ to navigate · Esc to cancel
#
# Verified keystroke model (drives the commit synthesis in ws_handler.py):
#   - Single-select question: ``N. Label`` rows; Enter on the focused row
#     SELECTS and AUTO-ADVANCES to the next question.
#   - Multi-select question: ``N. [ ] Label`` / ``N. [✔] Label`` rows;
#     Enter TOGGLES the focused box (no advance). A navigable unnumbered
#     ``Next`` row (after the last numbered option) advances on Enter. The
#     tab flips to ☒ once ≥1 box is checked.
#   - Free text ("Type something"): focus the row, type inline (text
#     replaces the label), Enter confirms.
#   - After the last question → review screen: same tab bar (all ☒),
#     "Review your answers" summary, then ``1. Submit answers / 2. Cancel``.
#     Enter on "Submit answers" submits.
#
# The HARD constraint: tmux only ever shows the ACTIVE question's options;
# the other tabs are off-screen. So the lens drives the form as a
# step-through wizard (answer active → advance → re-detect next → … →
# submit) rather than rendering all questions at once.
#
# The structural signal is the TAB BAR, NOT the footer: the footer text
# varies across Claude Code builds (some show "Tab to switch questions",
# others don't), but the ``←  ☐ … ✔ Submit  →`` bar is always present.

# Tab-bar anchor: starts with ← (U+2190), ends with → (U+2192), contains
# "Submit". Gated additionally on a checkbox glyph being present (below).
_TAB_BAR_RE = re.compile(r"^←.*\bSubmit\b.*→$")

# Ballot-box glyphs in the tab bar: ☐ unanswered, ☒/☑ answered.
_TAB_UNANSWERED = "☐"  # ☐
_TAB_ANSWERED = "☒☑"  # ☒ ☑
_TAB_GLYPHS = _TAB_UNANSWERED + _TAB_ANSWERED

# Inline multi-select checkbox prefix on an option row: "[ ] Label",
# "[✔] Label", "[x] Label". A blank mark means unchecked.
_CHECKBOX_RE = re.compile(r"^\[(?P<mark>[ xX✔✓☑☒])\]\s+(?P<label>.+)$")

# Box-drawing chars (U+2500–U+257F). A side-by-side preview diagram is
# glued onto the same terminal rows as the options; truncate option text
# at the first box-drawing char to drop the preview column.
_BOX_DRAWING_RE = re.compile(r"[─-╿]")


@dataclass
class QuestionTab:
    """One tab in the multi-question form's header bar."""

    label: str
    answered: bool


@dataclass
class FormRow:
    """One navigable row on the active form screen.

    ``kind``:
      - "choice"    a real answer option
      - "free_text" the "Type something" option (voice-note target)
      - "next"      the multi-select advance affordance
      - "meta"      "Chat about this"
      - "submit"    "Submit answers" on the review screen
      - "cancel"    "Cancel" on the review screen
    ``checked`` is the checkbox state for multi-select rows, else None.
    ``focused`` marks the row the on-screen cursor (❯) sits on.
    """

    text: str
    kind: str
    checked: bool | None = None
    focused: bool = False


@dataclass
class MultiQuestionForm:
    """A detected multi-question AskUserQuestion form's ACTIVE screen."""

    questions: list[QuestionTab]
    title: str
    # "single" | "multi" | "review"
    select_mode: str
    # All navigable rows in on-screen order (options + control rows). The
    # bridge uses this + the focused row for arrow-delta keystroke math.
    rows: list[FormRow]
    # Full body for the lens "read" view: the active question text plus each
    # option's description lines (which the row parse drops). EXCLUDED from
    # ``signature``/``form_id`` for the same no-churn reason as ChoicePrompt.
    body: list[str] = field(default_factory=list)
    # Per-screen signature: changes when the active question or any
    # selection/answered-state changes; STABLE across cursor-only moves
    # (focused excluded) so the lens isn't re-broadcast on every arrow.
    signature: str = field(default="")
    # Stable across the whole form (hash of the tab labels) so iOS knows
    # successive screens belong to the same form.
    form_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.signature:
            basis = (
                self.title
                + "\n"
                + "\n".join(
                    f"{r.kind}:{r.checked}:{r.text}" for r in self.rows
                )
                + "\n"
                + "".join("1" if q.answered else "0" for q in self.questions)
            )
            self.signature = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
        if not self.form_id:
            labels = "\n".join(q.label for q in self.questions)
            self.form_id = hashlib.sha1(labels.encode("utf-8")).hexdigest()[:12]

    @property
    def focused_index(self) -> int | None:
        """Index into ``rows`` of the cursor, or None if no cursor visible."""
        for i, r in enumerate(self.rows):
            if r.focused:
                return i
        return None

    @property
    def answer_rows(self) -> list[FormRow]:
        """The user-facing answer options (choices + free-text), excluding
        the Next/Submit/Cancel/meta control rows. This is what the lens
        renders as selectable picks."""
        return [r for r in self.rows if r.kind in ("choice", "free_text")]

    @property
    def answered_count(self) -> int:
        return sum(1 for q in self.questions if q.answered)


def _strip_preview(text: str) -> str:
    """Drop a side-by-side ASCII preview diagram glued onto an option row.

    The widget renders a box-drawn preview to the right of the options on
    the same terminal rows. Truncate at the first box-drawing char so the
    option label survives without the diagram column.
    """
    m = _BOX_DRAWING_RE.search(text)
    if m and m.start() > 0:
        return text[: m.start()].rstrip()
    return text


def _classify_row_text(text: str) -> str:
    low = text.lower().rstrip(".")
    if low == "type something":
        return "free_text"
    if low == "chat about this":
        return "meta"
    if low == "submit answers":
        return "submit"
    if low == "cancel":
        return "cancel"
    return "choice"


def _parse_form_row(line: str) -> FormRow | None:
    """Parse one body line into a FormRow, or None if it isn't a row.

    Numbered options (``N. …``) and the unnumbered control rows (``Next``,
    ``Chat about this``) are rows; descriptions, dividers, and review
    summary lines (``● …`` / ``→ …``) are not.
    """
    m = _TUI_OPTION_RE.match(line)
    if m:
        cursor = bool(m.group("cursor"))
        text = _strip_preview(m.group("text").strip())
        checked: bool | None = None
        cm = _CHECKBOX_RE.match(text)
        if cm:
            checked = cm.group("mark") != " "
            text = _strip_preview(cm.group("label").strip())
        return FormRow(
            text=text,
            kind=_classify_row_text(text),
            checked=checked,
            focused=cursor,
        )

    # Unnumbered control row (possibly cursor-prefixed): "Next" / "Chat
    # about this". Strip a leading cursor glyph before matching.
    s = line.strip()
    if not s:
        return None
    cursor = False
    if s[0] in _TUI_CURSOR_GLYPHS:
        cursor = True
        s = s[1:].strip()
    low = s.lower()
    if low == "next":
        return FormRow(text="Next", kind="next", focused=cursor)
    if low == "chat about this":
        return FormRow(text="Chat about this", kind="meta", focused=cursor)
    return None


def _parse_tab_bar(line: str) -> list[QuestionTab]:
    """Parse the ``←  ☐ A  ☒ B  ✔ Submit  →`` bar into question tabs.

    Splits on runs of 2+ spaces (labels may contain single spaces, e.g.
    "Motion model"). The ``✔ Submit`` token is the submit affordance, not
    a question, so it's excluded.
    """
    inner = line.strip().lstrip("←").rstrip("→").strip()
    tabs: list[QuestionTab] = []
    for token in re.split(r"\s{2,}", inner):
        token = token.strip()
        if not token:
            continue
        glyph = token[0]
        rest = token[1:].strip()
        if glyph in _TAB_GLYPHS:
            tabs.append(QuestionTab(label=rest, answered=(glyph in _TAB_ANSWERED)))
        # ✔ Submit (or any non-ballot token) is the submit affordance — skip.
    return tabs


def detect_question_form(lines: list[str]) -> MultiQuestionForm | None:
    """Detect a multi-question AskUserQuestion form's active screen.

    Anchored on the tab bar (top of the widget), then parses the active
    question's title, options, and control rows downward until the footer
    or status chrome. Returns None when no tab bar is present (not a
    multi-question form — caller falls back to ``detect_choice_prompt``).
    """
    # 1. Locate the tab bar (require a checkbox glyph too, so prose with
    #    "Submit" between arrows can't false-fire).
    tab_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _TAB_BAR_RE.match(stripped) and any(g in stripped for g in _TAB_GLYPHS):
            tab_idx = i
            break
    if tab_idx is None:
        return None
    questions = _parse_tab_bar(lines[tab_idx])
    if not questions:
        return None

    # 2. Body runs from below the tab bar to the footer. The footer sits
    #    below all form content and above any status chrome (task list,
    #    model/branch line), so anchoring ``end`` on it cleanly excludes
    #    the chrome. Do NOT use ``_is_status_bar_line`` here: it also
    #    matches the divider rules the widget draws BETWEEN the options and
    #    the "Chat about this" meta-row, which would truncate the body
    #    mid-form (dropping the free-text/Next/meta rows and the cursor).
    end = len(lines)
    for i in range(tab_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if _TUI_FOOTER_RE.search(stripped) or _PLAN_FOOTER_RE.search(stripped):
            end = i
            break

    # 3. Walk the body: first non-row line is the title; the rest parse
    #    into rows (descriptions / review summaries / dividers skipped).
    title = ""
    rows: list[FormRow] = []
    description_lines: list[str] = []
    for raw in lines[tab_idx + 1 : end]:
        stripped = raw.strip()
        if not stripped:
            continue
        if _ANSI_CSI_RE.search(raw):
            # Mid-render — bail, retry next poll.
            return None
        row = _parse_form_row(raw)
        if row is not None:
            rows.append(row)
            continue
        # Non-row content: a divider rule, a review bullet, the title, or an
        # option description body. Skip dividers/bullets; the first keeper is
        # the title; everything after is description text for the read view.
        if set(stripped) <= set("─━-_= "):
            continue
        if stripped[0] in "●→":  # ● review bullet, → answer
            continue
        if not title and not rows:
            title = stripped
            continue
        # Option description body — strip the side-by-side preview column.
        description_lines.append(_strip_preview(stripped))

    if not rows:
        return None

    body = (([title] if title else []) + description_lines)[-_MAX_BODY_LINES:]

    # 4. Classify the screen.
    if any(r.kind == "submit" for r in rows):
        select_mode = "review"
    elif any(r.checked is not None for r in rows):
        select_mode = "multi"
    else:
        select_mode = "single"

    return MultiQuestionForm(
        questions=questions,
        title=title,
        select_mode=select_mode,
        rows=rows,
        body=body,
    )


def is_form_invalidated(prev: MultiQuestionForm, current_lines: list[str]) -> bool:
    """True when ``prev`` is no longer the live form screen.

    Unlike a single choice prompt, a form's screen legitimately changes as
    the user advances (new question, flipped checkboxes). Invalidation
    means either no form is on screen, or a DIFFERENT FORM appeared
    (``form_id`` changed). A new SCREEN of the SAME form (signature changed
    but form_id same) is a normal step — not an invalidation.
    """
    current = detect_question_form(current_lines)
    if current is None:
        return True
    return current.form_id != prev.form_id
