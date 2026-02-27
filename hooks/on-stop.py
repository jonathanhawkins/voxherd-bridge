#!/usr/bin/env python3
"""VoxHerd on-stop hook — generates AI summary and notifies bridge server.

Reads hook JSON from stdin, parses the JSONL transcript to extract structured
context (files changed, commands run, key actions), sends to Haiku for a
concise voice summary, and POSTs the result to the bridge.

Must NEVER block Claude Code — all external calls have timeouts and errors
are swallowed.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Platform-specific imports
_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl

# Ignore SIGPIPE — if Claude Code closes the pipe, exit gracefully
# instead of crashing with BrokenPipeError.
# SIGPIPE does not exist on Windows.
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

# ---------------------------------------------------------------------------
# Recursion guard — prevent hooks firing when we spawn `claude -p`
# ---------------------------------------------------------------------------

if os.environ.get("VOXHERD_HOOK_RUNNING"):
    sys.exit(0)
os.environ["VOXHERD_HOOK_RUNNING"] = "1"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRIDGE_URL = "http://localhost:7777/api/events"

# Config directory: %APPDATA%\VoxHerd on Windows, ~/.voxherd on macOS/Linux
if _IS_WINDOWS:
    _CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / "VoxHerd"
else:
    _CONFIG_DIR = Path.home() / ".voxherd"

LOG_DIR = _CONFIG_DIR / "logs"
DEBUG_LOG = LOG_DIR / "on-stop-debug.log"
DEBOUNCE_FILE = LOG_DIR / ".stop-debounce"
DEBOUNCE_SECONDS = 3
HAIKU_TIMEOUT = 25  # seconds

LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

# Truncate debug log if over 500KB (keep tail)
try:
    if DEBUG_LOG.exists() and DEBUG_LOG.stat().st_size > 500_000:
        lines = DEBUG_LOG.read_text().splitlines()
        DEBUG_LOG.write_text("\n".join(lines[-500:]) + "\n")
except Exception:
    pass


def _safe_open_append(path: Path) -> int:
    """Open a file for appending, refusing to follow symlinks.

    On Windows, O_NOFOLLOW is not available -- symlinks are rare enough
    in %APPDATA% that we accept the trade-off.
    """
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if not _IS_WINDOWS:
            flags |= os.O_NOFOLLOW
        fd = os.open(str(path), flags, 0o600)
        return fd
    except OSError:
        return -1


def log(msg: str) -> None:
    """Append a timestamped debug line (refuses to follow symlinks)."""
    fd = _safe_open_append(DEBUG_LOG)
    if fd < 0:
        return
    try:
        os.write(fd, f"[{datetime.now()}] {msg}\n".encode())
    except Exception:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Debounce — skip if another stop fired within DEBOUNCE_SECONDS
# ---------------------------------------------------------------------------

def should_debounce() -> bool:
    if _IS_WINDOWS:
        return _should_debounce_windows()
    return _should_debounce_unix()


def _should_debounce_unix() -> bool:
    try:
        fd = os.open(str(DEBOUNCE_FILE), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return True  # another hook holds the lock -- debounce
        try:
            content = os.read(fd, 64).decode().strip()
            if content:
                last = float(content)
                if time.time() - last < DEBOUNCE_SECONDS:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                    return True
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(time.time()).encode())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except Exception:
        pass
    return False


def _should_debounce_windows() -> bool:
    """Windows debounce using msvcrt.locking for file locks."""
    try:
        # Use 'r+b' if file exists, create if not
        debounce_str = str(DEBOUNCE_FILE)
        if not os.path.exists(debounce_str):
            with open(debounce_str, "w") as f:
                f.write("")
        fh = open(debounce_str, "r+b")
        try:
            # Try non-blocking exclusive lock on first byte
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except (OSError, IOError):
            fh.close()
            return True  # another hook holds the lock -- debounce
        try:
            content = fh.read().decode().strip()
            if content:
                last = float(content)
                if time.time() - last < DEBOUNCE_SECONDS:
                    # Unlock and close
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    fh.close()
                    return True
            fh.seek(0)
            fh.truncate()
            fh.write(str(time.time()).encode())
            fh.flush()
        finally:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
            fh.close()
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Read stdin
# ---------------------------------------------------------------------------

try:
    raw = sys.stdin.read()
    hook_input = json.loads(raw)
except Exception as e:
    log(f"Failed to read stdin: {e}")
    sys.exit(0)

session_id = hook_input.get("session_id", "")
cwd = hook_input.get("cwd", "")
transcript_path = hook_input.get("transcript_path", "")
stop_reason = hook_input.get("stop_reason", "completed")
assistant = str(os.environ.get("VOXHERD_HOOK_ASSISTANT", "claude")).strip().lower() or "claude"

project_dir = cwd
project_name = os.path.basename(project_dir) if project_dir else "unknown"

log(f"=== on-stop.py invoked ===")
log(f"  session_id={session_id}")
log(f"  project={project_name}")
log(f"  assistant={assistant}")
log(f"  transcript_path={transcript_path}")
log(f"  transcript_exists={os.path.isfile(transcript_path) if transcript_path else False}")

if should_debounce():
    log("  Debounced — skipping summary generation")
    # Still POST with fallback summary so bridge gets the stop event
    summary = "Task completed."
else:
    summary = ""


# ---------------------------------------------------------------------------
# Extract structured context from JSONL transcript
# ---------------------------------------------------------------------------

def _extract_user_text(content) -> str:
    """Extract user text from content (handles both string and list formats)."""
    if isinstance(content, str) and content.strip():
        text = content.strip()
        if text.startswith(("<local-command-caveat>", "<task-notification>", "<system-reminder>")):
            return ""
        return text
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and not text.startswith(("<local-command-caveat>", "<task-notification>", "<system-reminder>")):
                    return text
    return ""


def _allowed_transcript_roots() -> list[str]:
    """Return acceptable transcript root dirs for known assistants.

    On all platforms, assistant CLIs store transcripts under ~/.<name>/.
    On Windows this expands to %USERPROFILE%\\.claude, etc.
    """
    preferred = {
        "claude": "~/.claude",
        "gemini": "~/.gemini",
        "codex": "~/.codex",
    }
    roots: list[str] = []
    primary = preferred.get(assistant)
    if primary:
        roots.append(os.path.realpath(os.path.expanduser(primary)))
    for fallback in ("~/.claude", "~/.gemini", "~/.codex"):
        candidate = os.path.realpath(os.path.expanduser(fallback))
        if candidate not in roots:
            roots.append(candidate)
    # On Windows, also accept APPDATA-based paths (some tools use this)
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            for name in ("claude", "gemini", "codex"):
                candidate = os.path.realpath(os.path.join(appdata, name))
                if candidate not in roots:
                    roots.append(candidate)
    return roots


# Patterns that indicate a string contains secrets — used to filter
# content before sending to Haiku for summarization.
_SECRET_PATTERNS = [
    "API_KEY", "SECRET", "TOKEN", "PASSWORD", "PASSW",
    "Bearer ", "Authorization:",
    "sk-ant-", "sk-", "ghp_", "gho_", "xoxb-", "xoxp-",
    "AKIA",  # AWS access key prefix
    "-----BEGIN",  # PEM keys
    "eyJ",  # JWT tokens (base64 of '{"')
]


def _contains_secret(text: str) -> bool:
    """Return True if text appears to contain a secret/credential."""
    return any(pattern in text for pattern in _SECRET_PATTERNS)


def _redact_secrets(text: str) -> str:
    """Replace lines containing secrets with a redaction notice."""
    lines = text.split("\n")
    result = []
    for line in lines:
        if _contains_secret(line):
            result.append("[redacted — contains credential]")
        else:
            result.append(line)
    return "\n".join(result)


def extract_context(path: str, max_lines: int = 500) -> str:
    """Parse the JSONL transcript and extract context for the CURRENT TURN.

    Turn-aware: finds the last real user message, then only extracts
    files/commands/assistant text from AFTER that point. This prevents
    stale data from earlier turns leaking into the summary.

    Also captures the original user request (from session start) and
    the current turn's user message for Haiku context.
    """
    if not path or not os.path.isfile(path):
        return ""

    # Validate transcript path is under an assistant transcript root
    # and open the fd immediately to eliminate TOCTOU race window.
    try:
        real_path = os.path.realpath(path)
        allowed_roots = _allowed_transcript_roots()
        if not any(real_path.startswith(root + os.sep) for root in allowed_roots):
            log(f"Rejected transcript path outside allowed roots: {path}")
            return ""
        # Open immediately after validation -- no window for symlink swap.
        # O_NOFOLLOW prevents opening symlinks even if one is created after realpath.
        # Not available on Windows, where symlinks are rare in user data dirs.
        open_flags = os.O_RDONLY
        if not _IS_WINDOWS:
            open_flags |= os.O_NOFOLLOW
        fd = os.open(real_path, open_flags)
    except (OSError, Exception):
        return ""

    # Cap file size to prevent memory exhaustion (50MB)
    try:
        size_mb = os.fstat(fd).st_size / (1024 * 1024)
        if size_mb > 50:
            log(f"Transcript too large ({size_mb:.1f}MB), skipping detailed parsing")
            os.close(fd)
            return ""
    except OSError:
        os.close(fd)
        return ""

    try:
        with os.fdopen(fd, "r") as f:
            lines = f.readlines()
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        return ""

    # --- Pass 1: Find the FIRST user message (the original request) ---
    first_user_request = ""
    for line in lines[:80]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _extract_user_text(msg.get("content", ""))
        if text and len(text) > 5:
            first_user_request = text.split("\n")[0][:150].lstrip("#-*>`").strip()
            break

    # --- Pass 2: Find the last real user message to scope current turn ---
    tail = lines[-max_lines:]
    last_user_idx = -1
    last_user_text = ""

    for i, line in enumerate(tail):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _extract_user_text(msg.get("content", ""))
        if text and len(text) > 5:
            last_user_idx = i
            last_user_text = text.split("\n")[0][:150]

    # --- Pass 3: Extract actions ---
    # Files/commands: scoped to current turn (after last user message)
    # Assistant text: scanned from FULL tail (so conversational responses are always captured)
    current_turn = tail[last_user_idx + 1:] if last_user_idx >= 0 else tail[-50:]

    files_changed: list[str] = []
    commands_run: list[str] = []
    git_commits: list[str] = []

    # Scan current turn for files, commands, and git commits
    for line in current_turn:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {})
            if name in ("Edit", "Write", "MultiEdit"):
                fp = inp.get("file_path", "")
                if fp:
                    short = os.path.basename(fp)
                    if short not in files_changed:
                        files_changed.append(short)
            elif name == "Bash":
                cmd = inp.get("command", "").strip()
                if cmd:
                    # Extract git commit messages
                    if "git commit" in cmd and "-m" in cmd:
                        m = re.search(r'-m\s+["\'](.+?)["\']', cmd)
                        if not m:
                            m = re.search(r'-m\s+"?\$\(cat <<.*?EOF\n(.+?)(?:\n|EOF)', cmd, re.DOTALL)
                        if m:
                            commit_msg = m.group(1).split("\n")[0].strip()
                            if commit_msg and not _contains_secret(commit_msg):
                                git_commits.append(commit_msg[:80])
                    if len(cmd) < 120:
                        skip_cmds = ["cat ", "head ", "tail ", "echo ", "ls ", "pwd"]
                        if not any(s in cmd for s in skip_cmds):
                            if not _contains_secret(cmd):
                                commands_run.append(cmd)

    # Scan FULL tail for assistant text blocks.
    # Collect ALL substantial assistant explanations (like the other app's
    # assistant_summaries) plus track the last turn's texts specifically.
    all_assistant_texts: list[str] = []
    last_assistant_turn_texts: list[str] = []
    current_turn_texts: list[str] = []
    last_role = ""

    for line in tail:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role != "assistant" and last_role == "assistant" and current_turn_texts:
            last_assistant_turn_texts = current_turn_texts[:]
            current_turn_texts = []
        last_role = role

        if role != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and len(text) > 50 and not text.startswith("```"):
                    # Skip text blocks that contain secrets
                    if not _contains_secret(text):
                        all_assistant_texts.append(text[:300])
                if text and len(text) > 10:
                    if not _contains_secret(text):
                        current_turn_texts.append(text[:300])

    if last_role == "assistant" and current_turn_texts:
        last_assistant_turn_texts = current_turn_texts[:]

    # Build structured context with labeled sections (matches other app's format).
    # Prioritize Claude's own explanations — they're the best summary material.
    parts: list[str] = []

    # Section 1: Claude's work summary (most recent substantial text blocks)
    # This is the highest-signal context — Claude explaining what it did.
    if all_assistant_texts:
        recent = all_assistant_texts[-3:]  # Last 3 substantial responses
        parts.append("Claude's work summary: " + " ".join(recent))

    # Section 2: What the user asked
    if last_user_text:
        parts.append("User request: " + last_user_text)
    elif first_user_request:
        parts.append("Original request: " + first_user_request)

    # Section 3: Files modified (secondary context)
    if files_changed:
        parts.append("Files: " + ", ".join(files_changed[:10]))

    # Section 4: Git commits (strong signal of what was accomplished)
    if git_commits:
        parts.append("Git commits: " + "; ".join(git_commits[:3]))

    # Section 5: Commands run (tertiary context)
    if commands_run:
        interesting = [c for c in commands_run if not c.startswith((
            "xcodebuild", "xcrun devicectl", "npx vite", "npm ",
        ))]
        if interesting:
            parts.append("Commands: " + "; ".join(interesting[:5]))

    # Section 6: Agent's final statement (fallback if no work summary)
    if not all_assistant_texts and last_assistant_turn_texts:
        final_text = last_assistant_turn_texts[-1]
        if len(final_text) > 30:
            parts.append("Agent's final statement: " + final_text[:200])

    if not parts:
        parts.append("Session ran but no clear actions were captured.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fast-path summary (skip Haiku for simple changes)
# ---------------------------------------------------------------------------

def try_fast_summary(path: str) -> str:
    """Generate a summary of the CURRENT TURN only (not the whole session).

    Turn-aware: finds the last user message, then only looks at assistant
    content AFTER it. This prevents stale summaries from earlier edits
    leaking into conversational responses.

    Priority: agent's final message > files+request > files only.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        open_flags = os.O_RDONLY
        if not _IS_WINDOWS:
            open_flags |= os.O_NOFOLLOW
        fd = os.open(path, open_flags)
        with os.fdopen(fd, "r") as f:
            lines = f.readlines()
    except Exception:
        return ""

    # Find the index of the LAST user message in the transcript.
    # Everything after it is the current turn's assistant response.
    last_user_idx = -1
    user_request = ""
    tail = lines[-300:]

    for i, line in enumerate(tail):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role != "user":
            continue
        content = msg.get("content", "")
        # Extract user text (handles both string and list formats)
        text = ""
        if isinstance(content, str) and content.strip():
            text = content.strip()
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    break
        # Skip system-injected messages
        if text and text.startswith(("<local-command-caveat>", "<task-notification>", "<system-reminder>")):
            continue
        if text and len(text) > 5:
            last_user_idx = i
            user_request = text[:120]

    # Files/edits: scoped to current turn (after last user message)
    current_turn = tail[last_user_idx + 1:] if last_user_idx >= 0 else tail[-50:]

    files: list[str] = []
    edit_count = 0

    for line in current_turn:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {})
            if name in ("Edit", "Write", "MultiEdit"):
                fp = inp.get("file_path", "")
                if fp:
                    short = os.path.basename(fp)
                    if short not in files:
                        files.append(short)
                    edit_count += 1

    # Last assistant text: scanned from FULL tail (not scoped to current turn)
    # so conversational responses are always captured
    last_assistant = ""
    for line in tail:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and len(text) > 20:
                    last_assistant = text

    # Only use fast path for simple turns (≤3 files, ≤5 edits)
    if len(files) > 3 or edit_count > 5:
        return ""

    # Best: if the agent's final message starts with a past-tense verb,
    # it's already a natural summary. Use first sentence.
    if last_assistant:
        first_sentence = last_assistant.split("\n")[0].split(". ")[0].strip()
        first_sentence = first_sentence.replace("*", "").replace("`", "").replace("#", "").strip()
        past_verbs = [
            "Fixed", "Updated", "Added", "Removed", "Refactored", "Implemented",
            "Configured", "Deployed", "Created", "Resolved", "Applied", "Installed",
            "Built", "Changed", "Moved", "Renamed", "Merged", "Set", "Enabled",
            "Disabled", "Cleaned", "Optimized", "Simplified", "Replaced",
            "Wrote", "Deleted", "Restored", "Upgraded", "Downgraded", "Converted",
            "Migrated", "Reverted", "Wrapped", "Extracted", "Formatted", "Debugged",
            "Tested", "Verified", "Confirmed", "Connected", "Started", "Stopped",
            "Initialized", "Published", "Documented", "Pushed", "Committed",
            "Deployed", "Installed", "Patched", "Rewrote", "Restructured",
        ]
        if any(first_sentence.startswith(v) for v in past_verbs) and len(first_sentence) < 120:
            return first_sentence + ("." if not first_sentence.endswith(".") else "")

        # Accept sentences that start with a capital letter, are short, contain
        # no markdown, and don't start with known-bad preambles.  This catches
        # natural summaries like "All 4 tests passing now."
        _bad_preambles = [
            "i ", "i'", "based on", "sure", "okay", "certainly", "of course",
            "happy to", "great ", "yes", "no,", "note:", "summary:",
            "here's", "here is", "let me",
        ]
        if (first_sentence and first_sentence[0].isupper()
                and len(first_sentence) < 120
                and "*" not in first_sentence and "`" not in first_sentence
                and "#" not in first_sentence
                and not any(first_sentence.lower().startswith(p) for p in _bad_preambles)):
            return first_sentence + ("." if not first_sentence.endswith(".") else "")

    if len(files) == 1:
        n = edit_count if edit_count else 1
        return f"Updated {files[0]} with {n} change{'s' if n != 1 else ''}."

    if files:
        return f"Updated {', '.join(files[:3])}."

    return ""


