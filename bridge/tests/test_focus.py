"""Follow mode's bridge half: which file each agent is editing (bridge/focus.py).

Its failures are quiet. A Reader following the wrong file, the lines one off,
or an edit announced every poll all look like an agent at work. So the arithmetic
is pinned first, then what counts as an edit, then the posture, then the wire.

The transcript shapes are the ones on disk: Claude Code's ``toolUseResult`` and
Codex's ``item_completed``/``FileChange``, read off real transcripts (keys and
numbers only) on 2026-09-28.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import types
import uuid
from unittest.mock import AsyncMock

import pytest

from bridge import focus, transcript_render
from bridge.session_manager import SessionManager
from bridge.tests.conftest import auth_headers, register_test_session

SID = "5d2c1f42-7def-43ef-bc86-b02ade04a7cf"
THREAD = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


@pytest.fixture(autouse=True)
def _fresh():
    focus.reset()
    yield
    focus.reset()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The agents' own directories (~/.claude, ~/.codex), as one allowed root."""
    root = tmp_path / "agents"
    root.mkdir()
    real = os.path.realpath(str(root))
    monkeypatch.setattr(transcript_render, "_allowed_transcript_roots", lambda assistant="claude": [real])
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "project"
    (path / "src").mkdir(parents=True)
    # `register_session` requires a project_dir under the user's home (that
    # containment is what decides where the Reader can be rooted), and pytest's
    # tmp_path is not. Point HOME at tmp_path so this stays an isolated
    # temporary directory AND satisfies the real check, rather than exempting
    # the test from it.
    monkeypatch.setenv("HOME", str(tmp_path))
    return os.path.realpath(str(path))


def session(project_dir: str, assistant: str = "claude", sid: str = SID):
    return types.SimpleNamespace(
        assistant=assistant, session_id=sid, project="homeschool", project_dir=project_dir,
        _last_focus_id=None,
    )


def claude_transcript(home, entries: list, tail: str = "") -> str:
    folder = home / "projects" / "-Users-dev-homeschool"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{SID}.jsonl"
    with open(path, "w") as f:
        f.writelines(json.dumps(e) + "\n" for e in entries)
        f.write(tail)
    return str(path)


def append(path: str, entries: list, tail: str = "") -> None:
    with open(path, "a") as f:
        f.writelines(json.dumps(e) + "\n" for e in entries)
        f.write(tail)


def edit(path: str, new_start: int, lines: list[str], entry_id: str = "e1", sidechain: bool = False) -> dict:
    """An Edit's result, as Claude Code records it on the user turn after the call."""
    return {
        "type": "user", "uuid": entry_id, "timestamp": "2026-09-28T10:00:00.000Z", "isSidechain": sidechain,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        "toolUseResult": {
            "filePath": path, "oldString": "a", "newString": "b", "originalFile": None,
            "structuredPatch": [{"oldStart": new_start, "oldLines": 3, "newStart": new_start,
                                 "newLines": len(lines), "lines": lines}],
            "userModified": False, "replaceAll": False,
        },
    }


def create(path: str, content: str, entry_id: str = "c1") -> dict:
    return {
        "type": "user", "uuid": entry_id, "timestamp": "2026-09-28T10:01:00.000Z",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}]},
        "toolUseResult": {"type": "create", "filePath": path, "content": content, "structuredPatch": [],
                          "originalFile": None, "userModified": False},
    }


def read(path: str) -> dict:
    return {
        "type": "user", "uuid": "r1",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_3", "content": "..."}]},
        "toolUseResult": {"type": "text", "file": {"filePath": path, "content": "x", "numLines": 1,
                                                   "startLine": 1, "totalLines": 1}},
    }


