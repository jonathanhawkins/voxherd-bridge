"""Activity detection, polling loops, session discovery, and pruning.

Extracted from ``bridge_server.py`` — these background loops poll tmux for
terminal output, detect what Claude Code is doing, broadcast changes to iOS,
prune dead sessions, and discover pre-existing tmux sessions at startup.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time

from bridge.env_utils import get_subprocess_env
from bridge import tmux_manager
from bridge import sub_agent_monitor
# session_status and choice_detector both import _has_idle_prompt /
# _is_status_bar_line from THIS module, so eager top-level imports
# would create a circular ImportError at boot. We import them lazily
# inside _activity_poll_loop (which is the only consumer) — Python
# caches the module after the first call, so cost is negligible.
from bridge.server_state import (
    sessions, broadcast_to_ios, _state_sync_msg, log_event,
    _STICKY_ACTIVITY, _cancel_terminal_subs_for_session,
)
import bridge.server_state as _state
from bridge.validation import _ANSI_RE, _load_projects
from bridge.assistant import assistants_compatible, infer_assistant_from_process


# Tracks the active numbered-choice prompt per session so the activity
# poll only broadcasts ``choice_prompt`` on the rising edge (and
# ``choice_prompt_cancelled`` on the falling edge). Sessions absent from
# this dict have no live choice on screen. Value type is
# ``choice_detector.ChoicePrompt`` (lazy import; see comment above).
_LAST_CHOICE_FOR_SESSION: dict = {}

# Consecutive-tick miss counter per session. The TUI prompt detector
# can transiently miss (cursor blink overlapping the capture, ANSI
# escape leaks, brief widget re-render) and a single-tick miss must
# NOT cancel the lens choice card — the user would tap, the bridge
# would broadcast cancellation between the tap arriving and iOS
# rendering the next focus state, and the card would vanish before
# the user could pick. Hold the previous prompt until we've missed
# ``_CHOICE_MISS_THRESHOLD`` consecutive polls (~3s at the 1.5s
# cadence). Cleared when detection re-succeeds or the session is
# removed.
_CHOICE_MISS_COUNT: dict[str, int] = {}
_CHOICE_MISS_THRESHOLD = 2

# Diagnostic throttle: the last "footer present but no options parsed"
# pane signature we logged per session, so the warning fires once per
# distinct failing capture instead of on every poll tick. Cleared on a
# successful detect.
_CHOICE_PARSE_FAIL: dict[str, str] = {}

# Multi-question AskUserQuestion form tracking — mirrors the single-choice
# state above. A form supersedes the single-choice path (a form IS a
# select widget, but richer). The signature changes each time the active
# question/selection changes (a normal wizard step), so edge-emit on
# signature change; the falling edge (form gone) is debounced with the
# same miss threshold as choices. Value type:
# ``choice_detector.MultiQuestionForm``.
_LAST_FORM_FOR_SESSION: dict = {}
_FORM_MISS_COUNT: dict[str, int] = {}


def _question_form_msg(session, form) -> dict:
    """Build the ``question_form`` WS payload for a detected form screen.

    ``options`` carries every navigable row EXCEPT the "Chat about this"
    meta-row (selecting it declines the form — not something we surface on
    the lens). The lens branches on ``select_mode`` + each row's ``kind``:
    single-select rows commit-and-advance, multi-select rows toggle, the
    ``next`` row advances, ``submit``/``cancel`` finish the review screen,
    and the ``free_text`` row arms voice dictation.
    """
    return {
        "type": "question_form",
        "session_id": session.session_id,
        "project": session.project,
        "form_id": form.form_id,
        "signature": form.signature,
        "select_mode": form.select_mode,
        "title": form.title,
        "body": form.body,
        "questions": [
            {"label": q.label, "answered": q.answered} for q in form.questions
        ],
        "answered_count": form.answered_count,
        "total_questions": len(form.questions),
        "options": [
            {
                "text": r.text,
                "kind": r.kind,
                "checked": bool(r.checked) if r.checked is not None else False,
            }
            for r in form.rows
            if r.kind != "meta"
        ],
    }


async def _deep_capture_choice_body(tmux_target: str, signature: str) -> list[str] | None:
    """Return the FULL body for a just-detected choice prompt via a deeper capture.

    The activity poll captures only ~30 on-screen rows, which truncates a long
    plan's body. On the RISING EDGE of a prompt (rare) we pay one extra
    ``capture-pane -S -200`` and re-detect; if it's the same prompt (matching
    ``signature``) we return its fuller ``body``. Returns None when the deep
    capture fails or resolves to a different/no prompt — the caller then keeps
    the shallow body.
    """
    from bridge import choice_detector
    from bridge.validation import _validate_tmux_pane_target
    # Validate for parity with ws_handler's capture paths (defense in depth:
    # rejects malformed/protected targets and a '-'-leading name tmux could
    # otherwise read as a flag).
    target, target_err = _validate_tmux_pane_target(tmux_target)
    if target_err or not target:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-t", target,
            "-p", "-S", "-200",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
    except (OSError, ValueError):
        return None
    if proc.returncode != 0 or not stdout:
        return None
    clean = _ANSI_RE.sub("", stdout.decode("utf-8", errors="replace"))
    deep = choice_detector.detect_choice_prompt(clean.splitlines())
    if deep is not None and deep.signature == signature and deep.body:
        return deep.body
    return None


async def _broadcast_question_form(session, pane_lines: list[str]) -> bool:
    """Detect + edge-broadcast a multi-question form for ``session``.

    Returns True when a form is on screen (or within the cancel debounce
    window) — the caller then SKIPS single-choice detection, because a
    form supersedes it (the review screen's Submit/Cancel would otherwise
    read as a 2-option choice prompt). Returns False when no form is or was
    recently on screen.
    """
    from bridge import choice_detector

    sid = session.session_id
    form = choice_detector.detect_question_form(pane_lines)
    prev = _LAST_FORM_FOR_SESSION.get(sid)

    if form is not None:
        _FORM_MISS_COUNT.pop(sid, None)
        # A live form supersedes any single-choice tracking for this
        # session — drop it so the choice falling-edge logic can't fire.
        _LAST_CHOICE_FOR_SESSION.pop(sid, None)
        _CHOICE_MISS_COUNT.pop(sid, None)
        is_new = prev is None or form.signature != prev.signature
        _LAST_FORM_FOR_SESSION[sid] = form  # cache fresh focus every tick
        if is_new:
            log_event(
                "info", session.project,
                f"Question form ({form.select_mode}) — "
                f"{form.answered_count}/{len(form.questions)} answered, "
                f"{len(form.answer_rows)} options"
            )
            await broadcast_to_ios(_question_form_msg(session, form))
        return True

    if prev is not None:
        # Debounce the falling edge exactly like choices: a one-tick miss
        # (mid-render, cursor blink) must not dismiss the lens card.
        miss = _FORM_MISS_COUNT.get(sid, 0) + 1
        _FORM_MISS_COUNT[sid] = miss
        if miss < _CHOICE_MISS_THRESHOLD:
            return True  # still holding the form — skip choice detection
        _LAST_FORM_FOR_SESSION.pop(sid, None)
        _FORM_MISS_COUNT.pop(sid, None)
        await broadcast_to_ios({
            "type": "question_form_cancelled",
            "session_id": sid,
            "project": session.project,
        })
        return False

    return False


# ---------------------------------------------------------------------------
# Queued command dispatch
# ---------------------------------------------------------------------------


async def _drain_queued_command(session) -> None:
    """If the session has a queued voice command, dispatch it now that it's idle."""
    command = session.queued_command
    if not command:
        return
    session.queued_command = None

    # Import here to avoid circular dependency
    from bridge.ws_handler import _dispatch_agent

    sessions.update_status(session.session_id, "active", activity_type="working")
    log_event("warning", session.project, f"Dispatching queued command: {command[:60]}")
    asyncio.create_task(_dispatch_agent(session.session_id, session.project_dir, command))
    await broadcast_to_ios({
        "type": "command_accepted",
        "session_id": session.session_id,
        "project": session.project,
        "assistant": session.assistant,
        "message": command,
        "activity_type": "working",
        "queued": True,
        # queued_command is only ever set by the voice path (REST dispatches
        # immediately), so the drain is the tail end of a spoken command —
        # iOS announces it so the user knows their queued command went.
        "origin": "queue",
    })


