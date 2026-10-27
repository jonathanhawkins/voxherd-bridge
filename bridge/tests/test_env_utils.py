"""The subprocess environment carries a UTF-8 locale when launchd gives none.

Bug (tmux 3.6a, found 2026-09-26): the macOS .app bundle is started by
launchd with no LANG or LC_* at all. tmux then treats its client as
non-UTF-8 and rewrites control characters in ``-F`` output to ``_`` -- so
``tmux list-sessions -F "#{session_name}\\t..."`` came back as
``name_1_1790446223_0_...``, ``tmux_manager.list_sessions`` split on a tab
that was no longer there, and returned nothing. Periodic discovery never
found a session and ``/api/tmux/sessions`` was always empty, silently.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from bridge import env_utils
from bridge.env_utils import get_subprocess_env

_LOCALE_KEYS = ("LANG", "LC_ALL", "LC_CTYPE")


@pytest.fixture
def no_locale(monkeypatch):
    """The environment a launchd-started app actually has."""
    for key in _LOCALE_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.skipif(sys.platform == "win32", reason="no tmux on Windows")
class TestLocale:
    def test_no_locale_gets_a_utf8_one(self, no_locale):
        env = get_subprocess_env()
        assert "UTF-8" in env["LC_CTYPE"]

    def test_an_existing_utf8_locale_is_left_alone(self, no_locale, monkeypatch):
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        env = get_subprocess_env()
        assert env["LANG"] == "de_DE.UTF-8"
        assert "LC_CTYPE" not in env

    def test_an_explicit_lc_all_is_never_overridden(self, no_locale, monkeypatch):
        # Whoever set LC_ALL meant it, and it would win over LC_CTYPE anyway.
        monkeypatch.setenv("LC_ALL", "C")
        env = get_subprocess_env()
        assert env["LC_ALL"] == "C"
        assert "LC_CTYPE" not in env

    def test_a_non_utf8_lang_still_gets_utf8_ctype(self, no_locale, monkeypatch):
        monkeypatch.setenv("LANG", "C")
        assert "UTF-8" in get_subprocess_env()["LC_CTYPE"]

    @pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
    def test_tmux_keeps_the_tab_list_sessions_splits_on(self, no_locale):
        # The real failure, against a real tmux server of our own (its own
        # socket, so no developer session is touched). A short directory,
        # because pytest's tmp_path overflows the 104-byte socket path limit
        # on macOS and tmux refuses to start.
        sock = os.path.join(tempfile.mkdtemp(prefix="vh", dir="/tmp"), "s")
        subprocess.run(
            ["tmux", "-S", sock, "new-session", "-d", "-s", "envprobe", "sleep 30"],
            check=True, env=get_subprocess_env(),
        )
        try:
            out = subprocess.run(
                ["tmux", "-S", sock, "list-sessions", "-F", "#{session_name}\t#{session_windows}"],
                capture_output=True, text=True, env=get_subprocess_env(), check=False,
            ).stdout
            assert out.splitlines() == ["envprobe\t1"], repr(out)
        finally:
            subprocess.run(["tmux", "-S", sock, "kill-server"], env=get_subprocess_env(), check=False)


def test_utf8_detection_follows_tmux_precedence():
    # LC_ALL wins, then LC_CTYPE, then LANG -- the first one that is set.
    assert env_utils._has_utf8_locale({"LC_ALL": "C", "LANG": "en_US.UTF-8"}) is False
    assert env_utils._has_utf8_locale({"LC_CTYPE": "C.UTF-8", "LANG": "C"}) is True
    assert env_utils._has_utf8_locale({"LANG": "en_US.utf8"}) is True
    assert env_utils._has_utf8_locale({}) is False


# ---------------------------------------------------------------------------
# PATH ordering
#
# The other half of this module, and the one with teeth: the bridge resolves
# `claude`, `tmux`, `git`, `jq`, `sh` and `env` by NAME, so whichever directory
# comes first on the PATH decides which binary runs. The ordering was rewritten
# three times and never tested, and each shape was wrong in a way the next
# comment described rather than a test caught:
#
#   * everything prepended as one block -- ~/.local/bin ahead of /usr/bin.
#   * "not under $HOME in front, under $HOME at the back" -- no effect, because
#     only MISSING dirs are added at all.
#   * added system dirs ahead of the inherited PATH -- under launchd that
#     promoted /opt/homebrew/bin (group-writable by `admin`) above /usr/bin.
# ---------------------------------------------------------------------------

#: The PATH a launchd-started .app actually inherits -- no Homebrew, no $HOME.
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _path_of(monkeypatch, inherited: str, extra_dirs: list[str],
             *, existing: set[str] | None = None) -> list[str]:
    """get_subprocess_env()'s PATH, with the dir set under test."""
    monkeypatch.setenv("PATH", inherited)
    monkeypatch.setattr(env_utils, "_EXTRA_PATH_DIRS", extra_dirs)
    # `added` is filtered by os.path.isdir; pretend the listed dirs exist.
    present = extra_dirs if existing is None else existing
    monkeypatch.setattr(env_utils.os.path, "isdir", lambda d: d in present)
    return get_subprocess_env()["PATH"].split(env_utils._PATH_SEP)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