# ---------------------------------------------------------------------------
# OpenAI summary (fast alternative to Haiku)
# ---------------------------------------------------------------------------

def generate_openai_summary(context: str, project: str) -> str:
    """Generate summary via OpenAI API (gpt-5-nano -> gpt-4o-mini fallback).

    Uses urllib.request (stdlib) so the hook has no external dependencies.
    Returns raw summary text or empty string on failure.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return ""

    system_prompt = (
        "You create ultra-concise audio summaries of coding agent work. "
        "Format: 1 sentence what was done + 'Next:' + 1 next step. "
        "Be ruthlessly concise, natural tone, no filler. "
        "Start with a past-tense verb. No markdown, no code blocks, no file paths. "
        "Example: 'Fixed the auth bug in login flow. Next: run the tests.'"
    )
    user_prompt = f"Project: {project}\n\n{context}"

    for model in ["gpt-5-nano", "gpt-4o-mini"]:
        try:
            body = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 100,
            }).encode()

            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())

            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "").strip()
                if text:
                    log(f"  OpenAI {model} succeeded: {text[:100]}")
                    return text
            log(f"  OpenAI {model} returned empty response")
        except Exception as e:
            log(f"  OpenAI {model} failed: {e}")
            continue

    return ""


def generate_gemini_summary(context: str, project: str) -> str:
    """Generate summary via Google Gemini API (gemini-2.0-flash).

    Uses urllib.request (stdlib). Checks GEMINI_API_KEY then GOOGLE_API_KEY.
    Returns raw summary text or empty string on failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return ""

    prompt = (
        "You create ultra-concise audio summaries of coding agent work. "
        "Format: 1 sentence what was done + 'Next:' + 1 next step. "
        "Be ruthlessly concise, natural tone, no filler. "
        "Start with a past-tense verb. No markdown, no code blocks, no file paths. "
        "Example: 'Fixed the auth bug in login flow. Next: run the tests.'\n\n"
        f"Project: {project}\n\n{context}"
    )

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/gemini-2.0-flash:generateContent?key={api_key}"
        )
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 100},
        }).encode()

        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        candidates = result.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                text = parts[0].get("text", "").strip()
                if text:
                    log(f"  Gemini API succeeded: {text[:100]}")
                    return text
        log("  Gemini API returned empty response")
    except Exception as e:
        log(f"  Gemini API failed: {e}")

    return ""