# ---------------------------------------------------------------------------
# Activity type detection from terminal output
# ---------------------------------------------------------------------------

# Patterns to detect what the assistant is doing from tmux output.
# These cover Claude Code, Codex, and Gemini CLI tool patterns.
_TOOL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Claude Code tools
    (re.compile(r"(?:^|\s)(?:Edit|Write|MultiEdit)\b", re.MULTILINE), "writing"),
    (re.compile(r"(?:^|\s)Bash\b", re.MULTILINE), "running"),  # refined below
    (re.compile(r"(?:^|\s)(?:Read|Grep|Glob|LS)\b", re.MULTILINE), "searching"),
    (re.compile(r"(?:^|\s)Task\b", re.MULTILINE), "working"),
    # Codex tools (uses shell, apply_patch, read_file patterns)
    (re.compile(r"(?:^|\s)(?:apply_patch|patch)\b", re.MULTILINE), "writing"),
    (re.compile(r"(?:^|\s)(?:shell|exec)\b", re.MULTILINE), "running"),
    (re.compile(r"(?:^|\s)read_file\b", re.MULTILINE), "searching"),
    # Gemini CLI tools (uses write_file, run_shell, read_file patterns)
    (re.compile(r"(?:^|\s)(?:write_file|edit_file)\b", re.MULTILINE), "writing"),
    (re.compile(r"(?:^|\s)run_shell\b", re.MULTILINE), "running"),
    (re.compile(r"(?:^|\s)(?:list_dir|search_files)\b", re.MULTILINE), "searching"),
]

_TEST_KEYWORDS = ("test", "pytest", "jest", "vitest", "cargo test", "npm test", "yarn test", "xctestrun")
_BUILD_KEYWORDS = ("build", "compile", "xcodebuild", "make ", "webpack", "vite build", "tsc ", "swiftc", "gcc", "clang")

# Sticky activity state: tracks the last "specific" (non-thinking) activity
# type per session so we don't flicker to 🧠 between tool calls.
# NOTE: The actual dict lives in server_state._STICKY_ACTIVITY (imported above).
_STICKY_HOLD_SECONDS = 5.0  # how long to hold a specific type before falling back to thinking

# Tracks when each session last had real terminal activity (spinners, tool calls).
# Used to auto-idle sessions that are stuck in "active" but actually at prompt.
_LAST_REAL_ACTIVITY: dict[str, float] = {}
_IDLE_TIMEOUT_SECONDS = 12.0  # mark active→idle after this many seconds with no detected activity

# Consecutive task-file scans (every ~4.5s) where the HOOK path claims live
# sub-agents but task files independently report zero in_progress, before we
# treat the hook entries as stranded (a dropped SubagentStop POST — hooks fail
# silently on network errors) and clear them. Without this, a stranded entry
# pins sub_agent_count > 0 forever, and _should_auto_idle then blocks the
# session from EVER auto-idling — the poller is the documented fallback for
# missed hooks, so neutering it for sub-agent sessions would strand them
# "active" at a bare prompt indefinitely. ~3 scans ≈ 13.5s grace absorbs the
# SubagentStart→task-file write race and brief gaps between sequential agents.
_SUBAGENT_STRANDED_THRESHOLD = 3
_SUBAGENT_STRANDED_MISS: dict[str, int] = {}


def _detect_activity_type(text: str) -> str | None:
    """Detect what the assistant (Claude/Codex/Gemini) is doing from terminal output.

    Returns the detected activity type, or None if nothing specific was found
    (caller should apply sticky logic).
    """
    # Check for spinner characters first — these are the strongest signal
    # that the assistant is actively working.  All three CLIs use braille
    # spinners or similar Unicode animation characters.
    has_spinner = bool(re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◑◒◓⏳]", text))

    # Claude's star/asterisk spinner frames (✶ ✢ ✳ * +) aren't in the braille
    # set above, AND it renders a bare ❯ input box even while working — so a
    # working session used to match the idle early-return below and get auto-
    # idled (footer said "Recombobulating" while the session was marked idle).
    # A present-tense working line is the authoritative "actively working"
    # signal; share it with the lens footer (lazy import breaks the
    # activity↔session_status cycle).
    from bridge.session_status import has_completion_line, has_live_working_line
    lines = text.splitlines()
    has_working_line = has_live_working_line(lines)

    # If there's no spinner and no live working line, the session isn't
    # actively working — provided it's visibly AT REST: either a bare ❯ idle
    # prompt, OR a turn-completion line ("✻ Churned for 1m 9s") near the
    # bottom. The completion-line case is what catches "user is TYPING after a
    # finished turn": the prompt holds typed text (so it's not a bare ❯), but
    # the turn is done — without this, the tool-pattern loop below matched
    # stale tool names / the user's own text in scrollback and re-activated the
    # session as a phantom "Testing".
    if not has_spinner and not has_working_line and (
        _has_idle_prompt(text) or has_completion_line(lines)
    ):
        return None

    for pattern, activity in _TOOL_PATTERNS:
        if pattern.search(text):
            # Refine Bash/shell into testing/building if command matches
            if activity == "running":
                lower = text.lower()
                if any(kw in lower for kw in _TEST_KEYWORDS):
                    return "testing"
                if any(kw in lower for kw in _BUILD_KEYWORDS):
                    return "building"
            return activity
    # Spinner / working line detected but no tool patterns — assistant is thinking
    if has_spinner or has_working_line:
        return "thinking"
    return None  # no match — let caller apply sticky logic


# Idle prompt patterns for all supported assistants:
# - Claude Code: bare ❯ or > on a line
# - Codex: "> " prompt or "codex>" style prompt
# - Gemini CLI: "❯" or ">" prompt, similar to Claude
_IDLE_PROMPT_RE = re.compile(
    r"^[\u276f>❯]\s*$"        # Claude/Gemini: bare ❯ or >
    r"|^codex>\s*$"            # Codex: "codex>" prompt
    r"|^gemini>\s*$",          # Gemini: "gemini>" prompt
    re.MULTILINE | re.IGNORECASE,
)


