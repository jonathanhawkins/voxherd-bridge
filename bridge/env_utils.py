"""Environment utilities for subprocess calls.

When the bridge runs inside a macOS .app bundle or a Windows frozen exe,
the user's shell PATH is not inherited. This module provides a consistent
subprocess environment with common tool locations included.
"""

import os
import sys


# PATH separator: ";" on Windows, ":" on macOS/Linux.
_PATH_SEP = ";" if sys.platform == "win32" else ":"

# Standard directories where CLI tools live.
# Non-existent dirs are silently skipped via the os.path.isdir() guard below.
if sys.platform == "win32":
    _EXTRA_PATH_DIRS: list[str] = [
        os.path.expanduser(r"~\.claude\local"),
        os.path.expanduser(r"~\.gemini\bin"),
        os.path.expanduser(r"~\.codex\bin"),
        os.path.expanduser(r"~\.cargo\bin"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python311"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python312"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python313"),
        r"C:\Program Files\Tailscale",
    ]
else:
    _EXTRA_PATH_DIRS: list[str] = [  # type: ignore[no-redef]
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.cargo/bin"),
        os.path.expanduser("~/.claude/local"),
        os.path.expanduser("~/.gemini/bin"),
        os.path.expanduser("~/.codex/bin"),
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/snap/bin",
        "/usr/local/sbin",
    ]


def get_subprocess_env() -> dict[str, str]:
    """Return an environment dict suitable for subprocess calls.

    Merges the current process environment with additional PATH entries
    that may not be present when running inside a .app bundle or frozen exe.
    """
    env = os.environ.copy()
    current_path = env.get("PATH", "")
    current_dirs = set(current_path.split(_PATH_SEP)) if current_path else set()

    # Prepend missing dirs to PATH
    additions = [d for d in _EXTRA_PATH_DIRS if d not in current_dirs and os.path.isdir(d)]
    if additions:
        env["PATH"] = _PATH_SEP.join(additions) + _PATH_SEP + current_path

    return env
