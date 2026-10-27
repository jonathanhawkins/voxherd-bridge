"""Task list names become directories under ``~/.claude/tasks/``.

``_PROJECT_NAME_RE`` alone is not enough for that: its character class permits
"." and "..", which ``task_store._sanitize_id`` rejects by *raising*, and an
unhandled ValueError here was a 500 over REST and — over the WebSocket, whose
receive loop catches only WebSocketDisconnect — a torn-down connection.

These also exist because nothing exercised ``_valid_task_project`` at all: it
called a helper ws_handler never imported, so the first ``task_create`` from any
client would have been a NameError, and the whole suite stayed green.
"""

from __future__ import annotations

import pytest

from bridge.ws_handler import _task_text, _valid_task_project


class TestValidTaskProject:
    @pytest.mark.parametrize("name", ["myproject", "voxherd-notes", "a_b.c", "with spaces"])
    def test_ordinary_names_pass_through(self, name):
        assert _valid_task_project(name) == name

    @pytest.mark.parametrize("name", [".", "..", "a/b", "a\\b", "../etc", "/abs"])
    def test_path_shapes_are_refused(self, name):
        assert _valid_task_project(name) is None, f"{name!r} could escape the task root"

    @pytest.mark.parametrize("name", ["", "   ", None, 5, [], {}])
    def test_empty_and_non_strings_are_refused(self, name):
        assert _valid_task_project(name) is None

    def test_an_over_long_name_is_refused(self):
        assert _valid_task_project("x" * 200) is None

    def test_it_does_not_raise_on_anything(self):
        # The original failure mode was an exception, not a wrong answer.
        for raw in (".", "..", None, 5, "x" * 500, "a/b", "\x00", "ok"):
            _valid_task_project(raw)


class TestTaskText:
    def test_text_is_capped(self):
        assert len(_task_text("x" * 5000)) == 500

    def test_control_characters_are_stripped(self):
        assert "\x00" not in _task_text("a\x00b")
        assert "\x1b" not in _task_text("a\x1bb")

    def test_non_strings_become_empty(self):
        assert _task_text(123) == ""
        assert _task_text(None) == ""