def _has_idle_prompt(text: str) -> bool:
    """Return True if the assistant's idle prompt appears near the bottom of text.

    Checks for idle prompts from all supported assistants:
    - Claude Code: ❯ prompt with model/cost bar below
    - Codex: "> " or "codex>" prompt
    - Gemini CLI: ❯ or ">" prompt

    We check the bottom 12 lines for any idle prompt.  If found, the
    assistant is at its input prompt, not actively working.
    """
    lines = text.rstrip().splitlines()
    bottom = lines[-12:] if len(lines) > 12 else lines
    for line in bottom:
        stripped = line.strip()
        if _IDLE_PROMPT_RE.match(stripped):
            return True
    return False


def _should_overwrite_activity_type(
    session_status: str, local: str, current: str
) -> bool:
    """Gate for the poll-loop's per-tick ``session.activity_type`` write.

    Returns True only when the session is currently active AND the
    locally-computed activity_type differs from what the session already
    has. For idle/waiting sessions, ``activity_type`` is owned by
    ``update_status`` (sleeping/completed/approval/input), and the poll
    loop's fallback "thinking" must not overwrite it — that's the bug
    where macOS rendered "Thinking" on sessions iOS rendered as idle
    (status="idle" + activity_type="thinking" mismatch via the REST poll
    path, masked on iOS by ``displayActivityType``).

    Extracted as a pure helper so the regression tests can exercise the
    gate directly without re-implementing the expression.
    """
    return session_status == "active" and local != current


def _should_auto_idle(
    *,
    effective_idle: bool,
    has_live_subagents: bool,
    inactive_seconds: float,
    idle_timeout: float = _IDLE_TIMEOUT_SECONDS,
) -> bool:
    """Decide whether an ``active`` session with no currently-detected activity
    should be auto-idled.

    A session running a sub-agent (Task/Explore) is NEVER idled: the parent turn
    isn't finished even though the pane reads as at-rest. While a sub-agent owns
    the foreground, Claude prints inline past-tense sub-task summaries
    ("✻ Explored for 2m 10s") that look like turn-completion lines, and the
    parent's live working line is frequently scrolled out of the ``-S -30``
    capture window — so neither the working-line nor completion gate can tell
    "turn done" from "sub-agent still running". ``sub_agent_count`` is tracked
    independently (hook path + task-file scan) and is the authoritative signal.
    This is the idle-while-coding bug where voxherd #1 ran an Explore sub-agent
    yet showed IDLE on the dashboard/lens.

    Otherwise: idle immediately when effectively idle (bare prompt / finished
    turn, no working line), else only after ``idle_timeout`` seconds of no
    detected activity. Extracted as a pure helper so the poll-loop decision is
    unit-testable (mirrors ``_should_overwrite_activity_type``)."""
    if has_live_subagents:
        return False
    if effective_idle:
        return True
    return inactive_seconds > idle_timeout


def _stranded_subagent_decision(
    hook_count: int,
    task_count: int,
    prev_misses: int,
    threshold: int = _SUBAGENT_STRANDED_THRESHOLD,
) -> tuple[bool, int]:
    """Decide whether hook-tracked sub-agent entries are stranded and should be
    cleared, given the independent task-file ``in_progress`` count.

    Hook tracking (SubagentStart/Stop) is real-time and normally authoritative,
    but a dropped SubagentStop POST strands the entry forever. Task files are an
    independent ground truth — a live Task sub-agent always has an ``in_progress``
    task file. So when hooks claim agents (``hook_count > 0``) but task files
    report zero for ``threshold`` consecutive scans, the entries are stranded.

    Returns ``(should_clear, new_misses)``. The consecutive-miss counter is
    debounced to absorb the SubagentStart→task-file-write race and brief gaps
    between sequential sub-agents — any scan with ``task_count > 0`` resets it.
    Extracted as a pure helper so the debounce is unit-testable."""
    if hook_count == 0 or task_count > 0:
        return False, 0
    new_misses = prev_misses + 1
    return new_misses >= threshold, new_misses


def _resolve_activity_type(session_id: str, detected: str | None, now: float) -> str:
    """Apply sticky hold logic: keep the last specific activity type for a few
    seconds instead of immediately falling back to thinking."""
    if detected is not None and detected != "thinking":
        # Specific activity detected — update sticky state
        _STICKY_ACTIVITY[session_id] = (detected, now)
        return detected

    # No specific match or just "thinking" — check sticky hold
    if session_id in _STICKY_ACTIVITY:
        held_type, held_time = _STICKY_ACTIVITY[session_id]
        if now - held_time < _STICKY_HOLD_SECONDS:
            return held_type  # hold the previous specific type
        # Expired — clean up
        del _STICKY_ACTIVITY[session_id]

    return detected or "thinking"


_SHELL_COMMANDS = frozenset({
    "bash", "zsh", "fish", "sh", "dash", "tcsh", "csh", "login", "-bash", "-zsh",
})

# Patterns that identify assistant status bar lines (bottom of terminal).
# These should be skipped when extracting the activity snippet.
# Covers Claude Code, Codex, and Gemini CLI chrome.
_STATUS_BAR_RE = re.compile(
    r"permissions on|"               # permission mode line: "bypass permissions on · 1 bash"
    r"auto-compact|"                 # context line: "Context left until auto-compact: 9%"
    r"\d+%\s*context\s*(?:used|left|remaining)|"  # warning row: "42% context used"
    r"shift\+tab to cycle|"          # hint line
    r"ctrl-g to edit in|"            # plan-mode editor hint: "ctrl-g to edit in Vim · ~/.claude/plans/..."
    r"Opus \d|Sonnet \d|Haiku \d|"   # Claude model info: "Opus 4.6 $113.77 ..."
    r"^\$\d+\.\d+\s|"               # cost at start of line
    r"^\d+\.?\d*k/\d+|"             # token count: "150.9k/200k"
    r"\+\d+\s*completed|"           # task progress: "... +10 completed"
    r"^\d+ tasks? \(\d+ done|"      # task count: "4 tasks (0 done, 4 open)"
    # Task truncation tail — handles both shapes:
    #   "… +5 pending, 165 completed"                 (2 counts)
    #   "… +1 in progress, 8 pending, 134 completed"  (3 counts)
    r"^\s*(?:…|\.{3})\s*\+\d+\s+(?:in progress|pending|completed)|"
    r"press up to edit|"            # hint: "Press up to edit queued messages"
    r"blocked by #\d+|"             # task dependency: "› blocked by #113"
    r"twice to enable|"             # wrapped hint: "...twice to enable."
    r"^Tip:|"                       # tip lines: "Tip: Use Plan Mode..."
    r"Discombobulating|"            # Claude Code thinking animation
    r"thought for \d+s|"            # thinking indicator: "(59s · ... thought for 2s)"
    r"\w+ for \d+[ms]\b|"           # completion timing: "✻ Worked for 6m 36s", "Sautéed for 53s"
    # Codex-specific chrome
    r"codex>|"                       # Codex prompt
    r"tokens used|"                  # Codex token counter
    r"thread-id:|"                   # Codex thread info
    # Gemini-specific chrome
    r"gemini>|"                      # Gemini prompt
    r"Gemini \d|"                    # Gemini model info: "Gemini 2.5 ..."
    r"tokens remaining",             # Gemini token counter
    re.IGNORECASE,
)

