"""Live status-line extraction for the lens footer.

Two independent extractors feed a single derived status dict that iOS
renders as the lens-footer left text ("⠹ Blanching · 54s · 1.3k").

  - ``extract_status_from_tmux``: regex scan over ANSI-stripped tmux
    capture lines. Picks up Claude Code's working-line footer
    ("* Blanching… (54s · ↓ 1.3k tokens)") for interactive sessions.

  - ``extract_status_from_stream_json``: walks stream-json events from
    a dispatched ``claude --resume -p`` subprocess. Pulls token counts
    from the latest assistant message and the current operation from
    the most recent ``tool_use`` block. Used when the dispatched run
    has no tmux pane to scrape.

``derive_status`` merges both sources (stream-json wins when fresh) and
falls back to the session's ``activity_type`` enum so a status line is
always present.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal

from bridge.activity import _has_idle_prompt, _is_status_bar_line


# Verbs Claude Code prints during work. We don't enumerate every possible
# gerund — the regex just captures any ``\w+ing`` token, but this set
# documents the common ones for readers tracing the code.
_KNOWN_VERBS = frozenset({
    "Blanching", "Brewing", "Cogitating", "Computing", "Considering",
    "Cooking", "Crafting", "Creating", "Deliberating", "Drafting",
    "Examining", "Exploring", "Fetching", "Generating", "Investigating",
    "Loading", "Pondering", "Preparing", "Processing", "Reading",
    "Refining", "Reflecting", "Reviewing", "Searching", "Synthesizing",
    "Thinking", "Tweaking", "Verifying", "Writing", "Working",
})


# Claude Code working line:
#   "* Blanching… (54s · ↓ 1.3k tokens)"
#   "* Thinking… (12s)"
#   "+ Combobulating… (7s · thinking with max effort)"
#   "✶ Grooving… (42s · ↓ 2.6k tokens · thought for 2s)"
# The leading glyph is an animated spinner that rotates through MANY frames —
# ``* + - · ✳ ✴ ✶ ✷ ✸ ✻ ✱`` and others — so we DON'T enumerate it: match any
# 1-3 leading non-word symbols. (The old fixed class ``[\*✳✴✻✱·]`` silently
# dropped the ``+`` / ``✶`` frames, so "Combobulating"/"Grooving" failed to
# parse and the footer fell back to a stale enum label like "Testing".) The
# real anchor that makes this a working line is the gerund verb followed by
# "(<digits>s" — prose almost never has that shape. Ellipsis is unicode (…)
# but we accept three ASCII dots too.
_WORKING_RE = re.compile(
    r"^\s*[^\w\s]{1,3}\s*"  # leading spinner glyph(s) — any 1-3 symbols
    r"(?P<verb>[A-Z][a-z]+ing)"                                 # verb (gerund)
    r"\s*(?:…|\.\.\.)?"                                    # optional ellipsis
    r"\s*\((?P<inner>[^)]*)\)",     # "(6m 40s · ↓ 1.3k tokens)" — parsed below
    re.IGNORECASE,
)

# Braille-spinner-prefixed variant:
#   "⠹ Drafting (12.4k)"
_BRAILLE_WORKING_RE = re.compile(
    r"^\s*[⠇-⣿]+\s*"                                  # any braille spinner
    r"(?P<verb>[A-Z][a-z]+ing)"
    r"\s*\((?P<inner>[^)]*)\)",
)

# Elapsed time inside a working-line "(...)". Claude always renders seconds,
# with optional minutes/hours on long turns: "7s", "54s", "6m 40s", "1h 2m 3s".
# (The old `\d+s`-only form silently failed on the minutes shape, so a turn
# running ≥1 min didn't parse and the footer fell back to a stale enum label.)
_ELAPSED_INNER_RE = re.compile(
    r"(?:(?P<h>\d+)\s*h\s*)?(?:(?P<m>\d+)\s*m\s*)?(?P<s>\d+)\s*s\b",
    re.IGNORECASE,
)


def _elapsed_seconds_from_inner(inner: str) -> int | None:
    """Total elapsed seconds parsed from a working-line inner ("6m 40s · …"),
    or None when no time token is present."""
    m = _ELAPSED_INNER_RE.search(inner)
    if m is None:
        return None
    h = int(m.group("h")) if m.group("h") else 0
    mn = int(m.group("m")) if m.group("m") else 0
    return h * 3600 + mn * 60 + int(m.group("s"))
# Token count inside a (…) inner. Two acceptable forms:
#   - "1.3k" or "12k"  — "k" suffix disambiguates from elapsed seconds.
#   - "999 tokens"    — bare integer followed by the literal word.
# Python doesn't allow duplicate named groups across alternation, so we
# use two unnamed groups and pick whichever matched.
_TOKENS_INNER_RE = re.compile(
    r"(\d+(?:\.\d+)?k)\s*(?:tokens?)?"
    r"|(\d+)\s*tokens?",
    re.IGNORECASE,
)


def _extract_tokens_from_inner(inner: str) -> str | None:
    """Search ``inner`` for a token-count form and return the matched
    string ("1.3k", "12k", or "999"), else None."""
    m = _TOKENS_INNER_RE.search(inner)
    if m is None:
        return None
    return m.group(1) or m.group(2)

# Interrupt hint Claude Code prints below the working line:
#   "(esc to interrupt)"
_CAN_INTERRUPT_RE = re.compile(r"\bes[cs]\s+to\s+interrupt\b", re.IGNORECASE)

# Stream-json tool-name → verb. Mirrors bridge/activity.py::_TOOL_PATTERNS
# so the lens shows consistent words across tmux-extracted and stream-json
# sources.
_TOOL_VERBS: dict[str, str] = {
    # Claude Code
    "Edit": "Writing", "Write": "Writing", "MultiEdit": "Writing",
    "NotebookEdit": "Writing",
    "Bash": "Running",
    "Read": "Searching", "Grep": "Searching", "Glob": "Searching", "LS": "Searching",
    "Task": "Working",
    "WebFetch": "Fetching", "WebSearch": "Searching",
    # Codex / Gemini common names — best-effort, these CLIs don't always emit
    # the same stream-json schema but if they ever do, this fills in.
    "apply_patch": "Writing", "patch": "Writing",
    "shell": "Running", "exec": "Running",
    "read_file": "Searching",
    "write_file": "Writing", "edit_file": "Writing",
    "run_shell": "Running",
    "list_dir": "Searching", "search_files": "Searching",
}

# Refine the generic "Running" verb if the bash command body smells like
# tests or a build. Same keyword sets as activity._TEST_KEYWORDS /
# _BUILD_KEYWORDS — kept in sync so emoji + footer text stay consistent.
_TEST_KEYWORDS = ("test", "pytest", "jest", "vitest", "cargo test", "npm test", "yarn test", "xctestrun")
_BUILD_KEYWORDS = ("build", "compile", "xcodebuild", "make ", "webpack", "vite build", "tsc ", "swiftc", "gcc", "clang")


@dataclass
class StatusLine:
    """Extracted live-status snapshot from one source (tmux or stream-json)."""

    label: str                      # "Blanching", "Writing", "Waiting for input"
    elapsed_s: int | None = None    # seconds since work started, if known
    tokens: str | None = None       # "1.3k", "12.4k", or None
    spinner: bool = True            # show animated spinner glyph?
    can_interrupt: bool = False     # "(esc to interrupt)" hint visible?
    source: Literal["tmux", "stream_json", "fallback"] = "fallback"


# Claude prints a dim per-turn summary line under its reply. Shapes seen live:
#   "Thought for 38s, ran 1 shell command"
#   "Thought for 30s, read 1 file"
#   "Thought for 15s, called claude-in-chrome, ran 1 shell command"
#   "Thought for 1m 3s"               (pure-thinking turn — no tool action)
#   "Ran 1 shell command"             (no "Thought for" preamble)
# When a session is idle we surface this in the lens footer so the bottom row
# shows WHAT CLAUDE JUST DID instead of the bare "Waiting for input" — for
# EVERY action type Claude reports (ran / read / edited / wrote / called / …),
# not just shell commands.
#
# Two anchors, each specific enough that ordinary reply prose can't
# false-match:
#   1. The "Thought for <dur>" preamble — Claude's verbatim turn header. The
#      ``dur`` group only accepts a real time token (digits + h/m/s), so a
#      sentence like "Thought for a moment, then…" never matches. The action
#      clause, if any, is whatever follows the first comma; a turn that only
#      reasoned ("Thought for 1m 3s") has no clause and we surface the thought.
#   2. A bare action clause opening with a known past-tense verb that is
#      immediately QUANTIFIED and END-ANCHORED ("Ran 1 shell command",
#      "Read 3 files") or is a "Called <tool>" form. The count + end anchor is
#      what stops prose ("ran 5 shell commands to verify the fix") from
#      matching — a real summary line ends right after the noun.
_THOUGHT_SUMMARY_RE = re.compile(
    r"^thought\s+for\s+"
    r"(?P<dur>(?:\d+(?:\.\d+)?\s*[hms]\s*)+)"   # "38s", "1m 3s", "1.5s"
    r"(?:,\s*(?P<action>.+?))?"                  # optional ", read 1 file"
    r"\s*\.?\s*$",
    re.IGNORECASE,
)
_BARE_ACTION_RE = re.compile(
    r"^(?P<action>"
    r"(?:(?:ran|read|edited|wrote|created|updated|listed|searched|fetched|"
    r"found|viewed|generated|removed|deleted|renamed|moved|added)"
    r"\s+\d+\s+[a-z]+(?:\s+[a-z]+){0,2})"        # verb + count + 1–3 noun words
    r"|(?:called\s+[\w.\-]+)"                     # "called claude-in-chrome"
    r")\s*\.?\s*$",
    re.IGNORECASE,
)

# Lens-footer width backstop. The footer's left label flex-shrinks/truncates
# on the glasses, but cap here so a pathologically long clause can't dominate
# the row before the lens trims it.
_ACTION_LABEL_MAX = 42


def _format_action_label(action: str) -> str:
    """Capitalize the leading letter and cap to the lens-footer width."""
    action = action.strip().rstrip(".")
    if not action:
        return ""
    label = action[:1].upper() + action[1:]
    return label if len(label) <= _ACTION_LABEL_MAX else label[: _ACTION_LABEL_MAX - 1] + "…"


def _extract_recent_action(lines: list[str]) -> str | None:
    """Most recent turn-summary action Claude reported, as a concise footer
    label ("Ran 1 shell command", "Read 1 file", "Edited 3 files",
    "Called claude-in-chrome"), or None when no summary line is visible.

    Scans bottom-up and returns the FIRST (latest) summary line — whether it
    carries a tool action or is a pure-thinking turn ("Thought for 1m 3s").
    Latest-wins is intentional: an action from an earlier turn would mislead
    once Claude has moved on. Lines arrive ANSI-stripped (module docstring)."""
    for raw in reversed(lines):
        s = raw.strip()
        if not s:
            continue
        m = _THOUGHT_SUMMARY_RE.match(s)
        if m:
            action = (m.group("action") or "").strip().rstrip(".")
            if action:
                return _format_action_label(action)
            dur = (m.group("dur") or "").strip()
            return _format_action_label(f"thought for {dur}") if dur else None
        m = _BARE_ACTION_RE.match(s)
        if m:
            return _format_action_label(m.group("action"))
    return None


def extract_status_from_tmux(lines: list[str]) -> StatusLine | None:
    """Scan the last few non-chrome rows of a tmux capture for a Claude Code
    working line. Returns ``None`` when nothing matches — caller falls back
    to the session's activity_type enum.
    """
    if not lines:
        return None

    # A LIVE working line wins over the bare-❯ idle heuristic (see
    # _find_working_line). If one is present, the session is working.
    found = _find_working_line(lines)
    if found is not None:
        verb, inner, can_interrupt = found
        return StatusLine(
            label=verb.capitalize(),
            elapsed_s=_elapsed_seconds_from_inner(inner),
            tokens=_normalize_tokens(_extract_tokens_from_inner(inner)),
            spinner=True,
            can_interrupt=can_interrupt,
            source="tmux",
        )

    # No live working line → Claude isn't working. Show the turn-COMPLETION
    # summary ("Crunched · 9m 27s") — the whole-turn verb + duration Claude
    # prints when it finishes. This is what the user wants on the footer once a
    # turn ends (preferred over the last action), and it shows while the user
    # is TYPING too (the completion line stays on screen above the prompt).
    completion = _extract_completion(lines)
    if completion is not None:
        verb, secs = completion
        return StatusLine(label=verb, elapsed_s=secs, spinner=False, source="tmux")

    # No completion line in view (e.g. scrolled off above a long typed
    # message) → fall back to WHAT IT LAST DID ("Ran 1 shell command").
    action = _extract_recent_action(lines)
    if action:
        return StatusLine(label=action, spinner=False, source="tmux")

    # Neither — if a bare prompt sits at the bottom, it's freshly waiting.
    if _has_idle_prompt("\n".join(lines)):
        return StatusLine(label="Waiting for input", spinner=False, source="tmux")

    return None


def _find_working_line(lines: list[str]) -> tuple[str, str, bool] | None:
    """Bottom-up scan for the most recent LIVE working line. Returns
    ``(verb, inner, can_interrupt)`` or ``None``.

    Why a working line beats the bare-❯ idle heuristic: Claude Code renders an
    empty "❯" input box at the bottom EVEN WHILE THINKING (with
    "✢ Finagling… (6m 40s)" a few rows above it), so ``_has_idle_prompt`` is
    True during active work. A present-tense gerund working line reliably means
    active work — Claude REMOVES it on completion (an idle pane shows a past-
    tense "Worked for 6m" line, which the gerund regex doesn't match), so its
    presence near the bottom is the signal.

    Match the working regexes BEFORE the _is_status_bar_line chrome filter —
    the working line itself routinely ENDS with "· thought for Ns" /
    "esc to interrupt", which the filter would classify as chrome and discard
    (the "✶ Grooving… (… · thought for 2s)" bug). The filter still bounds how
    far up we scan for OTHER content, so a stale working line deep in
    scrollback can't win.
    """
    can_interrupt = False
    seen_content = 0
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _CAN_INTERRUPT_RE.search(stripped):
            can_interrupt = True
        m = _WORKING_RE.match(stripped) or _BRAILLE_WORKING_RE.match(stripped)
        if m:
            return m.group("verb"), m.group("inner"), can_interrupt
        if _is_status_bar_line(stripped):
            continue
        seen_content += 1
        if seen_content >= 8:
            break
    return None


def has_live_working_line(lines: list[str]) -> bool:
    """True when a present-tense working line is visible near the bottom — the
    unambiguous "actively working" signal, independent of spinner glyph and of
    the bare-❯ input box Claude shows even while working. Shared by the lens-
    footer extractor and ``activity._detect_activity_type`` / the poll's idle
    gate so the two never disagree (the bug where the footer said
    "Recombobulating" while the session was marked idle)."""
    return _find_working_line(lines) is not None


# Turn-COMPLETION line Claude prints when a turn finishes (past tense):
#   "✻ Crunched for 9m 27s"   "✻ Worked for 14m 49s"   "✻ Churned for 1m 9s"
# Anchored on the leading glyph (✻ etc.) + "<Verb> for <duration>" so prose
# like "Waited for 5s" can't match. This is the canonical completion detector
# — activity.py defers to has_completion_line() rather than carrying its own
# copy, so the footer and the poll's at-rest gate never drift.
_COMPLETION_LINE_RE = re.compile(
    r"^[^\w\s]{1,3}\s*(?P<verb>[A-Za-z][a-z]+)\s+for\s+(?P<dur>\d[\d\s.hms]*[hms])\b",
    re.IGNORECASE,
)
_DUR_COMPONENT_RE = re.compile(r"(\d+)\s*(ms|h|m|s)\b", re.IGNORECASE)
_DUR_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1, "ms": 0}


def _parse_duration_seconds(phrase: str) -> int | None:
    """Sum a clean duration phrase ("9m 27s", "6m", "54s") to total seconds,
    or None when it carries no time component."""
    total = 0
    found = False
    for m in _DUR_COMPONENT_RE.finditer(phrase):
        found = True
        total += int(m.group(1)) * _DUR_UNIT_SECONDS[m.group(2).lower()]
    return total if found else None


def _extract_completion(lines: list[str]) -> tuple[str, int | None] | None:
    """Most recent turn-completion line near the bottom as (verb, seconds),
    e.g. ("Crunched", 567) for "✻ Crunched for 9m 27s". Else None."""
    bottom = lines[-12:] if len(lines) > 12 else lines
    for raw in reversed(bottom):
        m = _COMPLETION_LINE_RE.match(raw.strip())
        if m:
            return m.group("verb").capitalize(), _parse_duration_seconds(m.group("dur"))
    return None


def has_completion_line(lines: list[str]) -> bool:
    """True if a turn-completion line is near the bottom — the "turn finished"
    signal. Shared with ``activity.py`` (its at-rest gate + the auto-idle path)
    so the user-is-typing-after-a-finished-turn case stays consistent."""
    return _extract_completion(lines) is not None


def extract_status_from_stream_json(events: list[dict]) -> StatusLine | None:
    """Walk recent stream-json events from a dispatched ``claude --resume``
    subprocess and derive a status line. Returns ``None`` when the buffer
    is empty or contains no useful events.

    Logic:
      - The most recent ``assistant`` message provides token counts via
        ``message.usage.input_tokens + output_tokens``.
      - The most recent ``tool_use`` block sets the verb (Edit→Writing,
        Bash→Running, etc.). When no tool is in flight we fall back to
        "Thinking" — the model is producing output.
      - A trailing ``result`` event means the run finished; we return None
        so the caller can re-derive from the session enum (now likely
        "idle" or "completed").
    """
    if not events:
        return None

    # Run finished — the most recent event is a result envelope. No live
    # status to show; caller falls back to the (now likely "completed" or
    # "errored") activity_type enum.
    last_ev = events[-1] if events else None
    if isinstance(last_ev, dict) and last_ev.get("type") == "result":
        return None

    # Walk newest-first to find the most recent meaningful event.
    last_tool: str | None = None
    last_tool_input: str = ""
    last_usage_tokens: str | None = None

    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        et = ev.get("type")
        if et == "result":
            # An older result event from a prior run can sit in the buffer
            # while a new run streams in. Treat it as a separator: stop
            # walking back further so we only summarize the current run.
            break
        if et == "assistant":
            msg = ev.get("message") or {}
            usage = msg.get("usage") or {}
            if last_usage_tokens is None:
                total = _coerce_int(usage.get("input_tokens")) + _coerce_int(usage.get("output_tokens"))
                if total > 0:
                    last_usage_tokens = _format_tokens(total)
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use" and last_tool is None:
                    last_tool = block.get("name") or ""
                    inp = block.get("input") or {}
                    if isinstance(inp, dict):
                        # For Bash, the verb gets refined to testing/building
                        # below; capture the command body so we can grep it.
                        last_tool_input = str(inp.get("command", "")) + " " + str(inp.get("description", ""))
        if et == "user":
            # tool_result event — work resumed.
            continue
        if last_tool is not None and last_usage_tokens is not None:
            break

    verb = _TOOL_VERBS.get(last_tool or "", "Thinking")
    if verb == "Running" and last_tool_input:
        lower = last_tool_input.lower()
        if any(kw in lower for kw in _TEST_KEYWORDS):
            verb = "Testing"
        elif any(kw in lower for kw in _BUILD_KEYWORDS):
            verb = "Building"

    return StatusLine(
        label=verb,
        elapsed_s=None,  # caller adds from session._activity_started_at
        tokens=last_usage_tokens,
        spinner=True,
        source="stream_json",
    )


def derive_status(
    session,
    tmux_status: StatusLine | None,
    stream_status: StatusLine | None,
    context_pct: int | None = None,
) -> dict:
    """Merge extractor outputs into a single broadcast-ready dict.

    Merge order (most-specific first):
      1. stream_status — most authoritative for ``claude --resume``
         dispatches; carries real token counts.
      2. tmux_status — interactive tmux session footer; carries Claude
         Code's own working verb.
      3. session.activity_type enum — fallback when neither extractor
         matched (e.g., Codex/Gemini sessions whose footer shape we don't
         parse yet).
      4. "Idle" — terminal state.

    ``context_pct`` is the context-window usage percentage (0-100)
    extracted independently from the chrome via ``extract_context_percent``.
    It rides on every payload regardless of which branch above wins so the
    iOS lens header shows the indicator even when the session is idle /
    waiting (no working line, no stream activity).
    """
    now = time.monotonic()
    started = getattr(session, "_activity_started_at", None)
    # monotonic() is process-relative, so ``started`` can legitimately sit
    # near zero shortly after process boot — gate on type only, not sign,
    # and clamp to >=0 to swallow tiny clock skew.
    elapsed = max(0, int(now - started)) if isinstance(started, (int, float)) else None

    if stream_status is not None:
        return {
            "label": stream_status.label,
            "elapsed_s": stream_status.elapsed_s if stream_status.elapsed_s is not None else elapsed,
            "tokens": stream_status.tokens,
            "context_pct": context_pct,
            "spinner": stream_status.spinner,
            "can_interrupt": stream_status.can_interrupt,
            "source": stream_status.source,
            "hint": action_hint(session),
        }

    if tmux_status is not None:
        return {
            "label": tmux_status.label,
            "elapsed_s": tmux_status.elapsed_s if tmux_status.elapsed_s is not None else elapsed,
            "tokens": tmux_status.tokens,
            "context_pct": context_pct,
            "spinner": tmux_status.spinner,
            "can_interrupt": tmux_status.can_interrupt,
            "source": tmux_status.source,
            "hint": action_hint(session),
        }

    label = _enum_to_label(getattr(session, "activity_type", "sleeping"))
    return {
        "label": label,
        "elapsed_s": elapsed,
        "tokens": None,
        "context_pct": context_pct,
        # Spin only when the session is genuinely active AND its label is an
        # active-work verb. Two gates because either alone is wrong on the
        # connect-replay path: a just-finished session has label "Completed"
        # (caught by the verb gate), but an idle session can also carry a
        # STALE work label like "thinking" if its activity_type wasn't reset
        # — the status gate catches that. Without both, the lens shows a
        # spinner on a finished/idle session, implying it's still working.
        "spinner": (
            getattr(session, "status", "idle") == "active"
            and label in _SPINNING_LABELS
        ),
        "can_interrupt": False,
        "source": "fallback",
        "hint": action_hint(session),
    }


def action_hint(session) -> str:
    """Suggested right-side hint for the lens footer. iOS may override based
    on currentContext (e.g., when a permission card is on screen iOS knows
    the hint should be 'Tap: approve · Hold: deny' regardless of what the
    bridge suggests). Bridge sends a best-guess so single-session UIs don't
    need iOS to compute it from raw state.
    """
    activity = getattr(session, "activity_type", "sleeping")
    status = getattr(session, "status", "idle")
    if activity == "approval":
        return "Tap: approve · Hold: deny"
    if activity == "input":
        return "Tap: type · Hold: voice"
    if status == "active":
        return "Tap: interrupt"
    return "Tap: voice"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_tokens(raw: str | None) -> str | None:
    """Pass-through normalizer: returns "1.3k" or "12400" as-is, strips
    surrounding whitespace. Numeric form is preserved (the lens renders
    whatever the bridge sends)."""
    if raw is None:
        return None
    return raw.strip() or None


# Claude Code paints the context-window indicator inside the status chrome
# at the bottom of every pane refresh. Formats observed:
#   "0/1000k"      — [1M] builds, used has no `k` suffix
#   "150k/200k"    — standard builds, both sides carry `k`
#   "15.3k/200k"   — fractional thousands when context is mid-budget
# The (?<![\d.]) / (?![\d]) bounds keep the regex from matching numeric
# strings that happen to contain a slash (e.g. "20/24/2026" dates).
_CONTEXT_WINDOW_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)k?/(\d+)k(?![\d])",
    re.IGNORECASE,
)

# Some builds (and high-usage states) print a literal percentage line
# instead of — or above — the X/Yk gauge, e.g. "89% context used". This
# is the percent already USED, so we return it directly. Used only as a
# fallback when the X/Yk gauge isn't found, since the gauge is the
# long-standing primary and the two can disagree by a few points (the
# "used" figure folds in system-prompt/tool overhead).
_CONTEXT_PERCENT_RE = re.compile(
    r"(?<!\d)(\d{1,3})%\s+context\s+used\b",
    re.IGNORECASE,
)


def extract_context_percent(lines: list[str]) -> int | None:
    """Scan tmux capture lines for Claude Code's context-window indicator
    and return percent used (0-100), or None when no indicator is found.

    Unlike the working-line extractor, this one DOES walk chrome rows —
    that's exactly where the X/Yk fragment lives. Codex and Gemini don't
    emit a comparable format yet, so non-Claude sessions return None.

    Two passes so the X/Yk gauge always wins over the literal
    "NN% context used" line regardless of their vertical order on screen.
    """
    # Primary: the X/Yk gauge in the bottom status chrome.
    for line in reversed(lines):
        m = _CONTEXT_WINDOW_RE.search(line)
        if not m:
            continue
        try:
            used = float(m.group(1))
            budget = float(m.group(2))
        except ValueError:
            continue
        if budget <= 0:
            # Degenerate gauge — keep scanning other lines, then fall
            # through to the literal "% context used" pass below. (Don't
            # bail with None here: that would skip a perfectly good
            # literal fallback on the same frame.)
            continue
        return max(0, min(100, round(100 * used / budget)))
    # Fallback: the literal "NN% context used" line.
    for line in reversed(lines):
        m = _CONTEXT_PERCENT_RE.search(line)
        if m:
            return max(0, min(100, int(m.group(1))))
    return None


def sticky_context_percent(lines: list[str], last: int | None) -> int | None:
    """Resolve the context percentage for one poll, holding the last known
    value when this frame has no parseable indicator.

    The gauge isn't painted on every frame — Claude Code truncates the
    status line to terminal width (so the X/Yk fragment can fall off the
    right edge), redraws it mid-working-line, and some builds only print
    the literal "NN% context used". Treating those misses as "erase" makes
    the lens header chip flicker out between good frames. Context only
    creeps upward within a turn, so a slightly stale percent is the right
    tradeoff. Caller persists the return value as the new ``last``.
    """
    pct = extract_context_percent(lines)
    return pct if pct is not None else last


def _format_tokens(total: int) -> str:
    """Format a raw token integer the way Claude Code displays it: thousands
    suffix when ≥1000, with one decimal for 1000–9999."""
    if total < 1000:
        return str(total)
    if total < 10_000:
        return f"{total/1000:.1f}k"
    return f"{round(total/1000)}k"


def _coerce_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# Labels that represent active, in-flight work — the only ones that animate
# a spinner on the lens. The fallback branch of derive_status gates the
# spinner on this so terminal states (Completed/Error/Stopped), waiting
# states (Waiting for approval/input), and idle/dead never spin. It's the
# active-work subset of _enum_to_label's outputs.
_SPINNING_LABELS = frozenset({
    "Thinking", "Writing", "Searching", "Running", "Building", "Testing", "Working",
})


def _enum_to_label(activity_type: str) -> str:
    """Map session.activity_type enum value to a display label. Mirrors the
    set used in session_manager.Session.activity_type."""
    return {
        "thinking": "Thinking",
        "writing": "Writing",
        "searching": "Searching",
        "running": "Running",
        "building": "Building",
        "testing": "Testing",
        "working": "Working",
        "completed": "Completed",
        "errored": "Error",
        "stopped": "Stopped",
        "approval": "Waiting for approval",
        "input": "Waiting for input",
        "registered": "Ready",
        "sleeping": "Idle",
        "dead": "Disconnected",
    }.get(activity_type, "Idle")
