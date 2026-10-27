"""The Reader's bridge half.

Its failures are quiet. A secret served to a headset, a symlink followed out
of the project, a region one line off -- the wearer points at the wrong code
and the agent answers about it. So the posture is pinned first, then shapes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from bridge import reader, ws_handler
from bridge.reader import REDACTED, ReaderError
from bridge.session_manager import Session
from bridge.tests.conftest import auth_headers, register_test_session


@pytest.fixture(autouse=True)
def _quiet_git(monkeypatch):
    """The developer's git config must not change what a diff looks like."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def write(root, rel: str, text: str | bytes) -> str:
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(text, bytes) else "w"
    with open(path, mode) as f:
        f.write(text)
    return path


def root_of(tmp_path) -> str:
    return reader.project_root(str(tmp_path))


def git(root, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def status_of(call) -> int:
    with pytest.raises(ReaderError) as caught:
        call()
    return caught.value.status


# ---------------------------------------------------------------------------
# What is never served
# ---------------------------------------------------------------------------


class TestPosture:

    def test_a_path_out_of_the_project_is_refused(self, tmp_path):
        write(tmp_path.parent, "outside.txt", "not yours\n")
        root = root_of(tmp_path)
        assert status_of(lambda: reader.file_payload(root, "../outside.txt")) == 403
        assert status_of(lambda: reader.file_payload(root, "a/../../outside.txt")) == 403

    def test_absolute_and_home_paths_are_refused(self, tmp_path):
        root = root_of(tmp_path)
        assert status_of(lambda: reader.file_payload(root, "/etc/hosts")) == 400
        assert status_of(lambda: reader.file_payload(root, "~/.zshrc")) == 400
        assert status_of(lambda: reader.file_payload(root, "")) == 400
        assert status_of(lambda: reader.file_payload(root, "a\x00b")) == 400

    def test_a_symlink_out_of_the_project_is_refused_not_followed(self, tmp_path):
        outside = write(tmp_path.parent, f"elsewhere-{uuid.uuid4().hex}.txt", "private\n")
        os.symlink(outside, tmp_path / "notes.txt")
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "notes.txt")) == 403

    def test_a_symlink_to_a_secret_is_refused_like_the_secret(self, tmp_path):
        write(tmp_path, ".env", "OPENAI=sk-live\n")
        os.symlink(tmp_path / ".env", tmp_path / "config.txt")
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "config.txt")) == 403

    @pytest.mark.parametrize("rel", [
        ".env", ".env.local", "deploy/prod.env", ".envrc",
        "certs/server.pem", "signing.p12", "release.keystore", "id_rsa", "keys/id_ed25519",
        ".netrc", ".npmrc", "credentials.json", "ios/App/Secrets.xcconfig", "config/secrets.yaml",
        "firebase-adminsdk-x1.json", ".git/config", "sub/.git/HEAD",
        ".mcp.json", "tools/mcp.json", ".codex/config.toml", ".gemini/settings.json",
        ".claude/settings.local.json",
    ])
    def test_files_that_are_secrets_by_nature_are_refused(self, tmp_path, rel):
        write(tmp_path, rel, "harmless\n")
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), rel)) == 403

    @pytest.mark.parametrize("rel", [
        ".env.example", "id_rsa.pub", "Secrets.xcconfig.template", ".gitignore",
        "bridge/secrets.py", ".claude/rules/android.md", ".codex/agents/helper.toml",
    ])
    def test_their_templates_and_look_alikes_are_served(self, tmp_path, rel):
        write(tmp_path, rel, "fine\n")
        assert reader.file_payload(root_of(tmp_path), rel)["line_count"] == 1

    def test_a_symlink_swapped_in_after_the_check_is_not_followed(self, tmp_path):
        # The final open is O_NOFOLLOW: a path that became a link between the
        # realpath check and the open fails rather than reading its target.
        target = write(tmp_path, "real.txt", "x\n")
        os.symlink(target, tmp_path / "late.txt")
        assert status_of(lambda: reader._read_regular(str(tmp_path / "late.txt"))) == 403

    def test_a_fifo_is_refused_without_hanging_the_request(self, tmp_path):
        os.mkfifo(tmp_path / "pipe.txt")
        done = threading.Event()
        outcome: list[int] = []

        def read_it():
            outcome.append(status_of(lambda: reader.file_payload(root_of(tmp_path), "pipe.txt")))
            done.set()

        threading.Thread(target=read_it, daemon=True).start()
        assert done.wait(5), "reading a FIFO blocked"
        assert outcome == [400]

    def test_a_directory_is_not_a_file(self, tmp_path):
        os.makedirs(tmp_path / "src")
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "src")) == 400

    def test_a_missing_file_is_not_found(self, tmp_path):
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "nope.py")) == 404

    def test_an_oversized_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reader, "MAX_FILE_BYTES", 100)
        write(tmp_path, "big.py", "x = 1\n" * 50)
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "big.py")) == 413

    def test_an_oversized_file_is_refused_before_it_is_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reader, "MAX_FILE_BYTES", 100)
        write(tmp_path, "big.py", "x = 1\n" * 50)

        def no_reading(*_args):
            raise AssertionError("read a file it already knew was too big")

        monkeypatch.setattr(reader.os, "read", no_reading)
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "big.py")) == 413

    def test_a_file_that_grows_while_it_is_read_is_still_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reader, "MAX_FILE_BYTES", 100)
        write(tmp_path, "grows.py", "x = 1\n")
        real_read = os.read
        monkeypatch.setattr(reader.os, "read", lambda fd, n: real_read(fd, n) or b"y" * n)
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "grows.py")) == 413

    def test_a_binary_file_is_refused(self, tmp_path):
        write(tmp_path, "image.py", b"\x89PNG\x00\x00data")
        assert status_of(lambda: reader.file_payload(root_of(tmp_path), "image.py")) == 415

    def test_a_secret_line_is_redacted_and_every_line_keeps_its_number(self, tmp_path):
        write(tmp_path, "config.py", 'import os\n\nAPI_KEY = "live-value-123"\nDEBUG = True\n')
        payload = reader.file_payload(root_of(tmp_path), "config.py")
        assert payload["line_count"] == 4
        assert payload["redacted"] == [3]
        assert "".join(run[0] for run in payload["lines"][2]) == REDACTED
        assert "".join(run[0] for run in payload["lines"][3]) == "DEBUG = True"
        assert "live-value-123" not in json.dumps(payload)

    def test_redaction_is_transcript_renders_own(self):
        # One detector, not a second weaker one: a line transcript_render
        # redacts is redacted here, whatever it looks like.
        from bridge.transcript_render import _contains_secret

        for line in ["token = TOKEN", "Bearer abc", "x = 'ghp_abc'", "url = eyJhbGciOi"]:
            assert _contains_secret(line)
            assert reader.redact_lines([line]) == ([REDACTED], [1])

    def test_a_pem_block_is_redacted_whole(self, tmp_path):
        key = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\nhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
        write(tmp_path, "fixture.py", f'KEY = """\n{key}\n"""\nafter = 1\n')
        payload = reader.file_payload(root_of(tmp_path), "fixture.py")
        assert payload["redacted"] == [2, 3, 4, 5]
        assert "MIIEvQIBADANBgkq" not in json.dumps(payload)
        assert "".join(r[0] for r in payload["lines"][6]) == "after = 1"

    def test_a_pem_block_that_never_ends_is_redacted_to_the_end(self):
        lines = ["x = 1", "-----BEGIN RSA PRIVATE KEY-----", "MIIEvQIB", "c2VjcmV0"]
        shown, redacted = reader.redact_lines(lines)
        assert redacted == [2, 3, 4]
        assert shown[0] == "x = 1"

    def test_one_line_pem_closes_on_its_own_line(self):
        shown, redacted = reader.redact_lines(["-----BEGIN X-----abc-----END X-----", "ok = 1"])
        assert redacted == [1]
        assert shown[1] == "ok = 1"