# Characters used by Claude Code for task list checkboxes.
# U+25FB (◻ WHITE MEDIUM SQUARE) is the actual one used by Claude Code.
_TASK_CHECKBOX_CHARS = frozenset(
    "\u25a1"  # □ WHITE SQUARE
    "\u25fb"  # ◻ WHITE MEDIUM SQUARE (Claude Code uses this)
    "\u25a2"  # ▢ WHITE SQUARE WITH ROUNDED CORNERS
    "\u2610"  # ☐ BALLOT BOX
    "\u2611"  # ☑ BALLOT BOX WITH CHECK
    "\u2612"  # ☒ BALLOT BOX WITH X
    "\u2713"  # ✓ CHECK MARK
    "\u2714"  # ✔ HEAVY CHECK MARK
    "\u25a0"  # ■ BLACK SQUARE (filled — Claude Code uses for in-progress)
    "\u25fc"  # ◼ BLACK MEDIUM SQUARE
    "\u25aa"  # ▪ BLACK SMALL SQUARE
    "\u25ae"  # ▮ BLACK VERTICAL RECTANGLE
)


def _is_status_bar_line(line: str) -> bool:
    """Return True if *line* looks like assistant UI chrome (not real output).

    Handles status bar patterns from Claude Code, Codex, and Gemini CLI.

    Claude Code's terminal layout (bottom-up):
      ⏵⏵ bypass permissions on · 1 bash     ← permission mode
      🤖 Opus 4.6 $114.45 150k/200k ...     ← model / cost / context
      ────────────────────────────────────   ← separator
      ❯                                     ← prompt
      ────────────────────────────────────   ← separator
      (actual content above here)
    Also filters: task list items (◻/☑), prompt lines (› ...), hints.
    """
    if _STATUS_BAR_RE.search(line):
        return True
    # Prompt characters (bare or with text = user input echo)
    if line in (">", ">>>", "\u276f"):  # ❯
        return True
    # Lines starting with › or ❯ followed by text = user prompt or hint
    if line.startswith("\u203a") or line.startswith("\u276f"):  # › or ❯
        return True
    # Lines starting with ✻ (U+273B) = completion timing: "✻ Worked for 6m 36s"
    if line.startswith("\u273b"):
        return True
    # Task list items — Claude Code uses ◻ (U+25FB WHITE MEDIUM SQUARE)
    if line and line[0] in _TASK_CHECKBOX_CHARS:
        return True
    # Separator lines (box-drawing characters only)
    stripped = line.replace(" ", "")
    if stripped and all(c in "\u2500\u2501\u2502\u2503\u2504\u2505\u2508\u2509\u254c\u254d\u2550\u2551\u2574\u2576\u2578\u257a" for c in stripped):
        return True
    return False


# Minimum length for a line to be considered "meaningful" (not a wrapped fragment).
_MIN_SNIPPET_LEN = 12

# Patterns that indicate a line starts a meaningful thought (not a wrapped tail).
_MEANINGFUL_START_RE = re.compile(
    r"^(?:"
    r"\d+[\.\)]\s|"       # numbered list: "1. " or "1) "
    r"[●•▸▹▪◆◇▶-]\s|"    # bullet points
    r"[A-Z]|"             # starts with capital letter (sentence start)
    r"Changes |"           # "Changes made to..."
    r"Added |"             # "Added X..."
    r"Updated |"           # "Updated Y..."
    r"Fixed |"             # "Fixed Z..."
    r"Removed |"           # "Removed W..."
    r"Created |"           # "Created V..."
    r"Modified |"          # "Modified U..."
    r"Implemented |"       # "Implemented T..."
    r"Refactored |"        # "Refactored S..."
    r"The |"               # "The signal appears..."
    r"✓\s|"               # check mark items
    r"[`'\"]"              # starts with code/quote
    r")"
)