def failed(path: str) -> dict:
    return {"type": "user", "uuid": "f1", "toolUseResult": "Error: String to replace not found in file.",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": path, "is_error": True}]}}


def prompt(text: str = "go on") -> dict:
    return {"type": "user", "uuid": "p1", "message": {"role": "user", "content": text}}


# A hunk of three context lines either side of a two-line change at 12-13.
HUNK = [" a", " b", " c", "-old", "+new one", "+new two", " d", " e", " f"]


# ---------------------------------------------------------------------------
# Where a hunk changed the file
# ---------------------------------------------------------------------------


class TestSpans:

    def test_the_added_lines_not_the_context(self):
        assert focus.hunk_span(9, 8, HUNK) == (12, 13)

    def test_a_replacement_is_its_new_line(self):
        assert focus.hunk_span(4, 3, [" a", "-b", "+B", " c"]) == (5, 5)

    def test_a_deletion_is_the_line_above_where_it_was(self):
        assert focus.hunk_span(10, 6, [" a", " b", " c", "-x", "-y", " d", " e", " f"]) == (12, 12)

    def test_git_zero_length_start_already_names_the_line_above(self):
        # `@@ -5,2 +4,0 @@`: the lines went after line 4.
        assert focus.hunk_span(4, 0, ["-x", "-y"]) == (4, 4)

    def test_a_deletion_at_the_top_is_line_one(self):
        assert focus.hunk_span(1, 3, ["-x", " a", " b", " c"]) == (1, 1)
        assert focus.hunk_span(0, 0, ["-x"]) == (1, 1)

    def test_diff_hunks_in_order_and_no_newline_marker_is_not_a_line(self):
        diff = "\n".join([
            "@@ -1,3 +1,4 @@", " a", "+b", " c", " d",
            "@@ -20,3 +21,3 @@", " x", "-y", "+Y", " z", "\\ No newline at end of file",
        ])
        assert focus.diff_ranges(diff) == [(2, 2), (22, 22)]

    def test_no_hunks_no_ranges(self):
        assert focus.diff_ranges("") == [] and focus.diff_ranges(None) == []


# ---------------------------------------------------------------------------
# Claude Code: what counts as an edit
# ---------------------------------------------------------------------------


class TestClaude:

    def test_the_newest_edit_in_the_project_is_the_focus(self, home, project):
        claude_transcript(home, [
            prompt(),
            edit(f"{project}/src/old.py", 1, [" a", "+b", " c"], "e1"),
            edit(f"{project}/src/week.ts", 9, HUNK, "e2"),
        ])
        assert focus.payload(session(project)) == {
            "path": "src/week.ts", "ranges": [[12, 13]], "verb": "edited", "id": "e2",
            "at": "2026-09-28T10:00:00.000Z", "withheld": False,
        }

    def test_a_created_file_is_all_new(self, home, project):
        claude_transcript(home, [create(f"{project}/src/new.py", "one\ntwo\nthree\n")])
        got = focus.payload(session(project))
        assert (got["path"], got["verb"], got["ranges"]) == ("src/new.py", "created", [[1, 3]])

    def test_reads_failures_and_sub_agents_are_not_edits(self, home, project):
        claude_transcript(home, [
            edit(f"{project}/src/kept.py", 1, [" a", "+b"], "kept"),
            read(f"{project}/src/read.py"),
            failed(f"{project}/src/failed.py"),
            edit(f"{project}/src/sub.py", 1, [" a", "+b"], "sub", sidechain=True),
            prompt(),
        ])
        assert focus.payload(session(project))["id"] == "kept"

    def test_an_edit_outside_the_project_does_not_hide_the_one_inside(self, home, project, tmp_path):
        claude_transcript(home, [
            edit(f"{project}/src/inside.py", 1, [" a", "+b"], "inside"),
            edit(str(tmp_path / "elsewhere.py"), 1, [" a", "+b"], "outside"),
        ])
        assert focus.payload(session(project))["path"] == "src/inside.py"

    def test_a_link_out_of_the_project_is_outside_it(self, home, project, tmp_path):
        (tmp_path / "secret").mkdir()
        os.symlink(tmp_path / "secret", os.path.join(project, "src", "link"))
        claude_transcript(home, [edit(f"{project}/src/link/key.py", 1, [" a", "+b"], "linked")])
        assert focus.payload(session(project)) is None

    def test_a_file_never_served_is_named_and_withheld(self, home, project):
        claude_transcript(home, [edit(f"{project}/.env", 1, [" A=1", "+B=2"], "env")])
        got = focus.payload(session(project))
        assert (got["path"], got["withheld"]) == (".env", True)

    def test_a_write_that_changed_nothing_is_not_an_edit(self, home, project):
        same = edit(f"{project}/src/same.py", 1, [], "same")
        same["toolUseResult"]["structuredPatch"] = []
        claude_transcript(home, [edit(f"{project}/src/real.py", 1, [" a", "+b"], "real"), same])
        assert focus.payload(session(project))["id"] == "real"

    def test_ranges_are_capped(self, home, project):
        entry = edit(f"{project}/src/many.py", 1, [" a", "+b"], "many")
        entry["toolUseResult"]["structuredPatch"] = [
            {"oldStart": n * 10, "oldLines": 1, "newStart": n * 10, "newLines": 2, "lines": [" a", "+b"]}
            for n in range(1, 40)
        ]
        claude_transcript(home, [entry])
        got = focus.payload(session(project))["ranges"]
        assert len(got) == focus.MAX_RANGES and got[0] == [11, 11]


# ---------------------------------------------------------------------------
# Reading only what is new
# ---------------------------------------------------------------------------


class TestIncremental:

    def test_an_appended_edit_is_found_and_other_lines_keep_the_last(self, home, project):
        path = claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "one")])
        s = session(project)
        assert focus.payload(s)["id"] == "one"
        append(path, [prompt(), read(f"{project}/src/x.py")])
        assert focus.payload(s)["id"] == "one"
        append(path, [edit(f"{project}/src/b.py", 1, [" a", "+b"], "two")])
        assert focus.payload(s)["id"] == "two"

    def test_a_line_still_being_written_is_read_once_it_is_whole(self, home, project):
        whole = json.dumps(edit(f"{project}/src/b.py", 1, [" a", "+b"], "two")) + "\n"
        path = claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "one")])
        s = session(project)
        assert focus.payload(s)["id"] == "one"
        # A whole line, then the start of the next: only the whole one is read.
        append(path, [prompt()], tail=whole[:40])
        assert focus.payload(s)["id"] == "one"
        append(path, [], tail=whole[40:])
        assert focus.payload(s)["id"] == "two"

    def test_an_appended_edit_outside_the_project_is_not_followed(self, home, project, tmp_path):
        path = claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "inside")])
        s = session(project)
        assert focus.payload(s)["id"] == "inside"
        append(path, [edit(str(tmp_path / "elsewhere.py"), 1, [" a", "+b"], "outside")])
        assert focus.payload(s)["id"] == "inside"

    def test_a_line_nobody_foresaw_is_skipped_not_fatal(self, home, project, monkeypatch):
        path = claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "kept")])
        real = focus._PARSERS["claude"]

        def fussy(raw: bytes):
            if b"surprise" in raw:
                raise TypeError("a shape nobody foresaw")
            return real(raw)

        monkeypatch.setitem(focus._PARSERS, "claude", fussy)
        append(path, [prompt("surprise")])
        s = session(project)
        assert focus.payload(s)["id"] == "kept"
        append(path, [prompt("another surprise")])
        assert focus.payload(s)["id"] == "kept"

    def test_first_sight_of_a_half_written_line_reads_it_once_whole(self, home, project):
        whole = json.dumps(edit(f"{project}/src/b.py", 1, [" a", "+b"], "two")) + "\n"
        path = claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "one")], tail=whole[:40])
        s = session(project)
        assert focus.payload(s)["id"] == "one"
        append(path, [], tail=whole[40:])
        assert focus.payload(s)["id"] == "two"

    def test_a_poll_that_finds_nothing_new_parses_nothing(self, home, project, monkeypatch):
        claude_transcript(home, [prompt(), edit(f"{project}/src/a.py", 1, [" a", "+b"], "one"), prompt()])
        s = session(project)
        focus.payload(s)
        parsed = []
        real = focus._PARSERS["claude"]
        monkeypatch.setitem(focus._PARSERS, "claude", lambda raw: parsed.append(raw) or real(raw))
        assert focus.payload(s)["id"] == "one"
        assert parsed == []

    def test_a_replaced_transcript_is_read_again(self, home, project):
        path = claude_transcript(home, [prompt("x" * 5000), edit(f"{project}/src/a.py", 1, [" a", "+b"], "one")])
        s = session(project)
        assert focus.payload(s)["id"] == "one"
        claude_transcript(home, [edit(f"{project}/src/c.py", 1, [" a", "+b"], "fresh")])
        assert os.path.getsize(path) < 5000
        assert focus.payload(s)["id"] == "fresh"