# ---------------------------------------------------------------------------
# Highlighting
# ---------------------------------------------------------------------------

PY = '''import os


# Greets.
@cache
def greet(name: str) -> str:
    """Say hello."""
    return f"hi {name}"


class Box:
    size = 3

    def open(self):
        return 1

    async def close(self):
        return 2
'''


class TestHighlighting:

    def test_each_line_is_runs_of_text_and_class_and_loses_nothing(self, tmp_path):
        write(tmp_path, "app.py", PY)
        payload = reader.file_payload(root_of(tmp_path), "app.py")
        source = PY.split("\n")[:-1]
        assert payload["line_count"] == len(source)
        assert ["".join(run[0] for run in line) for line in payload["lines"]] == source
        assert payload["language"] == "Python"
        greet = payload["lines"][5]
        assert ["def", "k"] in greet
        assert ["greet", "nf"] in greet
        assert payload["lines"][1] == []  # a blank line is no runs

    def test_a_string_spanning_lines_is_coloured_on_every_one(self, tmp_path):
        write(tmp_path, "doc.py", 'x = """one\ntwo\nthree"""\n')
        lines = reader.file_payload(root_of(tmp_path), "doc.py")["lines"]
        assert lines[1] == [["two", "s2"]]
        assert lines[2][0][1].startswith("s")

    def test_pages_are_windows_of_one_lexing(self, tmp_path):
        write(tmp_path, "doc.py", 'x = """\n' + "body\n" * 10 + '"""\n')
        page = reader.file_payload(root_of(tmp_path), "doc.py", start=5, end=7)
        assert (page["start"], page["end"], page["line_count"]) == (5, 7, 12)
        assert len(page["lines"]) == 3
        # Line 5 is inside the string even though the page starts there.
        assert page["lines"][0] == [["body", "s2"]]

    def test_a_page_past_the_end_is_empty(self, tmp_path):
        write(tmp_path, "a.py", "x = 1\n")
        page = reader.file_payload(root_of(tmp_path), "a.py", start=9)
        assert (page["lines"], page["start"], page["end"]) == ([], 0, 0)

    def test_lines_are_numbered_as_an_editor_numbers_them(self, tmp_path):
        write(tmp_path, "crlf.py", b"a = 1\r\nb = 2\r\n")
        write(tmp_path, "empty.py", "")
        write(tmp_path, "nonl.py", "a = 1")
        root = root_of(tmp_path)
        assert reader.file_payload(root, "crlf.py")["line_count"] == 2
        assert reader.file_payload(root, "empty.py")["line_count"] == 0
        assert reader.file_payload(root, "nonl.py")["line_count"] == 1

    def test_an_unknown_kind_of_file_is_plain_text(self, tmp_path):
        write(tmp_path, "notes.zzz", "just words\n")
        payload = reader.file_payload(root_of(tmp_path), "notes.zzz")
        assert payload["lines"] == [[["just words", ""]]]

    def test_a_minified_line_is_cut(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reader, "MAX_LINE_CHARS", 10)
        write(tmp_path, "min.js", "var a=1;" * 20 + "\n")
        text = "".join(r[0] for r in reader.file_payload(root_of(tmp_path), "min.js")["lines"][0])
        assert text.endswith(" …") and len(text) == 12

    def test_a_long_file_says_it_was_truncated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reader, "MAX_LINES", 5)
        write(tmp_path, "long.py", "x = 1\n" * 9)
        payload = reader.file_payload(root_of(tmp_path), "long.py")
        assert payload["truncated"] is True and payload["line_count"] == 5


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