def _clean_summary(text: str) -> str:
    """Clean and validate an AI-generated summary for TTS.

    Shared by both OpenAI and Haiku paths — strips markdown, rejects
    refusals/roleplaying/first-person, caps length.
    """
    if not text:
        return ""

    # Strip markdown artifacts
    text = text.replace("*", "").replace("`", "").replace("#", "").replace('"', '')

    # Strip common preamble patterns
    for prefix in ["Here's ", "Here is ", "Summary: ", "Based on ", "The summary is ",
                    "TTS: ", "Output: ", "Answer: "]:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]

    # Reject refusals and roleplaying
    refusal_phrases = [
        "i need more context", "i can't", "i don't have",
        "unable to provide", "not enough context",
        "could you clarify", "i cannot", "i need your",
        "i'd need to", "can you paste", "i've updated",
        "i'll ", "let me ", "here's what",
        "requires your approval", "approve to proceed",
        "would you like", "should i ", "shall i ",
        "do you want", "can you confirm",
        "your permission", "your approval",
        "i'm sorry", "i apologize",
    ]
    if any(p in text.lower() for p in refusal_phrases):
        return ""

    # Reject first-person responses
    if any(text.strip().startswith(p) for p in [
        "I ", "I'", "I've", "I'll", "I'd", "I need", "I can",
    ]):
        return ""

    # Reject bad starts
    bad_starts = [
        "based on ", "sure", "okay",
        "certainly", "of course", "happy to", "great ",
        "yes", "no,", "note:", "summary:",
    ]
    if any(text.lower().startswith(p) for p in bad_starts):
        return ""

    # Reject lowercase start
    if text and text[0].islower():
        return ""

    # Reject questions
    if text.rstrip().endswith("?"):
        return ""

    # Take first line if multi-line
    if "\n" in text.strip():
        text = text.strip().split("\n")[0]

    # Cap length — spoken aloud
    if len(text) > 150:
        text = text[:147] + "..."

    return text.strip()


