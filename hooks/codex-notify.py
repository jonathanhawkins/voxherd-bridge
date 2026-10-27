#!/usr/bin/env python3
"""VoxHerd Codex notify handler — bridges Codex agent-turn-complete events to the VoxHerd bridge server.

Codex CLI invokes the ``notify`` script with a single JSON argument (sys.argv[1])
whenever an agent turn completes.  This script translates that into a VoxHerd
stop event POST to ``http://localhost:7777/api/events``.

JSON payload from Codex (sys.argv[1]):
    {
        "type": "agent-turn-complete",
        "thread-id": "...",
        "turn-id": "...",
        "cwd": "...",
        "input-messages": [...],
        "last-assistant-message": "..."
    }

Must NEVER block Codex — all external calls have timeouts and errors are
swallowed silently.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------

if os.environ.get("VOXHERD_HOOK_RUNNING"):
    sys.exit(0)
os.environ["VOXHERD_HOOK_RUNNING"] = "1"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRIDGE_URL = "http://localhost:7777/api/events"
_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / "VoxHerd"
else:
    _CONFIG_DIR = Path.home() / ".voxherd"

LOG_DIR = _CONFIG_DIR / "logs"
DEBUG_LOG = LOG_DIR / "codex-notify-debug.log"
DEBOUNCE_FILE = LOG_DIR / ".codex-notify-debounce"
DEBOUNCE_SECONDS = 3

LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

#: Codex runs ``notify`` without waiting for it, so a model call here holds
#: nothing up; this only bounds a CLI that never answers.
SUMMARY_TIMEOUT = 15
#: Enough of the turn's last message to summarise; a long one is mostly detail.
SUMMARY_CONTEXT_CHARS = 4000


def log(msg: str) -> None:
    """Append a timestamped debug line."""
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if not _IS_WINDOWS:
            flags |= os.O_NOFOLLOW
        fd = os.open(str(DEBUG_LOG), flags, 0o600)
        try:
            os.write(fd, f"[{datetime.now()}] {msg}\n".encode())
        finally:
            os.close(fd)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Model profile -- which small model, if any, summarises a Codex turn
# ---------------------------------------------------------------------------
#
# The same resolver on-stop.py uses (bridge/model_profile.py, which install.sh
# copies next to this file), and the same rule: shell out to the profile's CLI
# when it is this session's own assistant or the user chose a profile. The
# stock install chose nothing and summarises with claude, which Codex sessions
# never used -- so they keep the free first-line summary below.

def _load_profile():
    try:
        here = Path(__file__).resolve().parent
        for candidate in (here, here.parent / "bridge", here.parent):
            if (candidate / "model_profile.py").is_file():
                if str(candidate) not in sys.path:
                    sys.path.insert(0, str(candidate))
                break
        import model_profile  # type: ignore[import-not-found]
        return model_profile.resolve()
    except Exception as e:
        log(f"  Model profile unavailable: {e}")
        return None


def model_summary(message: str, project: str) -> str:
    """A 1-2 sentence summary from the profile's model, or "" to fall back."""
    profile = _load_profile()
    if profile is None:
        return ""
    cli = profile.summary_cli
    if cli != "codex" and not profile.summary_cli_configured:
        return ""
    if not shutil.which(cli):
        log(f"  {cli} not found, keeping the first-line summary")
        return ""
    prompt = (
        "Summarize what this coding agent just did in 1-2 short sentences for a "
        "voice announcement. Natural tone, no filler, no markdown.\n\n"
        f"Project: {project}\n"
        f"Agent's final message:\n{message.strip()[-SUMMARY_CONTEXT_CHARS:]}"
    )
    try:
        log(f"  Calling {cli} ({profile.summary_model}) for summary...")
        proc = subprocess.run(
            profile.summary_argv(prompt),
            # codex exec appends a piped stdin to the prompt; Codex's own
            # stdin is nothing the model should read.
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=SUMMARY_TIMEOUT, check=False,
            env={**os.environ, "VOXHERD_HOOK_RUNNING": "1"},
        )
    except Exception as e:
        log(f"  {cli} summary failed: {e}")
        return ""
    if proc.returncode != 0:
        log(f"  {cli} exited {proc.returncode}: {proc.stderr[:200]}")
        return ""
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    summary = " ".join(lines).replace("*", "").replace("`", "").strip()[:300]
    log(f"  {cli} summary: {summary}")
    return summary


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