# ---------------------------------------------------------------------------
# Posture
# ---------------------------------------------------------------------------


class TestPosture:

    def test_an_assistant_whose_edits_are_unread_is_not_followed(self, home, project):
        claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"])])
        for assistant in ("gemini", "antigravity", "grok"):
            assert not focus.followable(assistant)
            assert focus.payload(session(project, assistant=assistant)) is None

    def test_a_session_id_that_is_not_a_uuid_finds_nothing(self, home, project):
        claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"])])
        for bad in ("../../x", "*", "discovered-0011223344556677"):
            assert focus.payload(session(project, sid=bad)) is None

    def test_a_transcript_linked_from_outside_the_agents_root_is_not_read(self, home, project, tmp_path):
        outside = tmp_path / "outside.jsonl"
        outside.write_text(json.dumps(edit(f"{project}/src/a.py", 1, [" a", "+b"])) + "\n")
        folder = home / "projects" / "-Users-dev-homeschool"
        folder.mkdir(parents=True)
        os.symlink(outside, folder / f"{SID}.jsonl")
        assert focus.payload(session(project)) is None

    def test_a_project_directory_that_is_gone_follows_nothing(self, home, project):
        claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"])])
        assert focus.payload(session(project + "-gone")) is None


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def rollout(home, items: list[dict]) -> str:
    folder = home / "sessions" / "2026" / "09" / "28"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-09-28T10-00-00-{THREAD}.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"timestamp": "2026-09-28T10:00:00.000Z", "type": "session_meta",
                            "payload": {"id": THREAD}}) + "\n")
        f.writelines(
            json.dumps({"timestamp": f"2026-09-28T10:00:0{n}.000Z", "type": "event_msg",
                        "payload": {"type": "item_completed", "item": item}}) + "\n"
            for n, item in enumerate(items)
        )
    return str(path)