def regions_of(tmp_path, rel: str, text: str) -> list[tuple]:
    write(tmp_path, rel, text)
    payload = reader.regions_payload(root_of(tmp_path), rel)
    assert payload["source"] == "tree-sitter", payload
    return [(r["kind"], r["name"], r["start"], r["end"], r["depth"]) for r in payload["regions"]]


class TestRegions:

    def test_python(self, tmp_path):
        assert regions_of(tmp_path, "app.py", PY) == [
            ("block", "", 1, 1, 0),
            # The comment and the decorator above a function are part of it.
            ("function", "greet", 4, 8, 0),
            ("class", "Box", 11, 18, 0),
            ("method", "open", 14, 15, 1),
            ("method", "close", 17, 18, 1),
        ]

    def test_a_comment_a_blank_line_away_is_not_the_functions(self, tmp_path):
        text = "# Section.\n\ndef f():\n    return 1\n"
        assert regions_of(tmp_path, "s.py", text) == [
            ("block", "", 1, 1, 0),
            ("function", "f", 3, 4, 0),
        ]

    def test_typescript(self, tmp_path):
        text = '''import { x } from "y";

/** Options. */
export interface Opts {
  a: number;
}

export const add = (a: number, b: number) => {
  return a + b;
};

const ONE = 1;

@Component({})
export class Thing {
  run(): void {
    return;
  }
}

enum E {
  A,
}
'''
        assert regions_of(tmp_path, "thing.ts", text) == [
            ("block", "", 1, 1, 0),
            ("interface", "Opts", 3, 6, 0),
            ("function", "add", 8, 10, 0),
            ("block", "", 12, 12, 0),
            ("class", "Thing", 14, 19, 0),
            ("method", "run", 16, 18, 1),
            ("enum", "E", 21, 23, 0),
        ]

    def test_kotlin(self, tmp_path):
        text = '''package a

/** A place. */
class Seat(val n: Int) {
    /** Where. */
    fun spot(): Int {
        return n
    }

    companion object {
        const val MAX = 4
    }
}

object Stage {
    fun ring() = 1
}
'''
        assert regions_of(tmp_path, "Seat.kt", text) == [
            ("block", "", 1, 1, 0),
            ("class", "Seat", 3, 13, 0),
            ("method", "spot", 5, 8, 1),
            ("object", "companion", 10, 12, 1),
            ("object", "Stage", 15, 17, 0),
            ("method", "ring", 16, 16, 1),
        ]

    def test_swift(self, tmp_path):
        text = '''import Foundation

/// A thing.
struct Thing {
    init() {
    }

    func run() -> Int {
        return 1
    }
}

extension Thing {
    func stop() {}
}

protocol Runs {
    func run() -> Int
}
'''
        assert regions_of(tmp_path, "Thing.swift", text) == [
            ("block", "", 1, 1, 0),
            ("struct", "Thing", 3, 11, 0),
            ("method", "init", 5, 6, 1),
            ("method", "run", 8, 10, 1),
            ("extension", "Thing", 13, 15, 0),
            ("method", "stop", 14, 14, 1),
            ("protocol", "Runs", 17, 19, 0),
            # A requirement is a declaration too, and can be pointed at.
            ("method", "run", 18, 18, 1),
        ]

    def test_go(self, tmp_path):
        text = '''package main

// Server serves.
type Server struct {
\taddr string
}

func (s *Server) Handle() error {
\treturn nil
}

func main() {
}
'''
        assert regions_of(tmp_path, "main.go", text) == [
            ("block", "", 1, 1, 0),
            ("struct", "Server", 3, 6, 0),
            ("method", "Handle", 8, 10, 0),
            ("function", "main", 12, 13, 0),
        ]

    def test_rust(self, tmp_path):
        text = '''/// A point.
#[derive(Debug)]
struct Point {
    x: i32,
}

impl Display for Point {
    fn fmt(&self) -> i32 {
        0
    }
}

mod inner {
    pub fn f() {}
}
'''
        assert regions_of(tmp_path, "lib.rs", text) == [
            ("struct", "Point", 1, 5, 0),
            ("impl", "Display for Point", 7, 11, 0),
            ("method", "fmt", 8, 10, 1),
            ("module", "inner", 13, 15, 0),
            # A function in a module is not a method.
            ("function", "f", 14, 14, 1),
        ]

    def test_java(self, tmp_path):
        text = '''package a;

/** Doc. */
@Deprecated
public class Foo {
    public Foo() {
    }

    @Override
    public String toString() {
        return "";
    }
}
'''
        assert regions_of(tmp_path, "Foo.java", text) == [
            ("block", "", 1, 1, 0),
            ("class", "Foo", 3, 13, 0),
            ("constructor", "Foo", 6, 7, 1),
            ("method", "toString", 9, 12, 1),
        ]

    def test_a_language_without_a_grammar_is_cut_into_paragraphs(self, tmp_path):
        text = "int f() {\n  a();\n\n  b();\n}\n\nint g() {\n}\n\n}\n"
        write(tmp_path, "x.c", text)
        payload = reader.regions_payload(root_of(tmp_path), "x.c")
        assert payload["source"] == "blocks"
        # Indented lines and closing brackets stay with the block they close.
        assert [(r["start"], r["end"]) for r in payload["regions"]] == [(1, 5), (7, 10)]

    def test_a_missing_grammar_falls_back_rather_than_failing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reader, "_LANGUAGES", {"python": None})
        write(tmp_path, "a.py", "def f():\n    return 1\n\n\nx = 2\n")
        payload = reader.regions_payload(root_of(tmp_path), "a.py")
        assert payload["source"] == "blocks"
        assert [(r["start"], r["end"]) for r in payload["regions"]] == [(1, 2), (5, 5)]

    def test_regions_carry_names_and_numbers_never_text(self, tmp_path):
        write(tmp_path, "cfg.py", 'def load():\n    token = "sk-live-abcdef"\n    return token\n')
        payload = reader.regions_payload(root_of(tmp_path), "cfg.py")
        assert payload["regions"] == [{"kind": "function", "name": "load", "start": 1, "end": 3, "depth": 0}]
        assert "sk-live" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    write(tmp_path, "app.py", "a = 1\nb = 2\nc = 3\n")
    write(tmp_path, "gone.py", "old = 1\n")
    write(tmp_path, ".env", "PASSWORD=hunter2\n")
    write(tmp_path, "logo.bin", b"\x00\x01\x02")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "one")
    return tmp_path


