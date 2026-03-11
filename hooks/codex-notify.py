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

if should_debounce():
    log("  Debounced — using fallback summary")
    summary = "Task completed."
else:
    summary = ""

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