class TestPathOrdering:
    def test_nothing_this_module_adds_can_shadow_a_system_tool(self, monkeypatch):
        """The launchd case, where the inherited PATH is bare.

        /opt/homebrew/bin is drwxrwxr-x <user> admin -- writable by the user and
        by any admin -- so above /usr/bin it is a hijack of `git`, `jq` and
        `sh`. One earlier shape put it there.
        """
        home = os.path.expanduser("~")
        path = _path_of(monkeypatch, LAUNCHD_PATH,
                        ["/usr/bin", "/opt/homebrew/bin", f"{home}/.local/bin"])
        assert path.index("/usr/bin") < path.index("/opt/homebrew/bin")
        assert path.index("/usr/bin") < path.index(f"{home}/.local/bin")

    def test_a_planted_home_binary_cannot_outrank_a_real_one(self, monkeypatch):
        """`claude` is resolved by name; ~/.claude/local must lose to /usr/bin."""
        home = os.path.expanduser("~")
        added = [f"{home}/.claude/local", f"{home}/.local/bin", "/usr/local/bin"]
        path = _path_of(monkeypatch, LAUNCHD_PATH, added)
        for home_dir in (f"{home}/.claude/local", f"{home}/.local/bin"):
            assert path.index("/usr/bin") < path.index(home_dir)
            assert path.index("/usr/local/bin") < path.index(home_dir), \
                "added $HOME dirs must come after added system dirs"

    def test_the_developers_own_path_keeps_its_order_and_precedence(self, monkeypatch):
        """Reordering it changed which tools ran.

        Measured: added-system-first moved `git` from Homebrew's 2.53.0 to
        Apple's 2.50.1 and `python3` from the venv's 3.11 to the system 3.9 --
        the interpreter drift CLAUDE.md warns about.
        """
        home = os.path.expanduser("~")
        inherited = f"{home}/dev/voxherd/bridge/.venv/bin:/opt/homebrew/bin:/usr/bin:/bin"
        path = _path_of(monkeypatch, inherited, ["/usr/bin", "/usr/local/bin", "/snap/bin"])
        assert path[:4] == inherited.split(":"), "the inherited PATH was reordered"

    def test_a_home_dir_the_developer_chose_is_left_where_they_put_it(self, monkeypatch):
        """Only dirs THIS MODULE adds are demoted, not the user's own choice."""
        home = os.path.expanduser("~")
        inherited = f"{home}/.local/bin:/usr/bin:/bin"
        path = _path_of(monkeypatch, inherited, [f"{home}/.local/bin", "/usr/local/bin"])
        assert path[0] == f"{home}/.local/bin"
        assert path.count(f"{home}/.local/bin") == 1, "duplicated instead of deduped"

    def test_an_empty_component_is_dropped(self, monkeypatch):
        """An empty entry means the CWD to execvp, and the bridge spawns with
        cwd=project_dir -- so a trailing or doubled separator is a live hijack:
        a file named `jq` in the repo being supervised would be executed."""
        path = _path_of(monkeypatch, "/usr/bin::/bin:", ["/usr/local/bin"])
        assert "" not in path
        assert path[:2] == ["/usr/bin", "/bin"]

    def test_a_dir_that_does_not_exist_is_not_added(self, monkeypatch):
        path = _path_of(monkeypatch, LAUNCHD_PATH,
                        ["/opt/homebrew/bin", "/snap/bin"], existing={"/snap/bin"})
        assert "/opt/homebrew/bin" not in path
        assert "/snap/bin" in path

    def test_every_entry_appears_once(self, monkeypatch):
        home = os.path.expanduser("~")
        path = _path_of(monkeypatch, f"/usr/bin:/bin:/usr/bin:{home}/.local/bin",
                        ["/usr/bin", f"{home}/.local/bin", "/usr/local/bin"])
        assert len(path) == len(set(path))

    def test_an_absent_path_still_yields_the_added_dirs(self, monkeypatch):
        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.setattr(env_utils, "_EXTRA_PATH_DIRS", ["/usr/bin", "/usr/local/bin"])
        monkeypatch.setattr(env_utils.os.path, "isdir", lambda d: True)
        assert get_subprocess_env()["PATH"].split(":") == ["/usr/bin", "/usr/local/bin"]

    def test_the_assistant_install_dirs_are_still_reachable(self, monkeypatch):
        """Demoted, not dropped: a native `claude` lives in ~/.claude/local and
        nothing else on the PATH would find it."""
        home = os.path.expanduser("~")
        path = _path_of(monkeypatch, LAUNCHD_PATH, [f"{home}/.claude/local"])
        assert f"{home}/.claude/local" in path