# ---------------------------------------------------------------------------
# Generate summary: fast-path -> OpenAI -> Gemini -> Claude CLI -> fallback
# ---------------------------------------------------------------------------

if not summary:
    # Try fast path first — no Haiku needed for simple changes
    summary = try_fast_summary(transcript_path)
    if summary:
        log(f"  Fast-path summary: {summary}")

if not summary:
    context = extract_context(transcript_path)
    log(f"  Extracted context ({len(context)} chars)")

    if context:
        safe_context = _redact_secrets(context)
        log(f"  Context for summary:\n{safe_context}")

        # Try API-based summary — uses the user's own keys, not ours.
        # OpenAI API covers Claude + Codex users (OPENAI_API_KEY).
        # Gemini API covers Gemini users (GEMINI_API_KEY / GOOGLE_API_KEY).
        raw = generate_openai_summary(safe_context, project_name)
        if raw:
            summary = _clean_summary(raw)
            if summary:
                log(f"  OpenAI summary: {summary}")
            else:
                log(f"  OpenAI rejected by cleanup: {raw}")

        if not summary:
            raw = generate_gemini_summary(safe_context, project_name)
            if raw:
                summary = _clean_summary(raw)
                if summary:
                    log(f"  Gemini summary: {summary}")
                else:
                    log(f"  Gemini rejected by cleanup: {raw}")

    # CLI fallback — only Claude has a reliable non-interactive prompt mode.
    # Codex users are covered by OpenAI API; Gemini users by Gemini API.
    if not summary and context and assistant == "claude" and shutil.which("claude"):
        prompt = (
            "You create ultra-concise audio summaries of coding agent work.\n"
            "Format: 1 sentence what was done + 'Next:' + 1 next step.\n"
            "Be ruthlessly concise, natural tone, no filler.\n\n"
            "RULES:\n"
            "1. Start with a past-tense verb: Fixed, Updated, Added, Implemented, etc.\n"
            "2. Focus on the FINAL OUTCOME, not intermediate steps or exploration.\n"
            "3. The 'Agent's final statement' is the most important — it describes what was actually done.\n"
            "   Distill that into your summary. Ignore earlier exploration or verification steps.\n"
            "4. Never use first person (I, I've). Never ask questions.\n"
            "5. Never mention approvals, permissions, or screenshots.\n"
            "6. No markdown, code blocks, or file paths. Plain speech.\n\n"
            "GOOD: Fixed the auth bug in login flow. Next: run the tests.\n"
            "GOOD: Added status bar filtering for terminal UI. Next: verify on device.\n"
            "GOOD: Deployed latest build to iPhone. Next: check TestFlight.\n"
            "BAD: I need to see the actual work (first person)\n"
            "BAD: The screenshot requires approval (roleplaying)\n"
            "BAD: Verified the iOS structure exists (describing a check, not an outcome)\n\n"
            f"Project: {project_name}\n"
            f"Log:\n{safe_context}"
        )

        try:
            log("  Calling Haiku for summary...")
            proc = subprocess.run(
                ["claude", "-p", prompt, "--model", "claude-haiku-4-5-20251001", "--max-tokens", "100"],
                capture_output=True,
                text=True,
                timeout=HAIKU_TIMEOUT,
                env={**os.environ, "VOXHERD_HOOK_RUNNING": "1"},
            )
            if proc.returncode == 0 and proc.stdout.strip():
                raw = proc.stdout.strip()
                log(f"  Haiku raw output: {raw[:300]}")
                summary = _clean_summary(raw)
                if summary:
                    log(f"  Haiku summary: {summary}")
                else:
                    log(f"  Haiku rejected by cleanup: {raw}")
            else:
                log(f"  Haiku failed: exit={proc.returncode} stderr={proc.stderr[:200]}")
        except subprocess.TimeoutExpired:
            log("  Haiku timed out")
        except Exception as e:
            log(f"  Haiku error: {e}")
    elif not summary and context and assistant == "claude":
        log("  Claude CLI not found, skipping Haiku summarization")
    elif not summary and context:
        log(f"  No CLI fallback for {assistant} (API-based summary failed)")
    elif not summary:
        log("  No context extracted from transcript")