class TestDiff:

    def test_changes_come_back_as_files_of_hunks(self, repo):
        write(repo, "app.py", "a = 1\nb = 20\nc = 3\n")
        os.remove(repo / "gone.py")
        write(repo, "new.py", "fresh = 1\n")
        files = {f["path"]: f for f in reader.diff_payload(root_of(repo))["files"]}

        app = files["app.py"]
        assert app["status"] == "modified"
        (hunk,) = app["hunks"]
        assert (hunk["old_start"], hunk["old_lines"], hunk["new_start"], hunk["new_lines"]) == (1, 3, 1, 3)
        assert hunk["lines"] == [[" ", "a = 1"], ["-", "b = 2"], ["+", "b = 20"], [" ", "c = 3"]]

        assert files["gone.py"]["status"] == "deleted"
        assert files["new.py"]["status"] == "untracked"
        assert files["new.py"]["hunks"][0]["lines"] == [["+", "fresh = 1"]]

    def test_a_secret_file_is_withheld_and_a_secret_line_redacted(self, repo):
        write(repo, ".env", "PASSWORD=swordfish\n")
        write(repo, "app.py", 'a = 1\nAPI_KEY = "live-999"\nc = 3\n')
        write(repo, ".mcp.json", '{"headers": {"X-Api-Key": "abc"}}\n')
        payload = reader.diff_payload(root_of(repo))
        files = {f["path"]: f for f in payload["files"]}
        assert files[".env"]["withheld"] is True and files[".env"]["hunks"] == []
        assert files[".mcp.json"]["withheld"] is True and files[".mcp.json"]["hunks"] == []
        assert ["+", REDACTED] in files["app.py"]["hunks"][0]["lines"]
        blob = json.dumps(payload)
        for secret in ("swordfish", "hunter2", "live-999", "X-Api-Key"):
            assert secret not in blob

    def test_binary_and_oversized_files_are_flagged_not_shown(self, repo, monkeypatch):
        monkeypatch.setattr(reader, "MAX_UNTRACKED_BYTES", 10)
        write(repo, "logo.bin", b"\x00\x09\x09")
        write(repo, "huge.txt", "x" * 50 + "\n")
        files = {f["path"]: f for f in reader.diff_payload(root_of(repo))["files"]}
        assert files["logo.bin"]["binary"] is True and files["logo.bin"]["hunks"] == []
        assert files["huge.txt"]["omitted"] is True and files["huge.txt"]["hunks"] == []

    def test_a_rename_says_where_it_came_from(self, repo):
        git(repo, "mv", "app.py", "main.py")
        (entry,) = [f for f in reader.diff_payload(root_of(repo))["files"] if f["path"] == "main.py"]
        assert entry["status"] == "renamed" and entry["old_path"] == "app.py"

    def test_paths_are_relative_to_the_project_not_the_repository(self, repo):
        write(repo, "sub/mod.py", "x = 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "two")
        write(repo, "sub/mod.py", "x = 2\n")
        write(repo, "app.py", "outside the project\n")
        files = [f["path"] for f in reader.diff_payload(root_of(repo / "sub"))["files"]]
        assert files == ["mod.py"]

    def test_a_repository_with_no_commit_yet_diffs_against_nothing(self, tmp_path):
        git(tmp_path, "init", "-q")
        write(tmp_path, "first.py", "x = 1\n")
        git(tmp_path, "add", "first.py")
        (entry,) = reader.diff_payload(root_of(tmp_path))["files"]
        assert entry["path"] == "first.py" and entry["status"] == "added"

    def test_not_a_repository(self, tmp_path):
        assert status_of(lambda: reader.diff_payload(root_of(tmp_path))) == 409

    @pytest.mark.parametrize("setting,rel", [
        # w/app.py and c/app.py, unless the prefixes are spelled out.
        ("mnemonicPrefix = true", "app.py"),
        # No prefix at all, so a directory really named "b" loses its name.
        ("noprefix = true", "b/app.py"),
    ])
    def test_a_diff_is_parsed_whatever_the_users_git_config_says(
        self, repo, tmp_path_factory, monkeypatch, setting, rel,
    ):
        write(repo, rel, "a = 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "two")
        config = tmp_path_factory.mktemp("gitconfig") / "config"
        config.write_text(f"[diff]\n\t{setting}\n[color]\n\tui = always\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        write(repo, rel, "a = 2\n")
        (entry,) = reader.diff_payload(root_of(repo))["files"]
        assert entry["path"] == rel
        assert entry["hunks"][0]["lines"] == [["-", "a = 1"], ["+", "a = 2"]]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class TestFiles:

    def test_the_best_match_comes_first(self, repo):
        for rel in ["src/week.ts", "src/weekly_report.ts", "docs/week-plan.md", "src/sched/w_e_e_k.py"]:
            write(repo, rel, "x\n")
        found = reader.files_payload(root_of(repo), "week")["files"]
        # The name itself, then names that start with it, then letters in order.
        assert found[0] == "src/week.ts"
        assert set(found[1:3]) == {"docs/week-plan.md", "src/weekly_report.ts"}
        assert found[3:] == ["src/sched/w_e_e_k.py"]

    def test_secret_files_are_never_listed(self, repo):
        assert ".env" not in reader.files_payload(root_of(repo), "")["files"]
        assert reader.files_payload(root_of(repo), "env")["files"] == []

    def test_no_query_is_newest_first(self, repo):
        write(repo, "old.py", "x\n")
        write(repo, "new.py", "x\n")
        now = time.time()
        os.utime(repo / "old.py", (now - 1000, now - 1000))
        os.utime(repo / "new.py", (now + 10, now + 10))
        found = reader.files_payload(root_of(repo), "")["files"]
        assert found[0] == "new.py" and found[-1] == "old.py"

    def test_without_git_the_tree_is_walked_skipping_dependencies(self, tmp_path):
        write(tmp_path, "src/a.py", "x\n")
        write(tmp_path, "node_modules/lib/index.js", "x\n")
        write(tmp_path, ".hidden/b.py", "x\n")
        payload = reader.files_payload(root_of(tmp_path), "")
        assert payload["source"] == "walk"
        assert payload["files"] == ["src/a.py"]


# ---------------------------------------------------------------------------
# Deixis
# ---------------------------------------------------------------------------


class TestDeixis:

    def test_a_pointed_region_becomes_a_mention(self, tmp_path):
        write(tmp_path, "src/scheduler/week.ts", "x\n")
        ref = {"path": "src/scheduler/week.ts", "lines": [142, 168], "symbol": "formatISOWeek"}
        assert reader.expand_reference("what does this do?", ref, "claude", root_of(tmp_path)) == (
            "what does this do? @src/scheduler/week.ts (lines 142-168, formatISOWeek)"
        )

    def test_one_line_and_no_detail(self, tmp_path):
        write(tmp_path, "a.py", "x\n")
        root = root_of(tmp_path)
        assert reader.expand_reference("why?", {"path": "a.py", "lines": [7, 7]}, "claude", root) == "why? @a.py (line 7)"
        assert reader.expand_reference("open it", {"path": "a.py"}, "claude", root) == "open it @a.py"
        assert reader.expand_reference("", {"path": "a.py"}, "claude", root) == "@a.py"

    @pytest.mark.parametrize("assistant", ["codex", "gemini", "antigravity", "grok", "composer"])
    def test_an_assistant_not_yet_watched_taking_a_mention_gets_the_plain_path(self, tmp_path, assistant):
        # An @ typed into a TUI can open a popup that eats the Enter. Only
        # Claude Code has been watched getting it right.
        write(tmp_path, "a.py", "x\n")
        got = reader.expand_reference("why?", {"path": "a.py", "lines": [2, 4]}, assistant, root_of(tmp_path))
        assert got == "why? a.py (lines 2-4)"

    def test_a_path_with_a_space_is_quoted_never_mentioned(self):
        assert reader.mention("claude", "my docs/a.md") == '"my docs/a.md"'
        assert reader.mention("codex", "src/a.py") == "src/a.py"

    def test_a_symbol_that_is_not_a_name_is_dropped(self, tmp_path):
        write(tmp_path, "a.py", "x\n")
        ref = {"path": "a.py", "lines": [1, 2], "symbol": "x; rm -rf ~"}
        assert reader.expand_reference("hm", ref, "claude", root_of(tmp_path)) == "hm @a.py (lines 1-2)"

    @pytest.mark.parametrize("ref,status", [
        ({"path": "../x.py"}, 403),
        ({"path": ".env"}, 403),
        ({"path": "missing.py"}, 404),
        ({"path": "a.py", "lines": [5, 2]}, 400),
        ({"path": "a.py", "lines": [0, 2]}, 400),
        ({"path": "a.py", "lines": ["1", "2"]}, 400),
        ({"path": "a.py", "lines": [True, 2]}, 400),
        ({"lines": [1, 2]}, 400),
        ("a.py", 400),
    ])
    def test_a_reference_the_reader_would_not_serve_is_refused(self, tmp_path, ref, status):
        write(tmp_path, "a.py", "x\n")
        write(tmp_path, ".env", "X=1\n")
        root = root_of(tmp_path)
        assert status_of(lambda: reader.expand_reference("what?", ref, "claude", root)) == status


# ---------------------------------------------------------------------------
# Routes and dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def home_project():
    """A project dir under $HOME: registration refuses anything else."""
    path = os.path.join(os.path.expanduser("~"), ".voxherd-test-projects", f"reader-{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


class TestRoutes:

    async def test_file_regions_and_files_by_session(self, client, home_project):
        write(home_project, "app.py", PY)
        await register_test_session(client, session_id="rd-1", project="readerproj", project_dir=home_project)
        params = {"session_id": "rd-1", "path": "app.py"}
        body = (await client.get("/api/file", params=params, headers=auth_headers())).json()
        assert (body["session_id"], body["project"], body["line_count"]) == ("rd-1", "readerproj", 18)
        body = (await client.get("/api/regions", params=params, headers=auth_headers())).json()
        assert [r["name"] for r in body["regions"] if r["depth"] == 0 and r["name"]] == ["greet", "Box"]
        body = (await client.get("/api/files", params={"project": "readerproj", "q": "app"},
                                 headers=auth_headers())).json()
        assert body["files"] == ["app.py"]

    async def test_a_project_is_matched_exactly_never_nearly(self, client, home_project):
        write(home_project, "app.py", "x\n")
        await register_test_session(client, session_id="rd-2", project="readerproj", project_dir=home_project)
        resp = await client.get("/api/file", params={"project": "readerpro", "path": "app.py"},
                                headers=auth_headers())
        assert resp.status_code == 404 and "error" in resp.json()
        resp = await client.get("/api/file", params={"project": "ReaderProj", "path": "app.py"},
                                headers=auth_headers())
        assert resp.status_code == 200

    async def test_refusals_are_errors_with_their_status(self, client, home_project):
        write(home_project, ".env", "X=1\n")
        await register_test_session(client, session_id="rd-3", project="readerproj", project_dir=home_project)
        for params, status in [
            ({"session_id": "rd-3", "path": ".env"}, 403),
            ({"session_id": "rd-3", "path": "../../x"}, 403),
            ({"session_id": "nobody", "path": "a.py"}, 404),
            ({"path": "a.py"}, 400),
        ]:
            resp = await client.get("/api/file", params=params, headers=auth_headers())
            assert resp.status_code == status, params
            assert resp.json()["error"]

    async def test_the_reader_needs_the_token(self, client, home_project):
        await register_test_session(client, session_id="rd-4", project="readerproj", project_dir=home_project)
        for route in ("/api/file", "/api/regions", "/api/diff", "/api/files"):
            resp = await client.get(route, params={"session_id": "rd-4", "path": "a.py"})
            assert resp.status_code == 401, route

    async def test_diff_by_session(self, client, home_project):
        git(home_project, "init", "-q")
        write(home_project, "a.py", "x = 1\n")
        git(home_project, "add", "-A")
        git(home_project, "commit", "-q", "-m", "one")
        write(home_project, "a.py", "x = 2\n")
        await register_test_session(client, session_id="rd-5", project="readerproj", project_dir=home_project)
        body = (await client.get("/api/diff", params={"session_id": "rd-5"}, headers=auth_headers())).json()
        assert [f["path"] for f in body["files"]] == ["a.py"]

    async def test_rest_command_expands_a_reference(self, client, home_project):
        write(home_project, "src/week.ts", "x\n")
        await register_test_session(client, session_id="rd-6", project="readerproj", project_dir=home_project)
        with patch.object(ws_handler, "_dispatch_agent", new=AsyncMock()) as dispatch:
            resp = await client.post("/api/command", headers=auth_headers(), json={
                "project": "readerproj", "session_id": "rd-6", "message": "what does this do?",
                "reference": {"path": "src/week.ts", "lines": [3, 9], "symbol": "week"},
            })
            await asyncio.sleep(0)
        assert resp.json() == {"ok": True}
        assert dispatch.await_args.args[2] == "what does this do? @src/week.ts (lines 3-9, week)"

    async def test_rest_command_refuses_a_bad_reference_and_sends_nothing(self, client, home_project):
        await register_test_session(client, session_id="rd-7", project="readerproj", project_dir=home_project)
        with patch.object(ws_handler, "_dispatch_agent", new=AsyncMock()) as dispatch:
            resp = await client.post("/api/command", headers=auth_headers(), json={
                "project": "readerproj", "session_id": "rd-7", "message": "what?",
                "reference": {"path": "../../etc/hosts"},
            })
            await asyncio.sleep(0)
        assert "reference" in resp.json()["error"]
        dispatch.assert_not_awaited()


class TestVoiceCommand:
    """The headset's path: ``voice_command`` with a ``reference``."""

    @pytest.fixture(autouse=True)
    def _registry(self, monkeypatch):
        saved = ws_handler.sessions._sessions.copy()
        ws_handler.sessions._sessions.clear()
        monkeypatch.setattr(ws_handler, "_check_dispatch_rate", lambda _sid: True)
        with patch.object(ws_handler.sessions, "_save", return_value=None):
            yield
        ws_handler.sessions._sessions.clear()
        ws_handler.sessions._sessions.update(saved)

    def _session(self, root: str, status: str = "idle", assistant: str = "claude") -> Session:
        s = Session(session_id="vc-1", project="homeschool", project_dir=root,
                    status=status, tmux_target=None, assistant=assistant)
        ws_handler.sessions._sessions["vc-1"] = s
        return s

    async def _say(self, data: dict) -> tuple[AsyncMock, AsyncMock, list[dict]]:
        broadcasts: list[dict] = []

        async def capture(msg):
            broadcasts.append(msg)

        with patch.object(ws_handler, "_dispatch_agent", new=AsyncMock()) as dispatch, \
             patch.object(ws_handler, "send_signed", new=AsyncMock()) as sent, \
             patch.object(ws_handler, "broadcast_to_ios", new=capture):
            await ws_handler.handle_voice_command(data, websocket=None)
            await asyncio.sleep(0)
        return dispatch, sent, broadcasts

    async def test_the_agent_receives_what_was_pointed_at(self, tmp_path):
        write(tmp_path, "src/scheduler/week.ts", "x\n")
        self._session(root_of(tmp_path))
        dispatch, _, broadcasts = await self._say({
            "type": "voice_command", "session_id": "vc-1", "message": "what does this do?",
            "reference": {"path": "src/scheduler/week.ts", "lines": [142, 168], "symbol": "formatISOWeek"},
        })
        want = "what does this do? @src/scheduler/week.ts (lines 142-168, formatISOWeek)"
        assert dispatch.await_args.args[2] == want
        assert [b["message"] for b in broadcasts if b["type"] == "command_accepted"] == [want]

    async def test_a_busy_agent_queues_the_expanded_words(self, tmp_path):
        write(tmp_path, "a.py", "x\n")
        s = self._session(root_of(tmp_path), status="active")
        with patch.object(ws_handler, "_check_session_actually_idle", new=AsyncMock(return_value=False)):
            await self._say({"session_id": "vc-1", "message": "why?", "reference": {"path": "a.py", "lines": [1, 1]}})
        assert s.queued_command == "why? @a.py (line 1)"

    async def test_a_reference_that_cannot_be_served_sends_nothing(self, tmp_path):
        write(tmp_path, ".env", "X=1\n")
        self._session(root_of(tmp_path))
        dispatch, sent, broadcasts = await self._say({
            "session_id": "vc-1", "message": "what does this do?", "reference": {"path": ".env"},
        })
        dispatch.assert_not_awaited()
        assert not [b for b in broadcasts if b["type"] == "command_accepted"]
        (error,) = [c.args[1] for c in sent.await_args_list]
        assert error["type"] == "error" and "pointed at" in error["message"]

    async def test_without_a_reference_nothing_changes(self, tmp_path):
        self._session(root_of(tmp_path))
        dispatch, _, _ = await self._say({"session_id": "vc-1", "message": "run the tests"})
        assert dispatch.await_args.args[2] == "run the tests"
