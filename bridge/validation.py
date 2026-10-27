"""Input validation and sanitization helpers for the bridge server."""
from __future__ import annotations

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

# Directories directly under $HOME that hold credentials or agent state, and so
# must never themselves be a project root (see _validate_project_dir). Compared
# case-folded: macOS and Windows resolve ".SSH" to ".ssh".
_FORBIDDEN_HOME_DIRS = frozenset({
    ".voxherd", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
    ".claude", ".codex", ".gemini", ".grok", ".config", "library",
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
# Model id, as the assistant reports it. Wide enough for every provider's
# spelling -- `claude-opus-5-5`, `claude-sonnet-4-5[1m]`, Bedrock's
# `us.anthropic.claude-...-v1:0`, Vertex's `claude-opus-4@20250514`,
# OpenRouter's `anthropic/claude-...`, a local `qwen3.8:27b-mlx` -- and narrow
# enough that nothing but an id gets through. Display-only: never passed to a
# shell. Claude Code's `<synthetic>` (a turn it made up) fails it by design.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]{0,99}$")

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
# The same shape without the pane: `resize-window` takes a window target.
_SAFE_TMUX_WINDOW_TARGET_RE = re.compile(
    r"^(?P<session>[a-zA-Z0-9_-]+):(?P<window>[0-9]{1,4})$"
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


def _sanitize_model(value: object) -> str:
    """Return ``value`` if it looks like a model id, else ``""``.

    Dropped rather than refused: the model is a label, and a session must never
    fail to register over its label.
    """
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if _MODEL_RE.match(value) else ""


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


def _sanitize_dispatch_text(text: object) -> str:
    """``text`` as something safe to type into an assistant's terminal.

    ``_sanitize_message`` drops most control characters but keeps CR and LF,
    which are harmless in a summary and are *submissions* in a TUI: the
    dispatch path sends the text with ``send-keys -l`` and then Enter as a
    separate key, so an embedded CR would submit early and the remainder would
    land in whatever the assistant showed next. A bare ESC is worse -- with a
    select widget on screen, ``ESC [ B`` is Down and the following Enter
    confirms whatever the cursor moved to, which is how a reply once approved
    the command it meant to deny.
    """
    if not isinstance(text, str):
        return ""
    cleaned = _sanitize_message(text)
    # CR/LF/TAB survive _sanitize_message; collapse them to spaces so the
    # message stays one submission.
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return cleaned.strip()


def _validate_project_dir(
    raw_dir: str, *, require_exists: bool = True
) -> tuple[str, str | None]:
    """Canonicalize and validate a project directory path.

    Returns (canonical_path, error_message). error_message is None on success.

    ``require_exists`` separates the two things this function checks. Containment
    -- under the user's home, not a system directory, not the home directory
    itself, not a credential directory -- is the security property: it decides
    what the Reader can be rooted at and what a dispatch runs in. Existence is a
    liveness property, and the places that care already handle it better:
    ``reader.project_root`` answers 404 "the project's directory is gone", and a
    spawn fails outright. Registration therefore checks containment only, which
    also avoids a check that a directory removed a second later would pass
    anyway.
    """
    if not raw_dir:
        return "", "directory is required"
    canonical = os.path.realpath(os.path.expanduser(raw_dir))
    if canonical in _FORBIDDEN_DIRS:
        return "", f"forbidden directory: {canonical}"
    if require_exists and not os.path.isdir(canonical):
        return "", f"directory does not exist: {canonical}"
    # Must be under the current user's home directory.
    home = Path(os.path.expanduser("~")).resolve()
    canonical_path = Path(canonical)
    try:
        relative = canonical_path.relative_to(home)
    except ValueError:
        return "", f"directory must be under current user home: {canonical}"
    # ...and strictly *under* it. The home directory itself is not a project:
    # as a Reader root it would serve ~/.voxherd/auth_token, ~/.claude.json and
    # ~/.zsh_history, because reader.refusal() judges names relative to the
    # root, so every rule keyed on a directory name stops applying once that
    # directory *is* the root.
    if relative == Path("."):
        return "", "the home directory itself is not a project"
    # Same reasoning one level down, but only for the directory ITSELF: a
    # credential or agent-state directory cannot be laundered into a root to
    # expose the files it holds. Paths *below* one are fine -- reader's
    # containment check keeps a read inside the root, and its deny list is
    # re-applied from $HOME down whatever the root is (reader._home_refusal).
    if relative.parts and len(relative.parts) == 1 \
            and relative.parts[0].lower() in _FORBIDDEN_HOME_DIRS:
        return "", f"forbidden directory: {canonical}"
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


def _validate_tmux_window_target(raw_target: str | None) -> tuple[str | None, str | None]:
    """Validate a tmux *window* target (e.g. ``session:0``).

    Separate from :func:`_validate_tmux_pane_target` because that one requires
    a ``.pane`` suffix and a window target has none -- ``resize-window`` acts
    on a window, so the persisted pane-geometry record (``pane_geometry.py``)
    keys on one, and the startup restore feeds it straight to tmux.

    Returns ``(clean_target, error_message)``, ``(None, None)`` for no target.
    """
    if raw_target is None:
        return None, None
    target = raw_target.strip()
    if not target:
        return None, None
    if len(target) > _MAX_TMUX_TARGET_LEN:
        return None, "invalid tmux window target: too long"
    match = _SAFE_TMUX_WINDOW_TARGET_RE.match(target)
    if not match:
        return None, "invalid tmux window target format (expected 'session:window')"
    if match.group("session") in _PROTECTED_TMUX_SESSIONS:
        return None, "invalid tmux window target: protected session"
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