def file_change(changes: dict, status: str = "completed", item_id: str = "call_1") -> dict:
    return {"type": "FileChange", "id": item_id, "changes": changes, "status": status, "stdout": "", "stderr": ""}


class TestCodex:

    def codex(self, project):
        return session(project, assistant="codex", sid=f"codex-{THREAD}")

    def test_an_applied_patch_is_the_focus(self, home, project):
        rollout(home, [file_change({
            f"{project}/src/week.ts": {"type": "update", "move_path": None,
                                       "unified_diff": "@@ -9,7 +9,8 @@\n" + "\n".join(HUNK) + "\n"},
        })])
        got = focus.payload(self.codex(project))
        assert (got["path"], got["ranges"], got["verb"]) == ("src/week.ts", [[12, 13]], "edited")

    def test_a_failed_patch_changed_nothing(self, home, project):
        rollout(home, [
            file_change({f"{project}/src/a.py": {"type": "update", "unified_diff": "@@ -1,1 +1,2 @@\n a\n+b\n"}},
                        item_id="good"),
            file_change({f"{project}/src/b.py": {"type": "update", "unified_diff": "@@ -1,1 +1,2 @@\n a\n+b\n"}},
                        status="failed", item_id="bad"),
        ])
        assert focus.payload(self.codex(project))["path"] == "src/a.py"

    def test_a_new_file_a_rename_and_a_deletion(self, home, project):
        rollout(home, [file_change({f"{project}/src/new.py": {"type": "add", "content": "a\nb\n"}})])
        got = focus.payload(self.codex(project))
        assert (got["path"], got["verb"], got["ranges"]) == ("src/new.py", "created", [[1, 2]])

        focus.reset()
        rollout(home, [file_change({
            f"{project}/src/gone.py": {"type": "delete", "content": "x\n"},
            f"{project}/src/old.py": {"type": "update", "move_path": f"{project}/src/moved.py",
                                      "unified_diff": "@@ -1,1 +1,2 @@\n a\n+b\n"},
        })])
        assert focus.payload(self.codex(project))["path"] == "src/moved.py"

    def test_a_patch_to_several_files_is_followed_where_it_starts(self, home, project):
        rollout(home, [file_change({
            f"{project}/src/impl.py": {"type": "update", "unified_diff": "@@ -1 +1,2 @@\n a\n+b\n"},
            f"{project}/tests/test_impl.py": {"type": "update", "unified_diff": "@@ -1 +1,2 @@\n a\n+b\n"},
        })])
        assert focus.payload(self.codex(project))["path"] == "src/impl.py"

    def test_a_session_found_to_be_codex_is_looked_for_as_codex_at_once(self, home, project):
        # The activity poll corrects a session's assistant when its pane runs
        # another; a miss remembered for the old one must not hide the rollout.
        rollout(home, [file_change({f"{project}/src/a.py": {"type": "update", "unified_diff": "@@ -1 +1,2 @@\n a\n+b\n"}})])
        s = session(project, assistant="claude", sid=f"codex-{THREAD}")
        assert focus.transcript_of(s) is None
        s.assistant = "codex"
        assert focus.payload(s)["path"] == "src/a.py"

    def test_only_a_session_named_for_its_thread_finds_a_rollout(self, home, project):
        rollout(home, [file_change({f"{project}/src/a.py": {"type": "update", "unified_diff": "@@ -1 +1,2 @@\n a\n+b\n"}})])
        assert focus.payload(session(project, assistant="codex", sid=THREAD)) is None
        assert focus.payload(session(project, assistant="codex", sid="codex-../../x")) is None


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


