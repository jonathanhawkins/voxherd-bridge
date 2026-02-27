"""Input validation and sanitization helpers for the bridge server."""

import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories that should never be used as project roots.
# macOS + Linux system dirs that should never be project roots.
_FORBIDDEN_DIRS = frozenset({
    "/", "/etc", "/var", "/tmp", "/usr", "/bin", "/sbin",
    # macOS
    "/System", "/Library",
    # Linux
    "/proc", "/sys", "/dev", "/run", "/boot", "/snap",
})

# Limits for untrusted string fields.
_MAX_SESSION_ID_LEN = 128
_MAX_PROJECT_LEN = 100
_MAX_SUMMARY_LEN = 2000
_MAX_MESSAGE_LEN = 5000
_MAX_EVENT_PAYLOAD_LEN = 50_000  # bytes
_MAX_TMUX_TARGET_LEN = 128

# Session ID: UUID-style or alphanumeric with hyphens
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,128}$")
# Project name: letters, digits, hyphens, underscores, dots, spaces
_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9\-_\. ]{1,100}$")

# Max simultaneous WebSocket connections
_MAX_WS_CONNECTIONS = 10

# Regex to strip ANSI escape sequences for plain-text output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Names that must never be killed via the iOS UI.
from bridge import tmux_manager
_PROTECTED_TMUX_SESSIONS = frozenset({tmux_manager.BRIDGE_SESSION, "bridge", "vh-bridge"})
_SAFE_TMUX_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SAFE_TMUX_PANE_TARGET_RE = re.compile(
    r"^(?P<session>[a-zA-Z0-9_-]+):(?P<window>[0-9]{1,4})\.(?P<pane>[0-9]{1,4})$"
)

# Rate limiting
_DISPATCH_RATE_LIMIT = 10  # max dispatches per session
_DISPATCH_RATE_WINDOW = 60.0  # per this many seconds

# Projects config path
_PROJECTS_PATH = os.path.expanduser("~/.voxherd/projects.json")


# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------


def _validate_session_id(sid: str) -> str | None:
    """Return error message if session_id is invalid, else None."""
    if not sid:
        return "session_id is required"
    if not _SESSION_ID_RE.match(sid):
        return f"invalid session_id format (must be alphanumeric/hyphens, max {_MAX_SESSION_ID_LEN} chars)"
    return None


def _validate_project_name(name: str) -> str | None:
    """Return error message if project name is invalid, else None."""
    if not name or name == "unknown":
        return None  # allow empty/unknown — they're harmless defaults
    if not _PROJECT_NAME_RE.match(name):
        return f"invalid project name (alphanumeric/hyphens/underscores/dots/spaces, max {_MAX_PROJECT_LEN} chars)"
    return None


def _sanitize_summary(text: str) -> str:
    """Truncate and clean summary text for safe display/TTS."""
    if not text:
        return ""
    # Strip control characters except newline/tab
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return cleaned[:_MAX_SUMMARY_LEN]


def _sanitize_message(text: str) -> str:
    """Truncate message text for dispatch to Claude Code."""
    if not text:
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return cleaned[:_MAX_MESSAGE_LEN]


def _validate_project_dir(raw_dir: str) -> tuple[str, str | None]:
    """Canonicalize and validate a project directory path.

    Returns (canonical_path, error_message). error_message is None on success.
    """
    if not raw_dir:
        return "", "directory is required"
    canonical = os.path.realpath(os.path.expanduser(raw_dir))
    if canonical in _FORBIDDEN_DIRS:
        return "", f"forbidden directory: {canonical}"
    if not os.path.isdir(canonical):
        return "", f"directory does not exist: {canonical}"
    # Must be under the current user's home directory.
    home = Path(os.path.expanduser("~")).resolve()
    canonical_path = Path(canonical)
    try:
        canonical_path.relative_to(home)
    except ValueError:
        return "", f"directory must be under current user home: {canonical}"
    return canonical, None


def _validate_tmux_pane_target(raw_target: str | None) -> tuple[str | None, str | None]:
    """Validate a tmux pane target string (e.g. ``session:0.0``).

    Returns ``(clean_target, error_message)`` where ``clean_target`` is None
    when no target was provided.
    """
    if raw_target is None:
        return None, None
    target = raw_target.strip()
    if not target:
        return None, None
    if len(target) > _MAX_TMUX_TARGET_LEN:
        return None, "invalid tmux_target: too long"
    match = _SAFE_TMUX_PANE_TARGET_RE.match(target)
    if not match:
        return None, "invalid tmux_target format (expected 'session:window.pane')"
    session_name = match.group("session")
    if session_name in _PROTECTED_TMUX_SESSIONS:
        return None, "invalid tmux_target: protected session"
    return target, None


def _is_safe_tmux_target(name: str) -> bool:
    """Return True if the tmux session name is a VoxHerd-managed session.

    Only sessions starting with 'vh-' (spawned by bridge) or matching a
    registered session's tmux target are allowed.
    """
    from bridge.server_state import sessions

    if not name or name in _PROTECTED_TMUX_SESSIONS:
        return False
    if not _SAFE_TMUX_NAME_RE.match(name):
        return False
    if name.startswith("vh-"):
        return True
    for s in sessions.get_all_sessions().values():
        if s.tmux_target and s.tmux_target.split(":")[0] == name:
            return True
    return False


# ---------------------------------------------------------------------------
# Projects config
# ---------------------------------------------------------------------------


def _load_projects() -> list[dict]:
    """Read launchable projects from ~/.voxherd/projects.json."""
    if not os.path.isfile(_PROJECTS_PATH):
        return []
    try:
        with open(_PROJECTS_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict) and "name" in p and "dir" in p]
        return []
    except Exception:
        return []
