"""Tests for sub_agent_monitor.

Regression coverage for two bugs the user hit on real data:

1. Multiple tmux sessions for the same project (aligned-tools-0 +
   aligned-tools-1) were both being attributed the tasks from a single
   project-name directory in ~/.claude/tasks/. Each session showed N
   sub-agents when only one — or none — was actually running them.

2. Stale in_progress task JSON files from a crashed/killed previous
   Claude Code run were still counted as live sub-agents, producing
   phantom "5 sub-agents" badges with nothing running.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bridge import sub_agent_monitor


class _FakeSession:
    """Minimal Session-shaped object for sub_agent_monitor.get_all_sub_agent_counts."""

    def __init__(self, session_id: str, project: str) -> None:
        self.session_id = session_id
        self.project = project


def _write_task(dir_path: Path, task_id: str, status: str, *, mtime_age_seconds: float = 0) -> None:
    """Write a task JSON. mtime_age_seconds > 0 backdates the file."""
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{task_id}.json"
    path.write_text(json.dumps({
        "id": task_id,
        "subject": f"task {task_id}",
        "status": status,
        "activeForm": f"Doing task {task_id}",
    }))
    if mtime_age_seconds > 0:
        ts = time.time() - mtime_age_seconds
        os.utime(path, (ts, ts))


@pytest.fixture
def fake_tasks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch sub_agent_monitor.TASKS_ROOT to point at a temp directory."""
    monkeypatch.setattr(sub_agent_monitor, "TASKS_ROOT", tmp_path)
    return tmp_path


def test_stale_in_progress_tasks_are_ignored(fake_tasks_root: Path) -> None:
    """Old in_progress files (mtime > 4 hours ago) must NOT count.

    These are the "5 sub-agents but nothing running" cards the user saw —
    leftovers from a previous Claude Code session that died before
    marking its tasks completed.
    """
    project_dir = fake_tasks_root / "aligned-tools"
    _write_task(project_dir, "100", "in_progress", mtime_age_seconds=24 * 3600)  # 1 day old
    _write_task(project_dir, "101", "in_progress", mtime_age_seconds=8 * 3600)   # 8 hours old

    sessions = {"sid-1": _FakeSession("sid-1", "aligned-tools")}
    counts = sub_agent_monitor.get_all_sub_agent_counts(sessions)
    assert counts["sid-1"] == (0, []), (
        f"Stale in_progress task files (>4h mtime) MUST be ignored. "
        f"Got {counts['sid-1'][0]} active sub-agents from stale files. "
        f"This is the 'phantom 5 sub-agents' bug class."
    )


def test_long_running_in_progress_task_still_counts(fake_tasks_root: Path) -> None:
    """A sub-agent running a slow tool call (e.g. 3h `npm install`, slow
    CI poll, LLM batch) can legitimately go quiet for hours. With the
    earlier 30-min ceiling it would silently drop from the count and the
    user would read it as 'finished'. The 4h ceiling covers this case.
    Regression guard against tightening the threshold back down."""
    project_dir = fake_tasks_root / "voxherd"
    _write_task(project_dir, "1", "in_progress", mtime_age_seconds=3 * 3600)  # 3h old

    sessions = {"sid-slow": _FakeSession("sid-slow", "voxherd")}
    counts = sub_agent_monitor.get_all_sub_agent_counts(sessions)
    assert counts["sid-slow"][0] == 1, (
        "A 3-hour-quiet in_progress task should still count — real tool "
        "calls (large installs, CI polls, external APIs) legitimately "
        "exceed 30 minutes of silence between task-file touches."
    )


def test_fresh_in_progress_task_counts(fake_tasks_root: Path) -> None:
    """Recently-updated in_progress files DO count — they're real live agents."""
    project_dir = fake_tasks_root / "aligned-tools"
    _write_task(project_dir, "1", "in_progress")  # mtime = now

    sessions = {"sid-1": _FakeSession("sid-1", "aligned-tools")}
    counts = sub_agent_monitor.get_all_sub_agent_counts(sessions)
    assert counts["sid-1"][0] == 1


def test_ambiguous_project_match_skips_attribution(fake_tasks_root: Path) -> None:
    """Project-name dir tasks must NOT double-attribute to multiple sessions.

    When two tmux sessions share a project (aligned-tools-0 +
    aligned-tools-1), the project-name task dir has no way to identify
    which session owns the tasks. Previously the bridge attributed the
    same tasks to BOTH sessions, producing phantom counts on every row.
    """
    project_dir = fake_tasks_root / "aligned-tools"
    _write_task(project_dir, "1", "in_progress")
    _write_task(project_dir, "2", "in_progress")

    # Two sessions, same project — the ambiguous case.
    sessions = {
        "sid-0": _FakeSession("sid-0", "aligned-tools"),
        "sid-1": _FakeSession("sid-1", "aligned-tools"),
    }
    counts = sub_agent_monitor.get_all_sub_agent_counts(sessions)
    assert counts["sid-0"] == (0, []), (
        f"Ambiguous project-name attribution: got {counts['sid-0'][0]} sub-agents "
        f"on sid-0 from project-shared task dir. Should be 0 — task ownership "
        f"cannot be determined when multiple sessions share the project."
    )
    assert counts["sid-1"] == (0, [])


def test_unique_project_match_still_attributes(fake_tasks_root: Path) -> None:
    """When only one session has a given project, project-name dir tasks
    attribute to it normally — that's the legacy fallback for older Claude
    Code versions that key tasks by project, not session UUID."""
    project_dir = fake_tasks_root / "voxherd"
    _write_task(project_dir, "1", "in_progress")

    sessions = {"sid-only": _FakeSession("sid-only", "voxherd")}
    counts = sub_agent_monitor.get_all_sub_agent_counts(sessions)
    assert counts["sid-only"][0] == 1, (
        "Single session for a project should still get its project-name-dir tasks. "
        "Dedup logic must only kick in when MULTIPLE sessions share a project."
    )


def test_uuid_dir_attributes_correctly_alongside_ambiguous_project(fake_tasks_root: Path) -> None:
    """A session's UUID-dir tasks attribute to that session even when a
    project-name dir would be ambiguous. UUID matches are never ambiguous."""
    # Two sessions for same project — project-name attribution ambiguous.
    sessions = {
        "uuid-0": _FakeSession("uuid-0", "aligned-tools"),
        "uuid-1": _FakeSession("uuid-1", "aligned-tools"),
    }
    # Task in UUID dir — unambiguous for uuid-0
    _write_task(fake_tasks_root / "uuid-0", "1", "in_progress")
    # Task in project-name dir — ambiguous, should be skipped
    _write_task(fake_tasks_root / "aligned-tools", "2", "in_progress")

    counts = sub_agent_monitor.get_all_sub_agent_counts(sessions)
    assert counts["uuid-0"][0] == 1, "UUID-dir task must attribute to its session"
    assert counts["uuid-1"][0] == 0, "Other session must NOT pick up the ambiguous project-dir task"


def test_pending_tasks_never_count(fake_tasks_root: Path) -> None:
    """Pending tasks are excluded — only in_progress counts as a live sub-agent."""
    project_dir = fake_tasks_root / "voxherd"
    _write_task(project_dir, "1", "pending")
    _write_task(project_dir, "2", "completed")
    _write_task(project_dir, "3", "in_progress")

    sessions = {"sid-1": _FakeSession("sid-1", "voxherd")}
    counts = sub_agent_monitor.get_all_sub_agent_counts(sessions)
    assert counts["sid-1"][0] == 1, "Only in_progress tasks should count as live sub-agents."