class TestAnnouncing:

    def test_each_edit_is_announced_once(self, home, project):
        path = claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "one")])
        s = session(project)
        first = focus.changed(s, focus.payload(s))
        assert first == {"type": "agent_focus", "session_id": SID, "project": "homeschool",
                         "focus": focus.payload(s)}
        assert focus.changed(s, focus.payload(s)) is None
        append(path, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "two")])
        assert focus.changed(s, focus.payload(s))["focus"]["id"] == "two"

    def test_nothing_edited_is_not_news(self, home, project):
        claude_transcript(home, [prompt()])
        s = session(project)
        assert focus.changed(s, focus.payload(s)) is None

    async def test_the_activity_poll_broadcasts_a_new_edit_once(self, home, project, tmp_path, monkeypatch):
        from bridge import activity

        monkeypatch.setattr("bridge.session_manager._PERSIST_PATH", str(tmp_path / "sessions.json"))
        mgr = SessionManager()
        mgr._sessions.clear()
        mgr.register_session(SID, "homeschool", project, tmux_target="vh-h:0.0")
        claude_transcript(home, [edit(f"{project}/src/a.py", 1, [" a", "+b"], "one")])

        async def pane_exists(_target):
            return True

        async def fg(_target):
            return "2.1.283"

        async def capture(*args, **kwargs):
            class Proc:
                returncode = 0

                async def communicate(self):
                    return (b"\xe2\x9d\xaf \n", b"")
            return Proc()

        broadcast = AsyncMock()
        monkeypatch.setattr(activity, "sessions", mgr)
        monkeypatch.setattr(mgr, "_tmux_pane_exists", pane_exists)
        monkeypatch.setattr(activity, "_pane_fg_command", fg)
        monkeypatch.setattr("asyncio.create_subprocess_exec", capture)
        monkeypatch.setattr(activity, "broadcast_to_ios", broadcast)
        monkeypatch.setattr(activity, "_state_sync_msg", lambda: {"type": "state_sync"})

        task = asyncio.create_task(activity._activity_poll_loop())
        await asyncio.sleep(3.4)  # two ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        sent = [c.args[0] for c in broadcast.await_args_list if c.args[0].get("type") == "agent_focus"]
        assert [m["focus"]["path"] for m in sent] == ["src/a.py"]


# ---------------------------------------------------------------------------
# GET /api/focus
# ---------------------------------------------------------------------------


@pytest.fixture
def home_project():
    """A project dir under $HOME: registration refuses anything else."""
    path = os.path.join(os.path.expanduser("~"), ".voxherd-test-projects", f"focus-{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    yield os.path.realpath(path)
    shutil.rmtree(path, ignore_errors=True)


class TestRoute:

    async def test_the_focus_by_session(self, client, home, home_project):
        claude_transcript(home, [edit(f"{home_project}/src/week.ts", 9, HUNK, "e2")])
        await register_test_session(client, session_id=SID, project="focusproj", project_dir=home_project)
        resp = await client.get("/api/focus", params={"session_id": SID}, headers=auth_headers())
        body = resp.json()
        assert resp.status_code == 200
        assert (body["session_id"], body["project"], body["assistant"]) == (SID, "focusproj", "claude")
        assert (body["focus"]["path"], body["focus"]["ranges"]) == ("src/week.ts", [[12, 13]])

    async def test_nothing_edited_yet_is_a_null_focus(self, client, home, home_project):
        claude_transcript(home, [prompt()])
        await register_test_session(client, session_id=SID, project="focusproj", project_dir=home_project)
        body = (await client.get("/api/focus", params={"project": "focusproj"}, headers=auth_headers())).json()
        assert body["focus"] is None

    async def test_an_assistant_it_cannot_follow_says_so(self, client, home, home_project):
        await register_test_session(client, session_id=SID, project="focusproj", project_dir=home_project,
                                    assistant="gemini")
        resp = await client.get("/api/focus", params={"session_id": SID}, headers=auth_headers())
        assert resp.status_code == 422 and resp.json()["error"] == focus.NOT_FOLLOWABLE

    async def test_an_unknown_session_and_a_missing_token(self, client, home, home_project):
        resp = await client.get("/api/focus", params={"session_id": "nobody"}, headers=auth_headers())
        assert resp.status_code == 404 and resp.json()["error"]
        await register_test_session(client, session_id=SID, project="focusproj", project_dir=home_project)
        assert (await client.get("/api/focus", params={"session_id": SID})).status_code == 401
