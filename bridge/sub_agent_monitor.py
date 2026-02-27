"""Monitor Claude Code sub-agent activity via task files.

Sub-agents spawned by the Task tool do NOT fire separate SessionStart/Stop
hooks. They run within the parent session and create task files at
``~/.claude/tasks/{task_list_id}/``.

Task list IDs come in two flavors:
  - UUID-based: matches the session_id of the parent Claude Code session
  - Name-based: matches the project name (e.g. "quantcoach", "voxherd")

Each task file is a JSON object with at minimum:
  {"id": "1", "subject": "...", "status": "pending|in_progress|completed", ...}

This module scans those directories and returns sub-agent counts per session.
"""

import json
import os
from pathlib import Path

TASKS_ROOT = Path.home() / ".claude" / "tasks"


def _read_task_file(path: Path) -> dict | None:
    """Read a single task JSON file. Returns None on any error."""
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _scan_task_dir(task_dir: Path) -> list[dict]:
    """Read all task files in a directory and return their data."""
    if not task_dir.is_dir():
        return []
    tasks: list[dict] = []
    for f in task_dir.glob("*.json"):
        task = _read_task_file(f)
        if task and isinstance(task, dict) and "status" in task:
            tasks.append(task)
    return tasks


def get_sub_agent_info(session_id: str, project: str) -> tuple[int, list[dict]]:
    """Return (active_count, active_tasks) for a session's sub-agents.

    Checks both UUID-based (session_id) and name-based (project) task
    list directories. Deduplicates by task id if both exist.

    Returns:
        active_count: number of in_progress tasks (active sub-agents)
        active_tasks: list of dicts with {id, subject, status} for
                     non-completed tasks (pending + in_progress)
    """
    if not TASKS_ROOT.is_dir():
        return 0, []

    all_tasks: dict[str, dict] = {}  # keyed by task id to deduplicate

    # Check UUID-based task list (session-scoped)
    uuid_dir = TASKS_ROOT / session_id
    for task in _scan_task_dir(uuid_dir):
        tid = task.get("id", "")
        if tid:
            all_tasks[f"uuid:{tid}"] = task

    # Check name-based task list (project-scoped)
    # Try exact match first, then case-insensitive
    project_lower = project.lower()
    try:
        entries = list(TASKS_ROOT.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name == project or entry.name.lower() == project_lower:
            for task in _scan_task_dir(entry):
                tid = task.get("id", "")
                if tid:
                    all_tasks[f"name:{tid}"] = task

    # Count only in_progress tasks (active sub-agents).
    # Pending tasks are excluded because they may be stale leftovers from
    # previous sessions that share the same project name — showing them
    # clutters the visualization with dots that will never become active.
    active_count = 0
    active_tasks: list[dict] = []

    for task in all_tasks.values():
        status = task.get("status", "")
        if status == "in_progress":
            active_count += 1
            active_tasks.append({
                "id": task.get("id", ""),
                "subject": task.get("subject", ""),
                "status": status,
                "activeForm": task.get("activeForm", ""),
            })

    active_tasks.sort(key=lambda t: t.get("id", ""))

    return active_count, active_tasks


def get_all_sub_agent_counts(sessions: dict[str, "Session"]) -> dict[str, tuple[int, list[dict]]]:
    """Batch-scan sub-agent counts for all sessions.

    More efficient than calling get_sub_agent_info per session because
    it reads each task directory only once.

    Args:
        sessions: dict of session_id -> Session objects

    Returns:
        dict of session_id -> (active_count, active_tasks)
    """
    if not TASKS_ROOT.is_dir():
        return {}

    # Build lookup maps
    by_session_id: dict[str, str] = {}  # session_id -> session_id (identity)
    by_project: dict[str, list[str]] = {}  # project_lower -> [session_ids]

    for sid, session in sessions.items():
        by_session_id[sid] = sid
        proj = session.project.lower()
        by_project.setdefault(proj, []).append(sid)

    result: dict[str, tuple[int, list[dict]]] = {}

    # Initialize all sessions with zero counts
    for sid in sessions:
        result[sid] = (0, [])

    # Scan all task directories
    try:
        entries = list(TASKS_ROOT.iterdir())
    except OSError:
        return result

    for entry in entries:
        if not entry.is_dir():
            continue

        tasks = _scan_task_dir(entry)
        if not tasks:
            continue

        # Determine which session(s) this task list belongs to
        target_sids: list[str] = []

        # UUID match
        if entry.name in by_session_id:
            target_sids.append(entry.name)

        # Project name match
        entry_lower = entry.name.lower()
        if entry_lower in by_project:
            for sid in by_project[entry_lower]:
                if sid not in target_sids:
                    target_sids.append(sid)

        if not target_sids:
            continue

        # Count only in_progress tasks — pending tasks are excluded to avoid
        # showing stale leftovers from previous sessions.
        active_count = sum(1 for t in tasks if t.get("status") == "in_progress")
        active_tasks: list[dict] = []
        for t in tasks:
            status = t.get("status", "")
            if status == "in_progress":
                active_tasks.append({
                    "id": t.get("id", ""),
                    "subject": t.get("subject", ""),
                    "status": status,
                    "activeForm": t.get("activeForm", ""),
                })

        active_tasks.sort(key=lambda t: t.get("id", ""))

        # Assign to all matching sessions
        for sid in target_sids:
            existing_count, existing_tasks = result.get(sid, (0, []))
            # Merge task lists, deduplicating by id
            merged_ids = {t["id"] for t in existing_tasks}
            merged_tasks = list(existing_tasks)
            for t in active_tasks:
                if t["id"] not in merged_ids:
                    merged_tasks.append(t)
                    merged_ids.add(t["id"])
            # Recount in_progress from merged list to get accurate total
            merged_count = sum(1 for t in merged_tasks if t["status"] == "in_progress")
            result[sid] = (merged_count, merged_tasks)

    return result