def should_debounce() -> bool:
    """Skip if another notify fired within DEBOUNCE_SECONDS."""
    if _IS_WINDOWS:
        return False  # simplified for Windows
    try:
        import fcntl
        fd = os.open(str(DEBOUNCE_FILE), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return True
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


# ---------------------------------------------------------------------------
# Parse Codex notification
# ---------------------------------------------------------------------------

if len(sys.argv) < 2:
    log("No argument provided — exiting")
    sys.exit(0)

try:
    notification = json.loads(sys.argv[1])
except Exception as e:
    log(f"Failed to parse JSON arg: {e}")
    sys.exit(0)

event_type = notification.get("type", "")
if event_type != "agent-turn-complete":
    log(f"Ignoring event type: {event_type}")
    sys.exit(0)

thread_id = notification.get("thread-id", "")
cwd = notification.get("cwd", "")
last_message = notification.get("last-assistant-message", "")

project_dir = cwd
project_name = os.path.basename(project_dir) if project_dir else "unknown"

log(f"=== codex-notify.py invoked ===")
log(f"  thread_id={thread_id}")
log(f"  project={project_name}")
log(f"  cwd={cwd}")

# VOXHERD_QUIET: mark this worker silent so the bridge skips narration
# (matches on-stop.py / on-stop.sh). Accepts 1/true/yes/on (case-insensitive).
quiet = os.environ.get("VOXHERD_QUIET", "").strip().lower() in ("1", "true", "yes", "on")

if should_debounce():
    log("  Debounced — using fallback summary")
    summary = "Task completed."
else:
    summary = ""

# A quiet worker is never spoken, so it never pays for a summary either.
if not summary and last_message and not quiet:
    summary = model_summary(last_message, project_name)

# ---------------------------------------------------------------------------
# Extract summary from last assistant message
# ---------------------------------------------------------------------------

if not summary and last_message:
    # Try to extract a concise summary from the assistant's last message.
    # Look for first sentence starting with a past-tense verb.
    first_line = last_message.strip().split("\n")[0].split(". ")[0].strip()
    first_line = first_line.replace("*", "").replace("`", "").replace("#", "").strip()
    past_verbs = [
        "Fixed", "Updated", "Added", "Removed", "Refactored", "Implemented",
        "Configured", "Deployed", "Created", "Resolved", "Applied", "Installed",
        "Built", "Changed", "Moved", "Renamed", "Merged", "Set", "Enabled",
        "Disabled", "Cleaned", "Optimized", "Simplified", "Replaced",
        "Wrote", "Deleted", "Restored", "Upgraded", "Converted", "Migrated",
        "Tested", "Verified", "Committed", "Pushed", "Patched", "Restructured",
    ]
    if any(first_line.startswith(v) for v in past_verbs) and len(first_line) < 120:
        summary = first_line + ("." if not first_line.endswith(".") else "")
        log(f"  Fast-path summary: {summary}")

if not summary and last_message:
    # Fallback: use first 120 chars of last message
    truncated = last_message.strip().split("\n")[0][:120].strip()
    if truncated and len(truncated) > 10:
        summary = truncated + ("..." if len(last_message.strip().split("\n")[0]) > 120 else "")
        log(f"  Truncated summary: {summary}")

if not summary:
    summary = "Task completed."
    log(f"  Using fallback summary: {summary}")

# ---------------------------------------------------------------------------
# Detect tmux target
# ---------------------------------------------------------------------------

tmux_target = None
tmux_pane = os.environ.get("TMUX_PANE")
if tmux_pane:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", tmux_pane, "-p",
             "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            tmux_target = r.stdout.strip()
    except Exception:
        pass
elif os.environ.get("TMUX"):
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p",
             "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            tmux_target = r.stdout.strip()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# POST to bridge server
# ---------------------------------------------------------------------------

timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Use thread_id as session_id for Codex sessions.
# Prefix with "codex-" to avoid collision with Claude session IDs.
session_id = f"codex-{thread_id}" if thread_id else f"codex-{project_name}-{int(time.time())}"

payload = json.dumps({
    "event": "stop",
    "session_id": session_id,
    "project": project_name,
    "project_dir": project_dir,
    "assistant": "codex",
    "summary": summary,
    "stop_reason": "end_turn",
    "timestamp": timestamp,
    "tmux_target": tmux_target,
    "skip_tts": quiet,
})

# Read auth token
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
    result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=10)
    log(f"  Bridge POST: {result.stdout.strip()}")
except Exception as e:
    log(f"  Bridge POST failed: {e}")
finally:
    if curl_auth_file:
        try:
            os.unlink(curl_auth_file)
        except OSError:
            pass
