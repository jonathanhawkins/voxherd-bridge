"""Activity detection, polling loops, session discovery, and pruning.

Extracted from ``bridge_server.py`` — these background loops poll tmux for
terminal output, detect what Claude Code is doing, broadcast changes to iOS,
prune dead sessions, and discover pre-existing tmux sessions at startup.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time

from bridge.env_utils import get_subprocess_env
from bridge import tmux_manager
from bridge import sub_agent_monitor
from bridge.server_state import (
    sessions, broadcast_to_ios, _state_sync_msg, log_event,
    _STICKY_ACTIVITY, _cancel_terminal_subs_for_session,
)
import bridge.server_state as _state
from bridge.validation import _ANSI_RE, _load_projects
from bridge.assistant import infer_assistant_from_process


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


def _detect_activity_type(text: str) -> str | None:
    """Detect what the assistant (Claude/Codex/Gemini) is doing from terminal output.

    Returns the detected activity type, or None if nothing specific was found
    (caller should apply sticky logic).
    """
    # Check for spinner characters first — these are the strongest signal
    # that the assistant is actively working.  All three CLIs use braille
    # spinners or similar Unicode animation characters.
    has_spinner = bool(re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◑◒◓⏳]", text))

    # If the assistant's idle prompt appears near the bottom AND there
    # are no spinners, it's waiting for input.  Tool names in scrollback
    # above the prompt are stale history, not current activity.
    if not has_spinner and _has_idle_prompt(text):
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
    # Spinner detected but no tool patterns — assistant is thinking
    if has_spinner:
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
    r"shift\+tab to cycle|"          # hint line
    r"Opus \d|Sonnet \d|Haiku \d|"   # Claude model info: "Opus 4.6 $113.77 ..."
    r"^\$\d+\.\d+\s|"               # cost at start of line
    r"^\d+\.?\d*k/\d+|"             # token count: "150.9k/200k"
    r"\+\d+\s*completed|"           # task progress: "... +10 completed"
    r"^\d+ tasks? \(\d+ done|"      # task count: "4 tasks (0 done, 4 open)"
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
                    detected = infer_assistant_from_process(fg_cmd)
                    if detected and detected != session.assistant:
                        session.assistant = detected

                    if is_shell:
                        # Actual shell prompt — the assistant has exited.
                        # Immediately deregister so it disappears from the visualization.
                        sid, proj = session.session_id, session.project
                        sessions.remove_session(sid)
                        _STICKY_ACTIVITY.pop(sid, None)
                        _LAST_REAL_ACTIVITY.pop(sid, None)
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
                    if session.status != "active":
                        if detected is not None and not has_idle_prompt:
                            # Real activity in the terminal AND no idle prompt —
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
                        should_idle = False
                        if has_idle_prompt:
                            # Idle prompt visible and no active tool patterns —
                            # Claude Code is definitely waiting for input.
                            should_idle = True
                        else:
                            last_real = _LAST_REAL_ACTIVITY.get(session.session_id, 0.0)
                            if mono_now - last_real > _IDLE_TIMEOUT_SECONDS:
                                should_idle = True
                        if should_idle:
                            sessions.update_status(session.session_id, "idle")
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
                    type_changed = activity_type != session.activity_type
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
                except Exception:
                    pass

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
                            # Hooks are active — skip task-file overwrite
                            continue
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
    all_tmux = await tmux_manager.async_list_sessions()

    # Build set of tmux session names already registered
    registered_targets: set[str] = set()
    for s in sessions.get_all_sessions().values():
        if s.tmux_target:
            registered_targets.add(s.tmux_target.split(":")[0])

    # Load configured projects for directory lookup
    configured = {p["name"].lower(): p for p in _load_projects()}

    for ts in all_tmux:
        name = ts["name"]
        # Skip bridge sessions
        if name in (tmux_manager.BRIDGE_SESSION, "bridge"):
            continue
        # Skip if any session already registered for this tmux session
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
            continue

        # Generate a temporary session_id — will be replaced when the real
        # session hook fires and deduplicates by tmux_target.
        temp_id = f"discovered-{secrets.token_hex(8)}"
        session, _ = sessions.register_session(
            temp_id,
            project_name,
            project_dir,
            tmux_target=tmux_target,
            assistant=assistant,
        )
        # Mark as active since an assistant process is running.
        sessions.update_status(temp_id, "active", activity_type="working")
        log_event("success", project_name, f"Discovered {assistant} in tmux '{name}'")
        discovered += 1

    return discovered
