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
        os.path.expanduser(r"~\.grok\bin"),
        os.path.expanduser(r"~\.cargo\bin"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python311"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python312"),
        os.path.expanduser(r"~\AppData\Local\Programs\Python\Python313"),
        r"C:\Program Files\Tailscale",
    ]
else:
    # Ordered system-first, and APPENDED to the inherited PATH (see
    # get_subprocess_env) -- so a dir here only ever decides what to run when
    # nothing already on the PATH provides it. Within this list the order still
    # matters, because these are appended as a block: the user-writable
    # assistant install dirs have to stay on it (a native `claude` install lives
    # in ~/.claude/local and nothing else would find it), but they sit after the
    # system directories so a file dropped in one cannot shadow a system tool.
    # Homebrew is in the same category on an Apple-silicon Mac: /opt/homebrew/bin
    # is `drwxrwxr-x <user> admin`, writable by the user and by any admin.
    _EXTRA_PATH_DIRS: list[str] = [  # type: ignore[no-redef]
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/usr/local/sbin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/snap/bin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.cargo/bin"),
        os.path.expanduser("~/.claude/local"),
        os.path.expanduser("~/.gemini/bin"),
        os.path.expanduser("~/.codex/bin"),
        os.path.expanduser("~/.grok/bin"),
    ]


def _has_utf8_locale(env: dict[str, str]) -> bool:
    """True when the locale tmux consults says UTF-8.

    tmux reads the first of LC_ALL, LC_CTYPE, LANG that is set -- the same
    order as here -- and only checks whether it contains "UTF-8"/"UTF8".
    """
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.get(key, "")
        if value:
            return "utf-8" in value.lower() or "utf8" in value.lower()
    return False


# A locale that exists on every host we run on: macOS always ships
# en_US.UTF-8; minimal Linux images may not, but C.UTF-8 is built in.
_UTF8_CTYPE = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"


def get_subprocess_env() -> dict[str, str]:
    """Return an environment dict suitable for subprocess calls.

    Merges the current process environment with additional PATH entries
    that may not be present when running inside a .app bundle or frozen exe,
    and a UTF-8 locale when there is none.

    The locale is not cosmetic. A process started by launchd -- the .app
    bundle -- has no LANG or LC_* at all, and tmux then treats its client as
    non-UTF-8 and rewrites every control character in ``-F`` / ``-p`` output
    to ``_``. That includes the tab ``tmux_manager.list_sessions`` splits on,
    so in the bundle it returned nothing: periodic discovery never found a
    session and ``/api/tmux/sessions`` was always empty, with no error
    anywhere. It would also mangle a non-ASCII ``#{pane_current_path}``.
    Measured on tmux 3.6a, 2026-09-26.
    """
    env = os.environ.copy()
    current_path = env.get("PATH", "")

    # The PATH decides which binary the bridge executes: it resolves `claude`,
    # `tmux`, `git`, `jq`, `sh` and `env` by name, so a file dropped in a
    # user-writable directory that outranks a system one gets run instead.
    #
    # Order:
    #
    #   1. the inherited PATH, in its own order
    #   2. the system and package-manager dirs THIS MODULE adds
    #   3. the $HOME assistant install dirs this module adds
    #
    # deduped, first occurrence winning.
    #
    # The inherited PATH keeps its order and its precedence because it is the
    # developer's own toolchain choice, and overriding it changed which tools
    # ran: measured, putting the added system dirs first moved `git` from
    # Homebrew's 2.53.0 to Apple's 2.50.1 (a stub that nags on a Mac without the
    # Command Line Tools, so /api/diff died where it worked) and `python3` from
    # the venv's 3.11 to the system 3.9 -- the interpreter drift CLAUDE.md warns
    # about. A pyenv or nvm shim moves the same way.
    #
    # What this module controls is where its OWN additions land, which is where
    # the hijack lived. Three earlier shapes were each wrong:
    #
    #   * everything prepended as one block -- put ~/.local/bin ahead of /usr/bin.
    #   * the same, split into "not under $HOME in front, under $HOME at the
    #     back" -- changed nothing, because `added` holds only dirs that were
    #     MISSING, so anything already inherited kept its inherited position.
    #   * added system dirs in front of the inherited PATH -- under launchd the
    #     inherited PATH is just /usr/bin:/bin:/usr/sbin:/sbin, so this promoted
    #     /opt/homebrew/bin (drwxrwxr-x, writable by the user and by any admin)
    #     ABOVE /usr/bin. Strictly worse than what it replaced.
    #
    # So additions go behind the inherited PATH, and the $HOME ones go last: a
    # planted ~/.local/bin/claude cannot outrank a real one, and nothing added
    # here can shadow a system tool. The dirs still have to be on the list --
    # a native `claude` install lives in ~/.claude/local and nothing else would
    # find it. A user-writable dir the developer put on their own PATH is
    # theirs; their shell already resolves it that way.
    home_prefix = os.path.expanduser("~") + os.sep
    inherited = [
        d for d in (current_path.split(_PATH_SEP) if current_path else [])
        # An empty component means the current directory to execvp, and the
        # bridge spawns with cwd=project_dir -- so a trailing or doubled
        # separator on the inherited PATH is a live hijack. Drop them.
        if d
    ]
    inherited_set = set(inherited)
    added = [
        d for d in _EXTRA_PATH_DIRS
        if d not in inherited_set and os.path.isdir(d)
    ]
    added_system = [d for d in added if not d.startswith(home_prefix)]
    added_home = [d for d in added if d.startswith(home_prefix)]

    ordered: list[str] = []
    seen: set[str] = set()
    for d in [*inherited, *added_system, *added_home]:
        if d in seen:
            continue
        seen.add(d)
        ordered.append(d)
    if ordered:
        env["PATH"] = _PATH_SEP.join(ordered)

    # Only when nothing says UTF-8, and never over an explicit LC_ALL: that is
    # a deliberate choice by whoever set it, and LC_ALL would win anyway.
    if sys.platform != "win32" and not env.get("LC_ALL") and not _has_utf8_locale(env):
        env["LC_CTYPE"] = _UTF8_CTYPE

    return env