if not summary:
    # Build a fallback from extracted context
    if context:
        # Find files changed line
        for line in context.split("\n"):
            if line.startswith("Files changed:"):
                summary = f"Updated {line[len('Files changed: '):]}"
                break
    if not summary:
        summary = "Task completed."
    log(f"  Using fallback summary: {summary}")


# ---------------------------------------------------------------------------
# POST to bridge server
# ---------------------------------------------------------------------------

timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Detect tmux target so the bridge can dispatch via send-keys.
# Use $TMUX_PANE (pane ID like %5) with -t for correct resolution,
# even when Claude Code was dispatched from a different tmux session (e.g. bridge).
tmux_target = None
tmux_pane = os.environ.get("TMUX_PANE")
if tmux_pane:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", tmux_pane, "-p", "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            tmux_target = r.stdout.strip()
    except Exception:
        pass
elif os.environ.get("TMUX"):
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            tmux_target = r.stdout.strip()
    except Exception:
        pass

payload = json.dumps({
    "event": "stop",
    "session_id": session_id,
    "project": project_name,
    "project_dir": project_dir,
    "assistant": assistant,
    "summary": summary,
    "stop_reason": stop_reason,
    "transcript_path": transcript_path,
    "timestamp": timestamp,
    "tmux_target": tmux_target,
})

# Read auth token from file -- use temp config to keep it out of process list
auth_token = ""
token_file = _CONFIG_DIR / "auth_token"
try:
    auth_token = token_file.read_text().strip()
except Exception:
    pass

curl_cmd = [
    "curl", "-s", "--max-time", "5",
    "-X", "POST", BRIDGE_URL,
    "-H", "Content-Type: application/json",
    "-H", "X-VoxHerd: 1",
]

curl_auth_file = None
if auth_token:
    fd_tmp, curl_auth_file = tempfile.mkstemp(prefix="vh-curl-")
    os.write(fd_tmp, f'header = "Authorization: Bearer {auth_token}"\n'.encode())
    os.close(fd_tmp)
    os.chmod(curl_auth_file, 0o600)
    curl_cmd += ["--config", curl_auth_file]

curl_cmd += ["-d", payload]

try:
    result = subprocess.run(
        curl_cmd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    log(f"  Bridge POST: {result.stdout.strip()}")
except Exception as e:
    log(f"  Bridge POST failed: {e}")
finally:
    if curl_auth_file:
        try:
            os.unlink(curl_auth_file)
        except OSError:
            pass