def _extract_snippet(clean: str) -> tuple[str, str]:
    """Extract the best activity snippet and terminal preview from tmux captured text.

    Walks lines bottom-up, skipping blanks and status bar chrome.
    Prefers lines that look like meaningful content (sentences, list items)
    over short wrapped continuation fragments.

    Returns (snippet, terminal_preview) where terminal_preview is the last
    6-8 non-chrome lines joined by newlines (for hover tooltip display).
    """
    candidates: list[str] = []
    for line in reversed(clean.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if _is_status_bar_line(stripped):
            continue
        candidates.append(stripped)
        # Collect a small window of candidates (first 8 non-chrome lines)
        if len(candidates) >= 8:
            break

    # Build terminal preview from candidates (reverse to chronological order)
    preview = "\n".join(reversed(candidates)) if candidates else ""

    if not candidates:
        return "", ""

    # First pass: find the first (most recent) line that looks like a real
    # sentence or list item -- long enough and starts meaningfully.
    for c in candidates:
        if len(c) >= _MIN_SNIPPET_LEN and _MEANINGFUL_START_RE.match(c):
            return c, preview

    # Second pass: take the first line that's at least _MIN_SNIPPET_LEN chars
    # (even without a meaningful start -- could be a tool output line).
    for c in candidates:
        if len(c) >= _MIN_SNIPPET_LEN:
            return c, preview

    # Fallback: just return the most recent non-chrome line (original behavior).
    return candidates[0], preview


def _is_claude_code_process(fg_cmd: str) -> bool:
    """Return True if the foreground command looks like Claude Code.

    Claude Code reports its version string (e.g. "2.1.42") as the process
    name. We match on a semver-like pattern to distinguish it from other
    processes like node, python, etc.
    """
    return infer_assistant_from_process(fg_cmd) == "claude"


async def _pane_fg_command(target: str) -> str:
    """Return the foreground command in a tmux pane (e.g. 'claude', 'bash')."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "display-message", "-t", target,
            "-p", "#{pane_current_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip().lower()
    except Exception:
        return ""


async def _tmux_pane_path(session_name: str) -> str:
    """Get the current working directory of a tmux session's first pane."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "display-message", "-t", session_name, "-p", "#{pane_current_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Activity poll loop
# ---------------------------------------------------------------------------


async def _activity_poll_loop() -> None:
    """Poll tmux for all active sessions every ~1.5s and broadcast snippet changes."""
    # Lazy import to break the activity ↔ session_status / choice_detector
    # circular import (see note next to the import block at module top).
    from bridge import session_status, choice_detector

    sub_agent_poll_counter = 0  # scan task files every 3rd iteration (~4.5s)
    discovery_poll_counter = 0  # rediscover tmux sessions every 20th iteration (~30s)
    try:
        while True:
            await asyncio.sleep(1.5)
            now = asyncio.get_event_loop().time()
            for session in list(sessions.get_all_sessions().values()):
                if not session.tmux_target:
                    continue
                try:
                    # First check if the tmux pane still exists at all
                    pane_alive = await sessions._tmux_pane_exists(session.tmux_target)
                    if not pane_alive:
                        # Increment strike counter — only remove after 3 consecutive failures
                        # to tolerate transient tmux errors.
                        session.tmux_check_failures += 1
                        if session.tmux_check_failures < 3:
                            log_event("info", session.project,
                                      f"Pane check failed ({session.tmux_check_failures}/3) for {session.session_id[:12]}...")
                            continue
                        # 3 consecutive failures — pane is truly gone, deregister
                        sid, proj = session.session_id, session.project
                        sessions.remove_session(sid)
                        _STICKY_ACTIVITY.pop(sid, None)
                        _LAST_REAL_ACTIVITY.pop(sid, None)
                        _LAST_CHOICE_FOR_SESSION.pop(sid, None)
                        _CHOICE_MISS_COUNT.pop(sid, None)
                        _LAST_FORM_FOR_SESSION.pop(sid, None)
                        _FORM_MISS_COUNT.pop(sid, None)
                        _SUBAGENT_STRANDED_MISS.pop(sid, None)
                        await _cancel_terminal_subs_for_session(sid, project=proj)
                        log_event("warning", proj, f"Pane gone — deregistered ({sid[:12]}...)")
                        await broadcast_to_ios({
                            "type": "session_removed",
                            "session_id": sid,
                            "project": proj,
                        })
                        await broadcast_to_ios(_state_sync_msg())
                        continue

                    # Pane is alive — reset failure counter
                    session.tmux_check_failures = 0

                    # Check if Claude is still the foreground process.
                    # Claude Code reports its version (e.g. "2.1.38") as the process
                    # name whether actively working or idle at its prompt — so the
                    # version string does NOT indicate exit.  Only an actual shell
                    # (bash/zsh) means Claude Code has truly exited.
                    fg_cmd = await _pane_fg_command(session.tmux_target)
                    is_shell = fg_cmd in _SHELL_COMMANDS or (not fg_cmd and session.tmux_target)

                    # Correct assistant field if the actual process differs
                    # (e.g. session registered as "claude" but running Codex).
                    # Grok 4.5 and Composer share the same binary — don't thrash
                    # between those ids.
                    detected = infer_assistant_from_process(fg_cmd)
                    if (
                        detected
                        and detected != session.assistant
                        and not assistants_compatible(detected, session.assistant)
                    ):
                        session.assistant = detected

                    if is_shell:
                        # Actual shell prompt — the assistant has exited.
                        # Immediately deregister so it disappears from the visualization.
                        sid, proj = session.session_id, session.project
                        sessions.remove_session(sid)
                        _STICKY_ACTIVITY.pop(sid, None)
                        _LAST_REAL_ACTIVITY.pop(sid, None)
                        _LAST_CHOICE_FOR_SESSION.pop(sid, None)
                        _CHOICE_MISS_COUNT.pop(sid, None)
                        _LAST_FORM_FOR_SESSION.pop(sid, None)
                        _FORM_MISS_COUNT.pop(sid, None)
                        _SUBAGENT_STRANDED_MISS.pop(sid, None)
                        await _cancel_terminal_subs_for_session(sid, project=proj)
                        log_event("info", proj, f"{session.assistant.title()} exited — deregistered ({sid[:12]}...)")
                        await broadcast_to_ios({
                            "type": "session_removed",
                            "session_id": sid,
                            "project": proj,
                        })
                        await broadcast_to_ios(_state_sync_msg())
                        continue

                    # --- Capture terminal content & detect activity FIRST ---
                    # We need the content to decide whether to re-activate idle
                    # sessions (Claude at its prompt vs actively working both
                    # show the same foreground process — version string).
                    proc = await asyncio.create_subprocess_exec(
                        "tmux", "capture-pane", "-t", session.tmux_target,
                        "-p", "-S", "-30",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=get_subprocess_env(),
                    )
                    stdout, _ = await proc.communicate()
                    raw = stdout.decode("utf-8", errors="replace")
                    clean = _ANSI_RE.sub("", raw) if raw else ""
                    snippet, terminal_preview = _extract_snippet(clean)
                    detected = _detect_activity_type(clean)
                    activity_type = _resolve_activity_type(session.session_id, detected, now)

                    # Track when sessions last had real terminal activity.
                    mono_now = time.monotonic()
                    if detected is not None:
                        _LAST_REAL_ACTIVITY[session.session_id] = mono_now

                    # Re-activate idle sessions ONLY if the terminal shows
                    # actual activity (spinners, tool calls) AND the idle
                    # prompt is NOT visible.  Tool names in scrollback above
                    # the idle prompt are stale history — they must not
                    # cause re-activation, which would block voice commands
                    # with "session busy" even though nothing is running.
                    idle_cooldown = 10.0  # seconds — prevents flicker right after stop events
                    has_idle_prompt = _has_idle_prompt(clean)
                    # Claude shows a bare ❯ even while working, so the prompt
                    # alone no longer means "idle". A present-tense working
                    # line overrides it — without this, a thinking session
                    # (which detects as active above) still wouldn't re-activate
                    # because has_idle_prompt stayed True. See has_live_working_line.
                    # A turn-completion line ("✻ Churned for 1m 9s") also means
                    # at-rest — that's the "user is typing after a finished
                    # turn" case where the prompt isn't bare.
                    _pane_lines_for_state = clean.splitlines()
                    has_working_line = session_status.has_live_working_line(_pane_lines_for_state)
                    at_rest = has_idle_prompt or session_status.has_completion_line(_pane_lines_for_state)
                    # A session running a sub-agent (Task/Explore) is NOT at rest
                    # even though the pane looks idle: Claude prints inline past-
                    # tense sub-task summaries ("✻ Explored for 2m 10s") that read
                    # as completion lines, and the parent's live working line is
                    # often scrolled out of the -S -30 capture window, so neither
                    # has_working_line nor the completion gate can tell "turn done"
                    # from "sub-agent still running". sub_agent_count is tracked
                    # independently (hook path + task-file scan) and is the
                    # authoritative "work in flight" signal here.
                    has_live_subagents = session.sub_agent_count > 0
                    # Background shells/monitors Claude spawned ("· 2 shells,
                    # 1 monitor still running" on its completion line) keep the
                    # session in flight even when the main agent is parked at a
                    # prompt. sub_agent_count only tracks Task/Explore sub-agents,
                    # not these background tasks, so check the pane directly.
                    has_background_work = session_status.has_live_background_work(_pane_lines_for_state)
                    work_in_flight = has_live_subagents or has_background_work
                    effective_idle = at_rest and not has_working_line and not work_in_flight
                    if session.status != "active":
                        # Re-activation requires REAL detected activity (spinner /
                        # tool call). Background shells alone must NOT flip an
                        # idle session back to active — a long-lived dev server
                        # keeps "· 1 shell still running" on the completion line
                        # for hours and would otherwise override the Stop hook's
                        # idle forever. work_in_flight still gates AUTO-IDLE
                        # below (anti-flap while a turn is genuinely in flight).
                        if detected is not None and not effective_idle:
                            # Real activity in the terminal AND not effectively idle —
                            # re-activate (with cooldown to avoid flicker after stop events).
                            if session.idle_since and (mono_now - session.idle_since) < idle_cooldown:
                                pass  # still in cooldown — don't re-activate yet
                            else:
                                sessions.update_status(session.session_id, "active", activity_type=activity_type)
                                log_event("info", session.project, f"Re-activated idle session ({session.session_id[:12]}...)")
                                await broadcast_to_ios({
                                    "type": "activity_update",
                                    "session_id": session.session_id,
                                    "project": session.project,
                                    "activity_type": activity_type,
                                    "snippet": session.activity_snippet,
                                    "status": "active",
                                })
                    elif detected is None:
                        # Session is "active" but no spinners/tools detected.
                        # Two cases for auto-idling:
                        # 1. Idle prompt is visible — idle immediately (no need to wait)
                        # 2. No idle prompt — wait _IDLE_TIMEOUT_SECONDS before idling
                        #    (might be between tool calls, thinking, etc.)
                        last_real = _LAST_REAL_ACTIVITY.get(session.session_id, 0.0)
                        should_idle = _should_auto_idle(
                            effective_idle=effective_idle,
                            has_live_subagents=work_in_flight,
                            inactive_seconds=mono_now - last_real,
                        )
                        if should_idle:
                            sessions.update_status(session.session_id, "idle")
                            # update_status set session.activity_type="sleeping".
                            # Sync the local variable AND drop the sticky entry
                            # so the post-idle comparison below doesn't re-stamp
                            # the prior "thinking" label (which would leave
                            # status="idle" + activity_type="thinking" — a state
                            # macOS renders as "Thinking" while iOS renders as
                            # "idle" via displayActivityType).
                            activity_type = "sleeping"
                            _STICKY_ACTIVITY.pop(session.session_id, None)
                            reason = "idle prompt detected" if has_idle_prompt else f"no activity for {_IDLE_TIMEOUT_SECONDS:.0f}s"
                            log_event("info", session.project,
                                      f"Auto-idled ({reason})")
                            await broadcast_to_ios({
                                "type": "activity_update",
                                "session_id": session.session_id,
                                "project": session.project,
                                "activity_type": "sleeping",
                                "snippet": session.activity_snippet,
                                "status": "idle",
                            })
                            # Drain queued command if one was waiting
                            await _drain_queued_command(session)

                    snippet_changed = snippet and snippet != session.activity_snippet
                    # See _should_overwrite_activity_type() for why this
                    # is gated on session.status — it's the fix for the
                    # macOS=Thinking / iOS=idle desync bug. Using the
                    # extracted helper instead of inlining the
                    # expression so the regression tests in
                    # test_activity.py::TestActivityTypeGate exercise
                    # the actual production gate.
                    type_changed = _should_overwrite_activity_type(
                        session.status, activity_type, session.activity_type
                    )
                    preview_changed = terminal_preview and terminal_preview != session.terminal_preview
                    if snippet_changed or type_changed or preview_changed:
                        if snippet_changed:
                            session.activity_snippet = snippet
                        if preview_changed:
                            session.terminal_preview = terminal_preview
                        if type_changed:
                            prev_activity = session.activity_type
                            session.activity_type = activity_type
                            # Feed activity changes to narration engine
                            if _state.narration:
                                await _state.narration.on_activity_change(
                                    session_id=session.session_id,
                                    project=session.project,
                                    old_type=prev_activity,
                                    new_type=activity_type,
                                )
                        await broadcast_to_ios({
                            "type": "activity_update",
                            "session_id": session.session_id,
                            "project": session.project,
                            "snippet": snippet or session.activity_snippet,
                            "terminal_preview": session.terminal_preview,
                            "activity_type": session.activity_type,
                            "status": session.status,
                            "sub_agent_count": session.sub_agent_count,
                        })

                    # --- Lens-footer status + numbered-choice detection ---
                    # Derive a status dict from tmux capture (always) +
                    # stream-json buffer (when a dispatched run is in
                    # flight). Broadcast only on edge changes — the
                    # extractors return identical dicts when nothing
                    # interesting has changed, so this naturally
                    # rate-limits sends.
                    pane_lines = clean.splitlines() if clean else []
                    tmux_status = session_status.extract_status_from_tmux(pane_lines)
                    stream_buf = session._stream_json_buffer
                    stream_status = session_status.extract_status_from_stream_json(
                        list(stream_buf) if stream_buf else []
                    )
                    # Sticky: hold the last parsed percent when this frame
                    # has no parseable gauge so the lens chip doesn't flicker.
                    context_pct = session_status.sticky_context_percent(
                        pane_lines, session._last_context_pct
                    )
                    session._last_context_pct = context_pct
                    derived = session_status.derive_status(
                        session, tmux_status, stream_status, context_pct
                    )
                    # Diff key strips elapsed_s — that field ticks every
                    # poll for active sessions (Claude Code's footer
                    # "(54s)" → "(55s)"), and including it would
                    # broadcast `session_status` on every tick across
                    # every active session. Active session count × poll
                    # rate gets noisy fast. iOS animates the counter
                    # locally between snapshots, so a slightly stale
                    # elapsed at idle moments is fine.
                    diff_key = {k: v for k, v in derived.items() if k != "elapsed_s"}
                    last_diff_key = (
                        {k: v for k, v in session._last_broadcast_status.items() if k != "elapsed_s"}
                        if isinstance(session._last_broadcast_status, dict)
                        else None
                    )
                    if diff_key != last_diff_key:
                        session._last_broadcast_status = derived
                        await broadcast_to_ios({
                            "type": "session_status",
                            "session_id": session.session_id,
                            **derived,
                        })

                    # Multi-question AskUserQuestion form detection runs
                    # FIRST and takes precedence over the single-choice
                    # path. When a form is on screen (or within its cancel
                    # debounce) we skip choice detection entirely — a form
                    # is a richer select widget, and its review screen's
                    # Submit/Cancel would otherwise misread as a 2-option
                    # choice prompt.
                    form_active = await _broadcast_question_form(session, pane_lines)

                    # Numbered-choice prompt detection. Edge-emit when a
                    # new prompt appears or the signature changes; emit
                    # ``choice_prompt_cancelled`` when the previously
                    # detected prompt is no longer on screen.
                    #
                    # Note: ``_LAST_CHOICE_FOR_SESSION`` is updated on
                    # EVERY tick that detects a choice, not just edge
                    # ticks. The signature is stable across cursor
                    # movement (focused_index isn't in the hash), so a
                    # TUI focus shift won't trigger a broadcast — but
                    # we still need the latest ``focused_index`` cached
                    # server-side so the commit handler can compute
                    # the arrow-key delta for "tui_select" mode.
                    choice = (
                        None if form_active
                        else choice_detector.detect_choice_prompt(pane_lines)
                    )
                    prev_choice = _LAST_CHOICE_FOR_SESSION.get(session.session_id)
                    if choice is not None:
                        # Reset miss + parse-fail trackers on any detect.
                        _CHOICE_MISS_COUNT.pop(session.session_id, None)
                        _CHOICE_PARSE_FAIL.pop(session.session_id, None)
                        is_new_prompt = (
                            prev_choice is None
                            or choice.signature != prev_choice.signature
                        )
                        # Carry the deep-captured body forward across non-edge
                        # re-detects. Each tick re-detects with only the shallow
                        # ~30-line body; without this it would clobber the fuller
                        # body captured on the rising edge — and the connect-
                        # replay (ws_handler) reads `body` from THIS cache, so a
                        # client connecting after the first tick would otherwise
                        # replay a truncated plan.
                        if (
                            prev_choice is not None
                            and prev_choice.signature == choice.signature
                            and len(prev_choice.body) > len(choice.body)
                        ):
                            choice.body = prev_choice.body
                        _LAST_CHOICE_FOR_SESSION[session.session_id] = choice
                        if is_new_prompt:
                            # Enrich the body with a deeper capture so the lens
                            # "read" view shows the FULL plan, not just the ~30
                            # on-screen rows. Only on the rising edge → cheap.
                            deeper = await _deep_capture_choice_body(
                                session.tmux_target, choice.signature
                            )
                            if deeper:
                                choice.body = deeper
                            log_event(
                                "info", session.project,
                                f"Choice prompt detected — {len(choice.options)} "
                                f"options ({choice.input_mode})"
                            )
                            await broadcast_to_ios({
                                "type": "choice_prompt",
                                "session_id": session.session_id,
                                "project": session.project,
                                "title": choice.title,
                                "options": choice.options,
                                "signature": choice.signature,
                                "input_mode": choice.input_mode,
                                "body": choice.body,
                            })
                    elif prev_choice is not None:
                        # No choice detected this tick AND we previously
                        # emitted one. Increment the miss counter —
                        # only broadcast cancellation after enough
                        # consecutive misses that this isn't a one-tick
                        # render glitch. Without the debounce, the lens
                        # focused-list often vanishes the instant the
                        # user taps to enter it (a poll between
                        # broadcast and tap lands during a transient
                        # render and dismisses the prompt iOS-side).
                        miss_count = _CHOICE_MISS_COUNT.get(
                            session.session_id, 0
                        ) + 1
                        _CHOICE_MISS_COUNT[session.session_id] = miss_count
                        if miss_count >= _CHOICE_MISS_THRESHOLD:
                            _LAST_CHOICE_FOR_SESSION.pop(
                                session.session_id, None
                            )
                            _CHOICE_MISS_COUNT.pop(session.session_id, None)
                            await broadcast_to_ios({
                                "type": "choice_prompt_cancelled",
                                "session_id": session.session_id,
                                "project": session.project,
                            })
                        # else: hold prev_choice; iOS keeps the lens
                        # card up while we wait for the next tick.
                    elif not form_active and choice_detector.has_select_footer(pane_lines):
                        # A select-widget footer is visibly on screen but
                        # we parsed zero options — the prompt will never
                        # reach the lens. Log once per distinct failing
                        # pane (throttled by tail signature) so the
                        # offending capture is recoverable from the bridge
                        # log without spamming every poll tick.
                        sig = hashlib.sha1(
                            "\n".join(pane_lines[-8:]).encode("utf-8")
                        ).hexdigest()[:12]
                        if _CHOICE_PARSE_FAIL.get(session.session_id) != sig:
                            _CHOICE_PARSE_FAIL[session.session_id] = sig
                            log_event(
                                "warning", session.project,
                                "Select-widget footer on screen but no "
                                "options parsed — choice prompt will not "
                                "surface"
                            )
                except asyncio.CancelledError:
                    raise  # let outer handler exit cleanly
                except Exception:
                    pass

            # Periodically rediscover tmux sessions (~every 30s).
            # Catches sessions that started after the bridge, or that were missed
            # at startup (e.g. sync subprocess failed in macOS app bundle context).
            discovery_poll_counter += 1
            if discovery_poll_counter >= 20:
                discovery_poll_counter = 0
                try:
                    newly_found = await _discover_tmux_sessions()
                    if newly_found:
                        log_event("success", "bridge", f"Periodic discovery: found {newly_found} new session(s)")
                        await broadcast_to_ios(_state_sync_msg())
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Used to be `pass` — that swallowed errors when
                    # discovery failed in the bundled .app context, leaving
                    # the user wondering why tmux sessions weren't being
                    # picked up. Surface them loudly so the next time this
                    # breaks we see WHY in the bridge log.
                    log_event("error", "bridge", f"Periodic discovery failed: {type(e).__name__}: {e}")

            # Scan task files for sub-agent counts every ~4.5s (every 3rd iteration).
            # This supplements the hook-based tracking (SubagentStart/SubagentStop)
            # which provides real-time data. Task-file scanning catches agents that
            # started before the bridge was running or whose hooks didn't fire.
            sub_agent_poll_counter += 1
            if sub_agent_poll_counter >= 3:
                sub_agent_poll_counter = 0
                try:
                    all_sessions = sessions.get_all_sessions()
                    counts = await asyncio.to_thread(
                        sub_agent_monitor.get_all_sub_agent_counts, dict(all_sessions)
                    )
                    for sid, (task_count, task_list) in counts.items():
                        session = all_sessions.get(sid)
                        if session is None:
                            continue
                        # Hook-tracked sub-agents take priority — they're real-time.
                        # Only use task-file data if hooks aren't providing anything.
                        hook_count = len(session.live_subagents)
                        if hook_count > 0:
                            # Hooks claim live sub-agents and normally win. BUT a
                            # dropped SubagentStop POST strands the entry forever,
                            # pinning sub_agent_count > 0 — and _should_auto_idle
                            # then blocks this session from ever auto-idling. Cross-
                            # check against task files (independent ground truth) and
                            # clear the entries once they're confirmed stranded, so
                            # the session can self-heal to idle.
                            should_clear, misses = _stranded_subagent_decision(
                                hook_count, task_count, _SUBAGENT_STRANDED_MISS.get(sid, 0),
                            )
                            if should_clear:
                                session.live_subagents.clear()
                                session.sub_agent_count = 0
                                session.sub_agent_tasks = None
                                _SUBAGENT_STRANDED_MISS.pop(sid, None)
                                log_event("warning", session.project,
                                          f"Cleared stranded sub-agent tracking — task files "
                                          f"show none running ({sid[:12]}...)")
                                await broadcast_to_ios({
                                    "type": "sub_agent_update",
                                    "session_id": sid,
                                    "project": session.project,
                                    "sub_agent_count": 0,
                                    "sub_agent_tasks": [],
                                })
                            elif misses > 0:
                                _SUBAGENT_STRANDED_MISS[sid] = misses
                            else:
                                _SUBAGENT_STRANDED_MISS.pop(sid, None)
                            continue
                        _SUBAGENT_STRANDED_MISS.pop(sid, None)
                        prev_count = session.sub_agent_count
                        if task_count != prev_count or task_list != (session.sub_agent_tasks or []):
                            session.sub_agent_count = task_count
                            session.sub_agent_tasks = task_list if task_list else None
                            await broadcast_to_ios({
                                "type": "sub_agent_update",
                                "session_id": sid,
                                "project": session.project,
                                "sub_agent_count": task_count,
                                "sub_agent_tasks": task_list,
                            })
                            if task_count > prev_count:
                                log_event("info", session.project,
                                          f"Sub-agents (task files): {task_count} active (was {prev_count})")
                            elif task_count < prev_count and task_count == 0:
                                log_event("info", session.project,
                                          "All sub-agents completed (task files)")
                except asyncio.CancelledError:
                    raise  # let outer handler exit cleanly
                except Exception:
                    pass
    except asyncio.CancelledError:
        log_event("info", "bridge", "Activity poll loop cancelled — shutting down")
        return


# ---------------------------------------------------------------------------
# Periodic prune
# ---------------------------------------------------------------------------


async def _periodic_prune() -> None:
    """Prune dead sessions every 30 seconds and sync iOS clients.

    Also removes idle sessions where Claude Code has exited (pane shows
    a shell prompt), so they disappear from the visualization.
    """
    while True:
        await asyncio.sleep(30)
        # prune_dead is now async — tmux checks use asyncio subprocesses.
        removed = await sessions.prune_dead()
        if removed:
            log_event("warning", "bridge", f"Pruned {len(removed)} dead session(s)")
            for sid, project in removed:
                # ``prune_dead`` already removed the Session from the
                # manager, but module-level per-session caches still
                # carry the stale key. Pop them here so we don't leak
                # entries — same cleanup pattern as the exit/pane-gone
                # paths above in `_activity_poll_loop`.
                _STICKY_ACTIVITY.pop(sid, None)
                _LAST_REAL_ACTIVITY.pop(sid, None)
                _LAST_CHOICE_FOR_SESSION.pop(sid, None)
                _CHOICE_MISS_COUNT.pop(sid, None)
                _LAST_FORM_FOR_SESSION.pop(sid, None)
                _FORM_MISS_COUNT.pop(sid, None)
                _SUBAGENT_STRANDED_MISS.pop(sid, None)
                await _cancel_terminal_subs_for_session(sid, project=project)
                await broadcast_to_ios({
                    "type": "session_removed",
                    "session_id": sid,
                    "project": project,
                })

        # Check idle sessions: if Claude Code has truly exited (pane shows
        # a shell prompt, NOT a version string), deregister the session.
        # Version string = Claude still running at prompt = keep registered.
        exited: list[tuple[str, str]] = []
        for session in list(sessions.get_all_sessions().values()):
            if session.status != "idle" or not session.tmux_target:
                continue
            fg_cmd = await _pane_fg_command(session.tmux_target)
            is_shell = fg_cmd in _SHELL_COMMANDS or (not fg_cmd and session.tmux_target)
            if is_shell:
                exited.append((session.session_id, session.project))

        for sid, project in exited:
            sessions.remove_session(sid)
            _STICKY_ACTIVITY.pop(sid, None)
            _LAST_REAL_ACTIVITY.pop(sid, None)
            _LAST_CHOICE_FOR_SESSION.pop(sid, None)
            _CHOICE_MISS_COUNT.pop(sid, None)
            _LAST_FORM_FOR_SESSION.pop(sid, None)
            _FORM_MISS_COUNT.pop(sid, None)
            _SUBAGENT_STRANDED_MISS.pop(sid, None)
            await _cancel_terminal_subs_for_session(sid, project=project)
            log_event("warning", "bridge", f"Deregistered exited session: {project} ({sid[:12]}...)")
            await broadcast_to_ios({
                "type": "session_removed",
                "session_id": sid,
                "project": project,
            })

        if removed or exited:
            await broadcast_to_ios(_state_sync_msg())


# ---------------------------------------------------------------------------
# Tmux session discovery
# ---------------------------------------------------------------------------


async def _discover_tmux_sessions() -> int:
    """Scan tmux for assistant CLI sessions not yet registered with the bridge.

    Called at startup to pick up sessions that started before the bridge.
    Registers them with a temporary session_id — when the real session hooks
    fire (next stop/start event), the real session_id replaces the temp one
    via tmux_target deduplication.

    Returns the number of newly discovered sessions.
    """
    discovered = 0
    try:
        all_tmux = await tmux_manager.async_list_sessions()
    except Exception as e:
        # Used to silently propagate to the caller's bare `except: pass`,
        # leaving the user with no signal that discovery never ran.
        log_event("error", "bridge", f"Discovery: async_list_sessions failed: {type(e).__name__}: {e}")
        return 0

    # Build set of tmux session names already registered
    registered_targets: set[str] = set()
    for s in sessions.get_all_sessions().values():
        if s.tmux_target:
            registered_targets.add(s.tmux_target.split(":")[0])

    # Load configured projects for directory lookup
    configured = {p["name"].lower(): p for p in _load_projects()}

    for ts in all_tmux:
        name = ts["name"]
        # Skip bridge sessions and sessions already in the registry. These
        # are the common paths; no logging — they fire every 30s and would
        # spam the bridge log.
        if name in (tmux_manager.BRIDGE_SESSION, "bridge"):
            continue
        if name in registered_targets:
            continue

        # Check if a supported assistant is running in this pane.
        # Other processes (node, python, etc.) should NOT be registered.
        tmux_target = f"{name}:0.0"
        fg_cmd = await _pane_fg_command(tmux_target)
        assistant = infer_assistant_from_process(fg_cmd)
        if not assistant:
            continue

        # Resolve project directory: prefer projects.json, fall back to pane CWD
        cfg = configured.get(name.lower())
        if cfg:
            project_dir = cfg["dir"]
            project_name = cfg["name"]
        else:
            project_dir = await _tmux_pane_path(name)
            project_name = name  # use tmux session name as project name

        if not project_dir:
            # Worth surfacing: a pane that looks like an assistant but
            # whose CWD couldn't be read is a real misconfiguration, not
            # noise — the user needs to know we saw it and skipped.
            log_event("warning", "bridge", f"Discovery: skip '{name}' (could not resolve project_dir)")
            continue

        # Generate a temporary session_id — will be replaced when the real
        # session hook fires and deduplicates by tmux_target.
        temp_id = f"discovered-{secrets.token_hex(8)}"
        try:
            session, _ = sessions.register_session(
                temp_id,
                project_name,
                project_dir,
                tmux_target=tmux_target,
                assistant=assistant,
            )
            # Mark as active since an assistant process is running.
            sessions.update_status(temp_id, "active", activity_type="working")
        except Exception as e:
            # register_session can fail validation (project name, path).
            # Don't let one bad entry block the rest of the scan, and log
            # the reason so the next missing-session report has a trail.
            log_event("error", "bridge", f"Discovery: register '{name}' failed: {type(e).__name__}: {e}")
            continue
        log_event("success", project_name, f"Discovered {assistant} in tmux '{name}'")
        discovered += 1

    return discovered
