"""Read and write Claude Code task files from ~/.claude/tasks/.

Each task list is a directory at ``~/.claude/tasks/{list_id}/`` containing
individual ``{task_id}.json`` files and a ``.highwatermark`` file that tracks
the next available numeric ID.

Functions are synchronous — local file I/O is fast enough to call from
async FastAPI handlers without blocking the event loop noticeably.
"""

from __future__ import annotations

import json
import os
import os.path
from pathlib import Path

TASKS_ROOT = Path.home() / ".claude" / "tasks"


def _sanitize_id(value: str) -> str:
    """Sanitize a task list ID or task ID to prevent path traversal.

    Strips path separators and parent-directory references.
    Raises ValueError if the result is empty.
    """
    # Remove any path components — only allow the basename
    clean = os.path.basename(value)
    # Reject parent-dir references and empty strings
    if not clean or clean in (".", ".."):
        raise ValueError(f"Invalid ID: {value!r}")
    return clean


def _task_dir(task_list_id: str) -> Path:
    return TASKS_ROOT / _sanitize_id(task_list_id)


def _read_highwatermark(task_dir: Path) -> int:
    """Read the .highwatermark file and return the stored value."""
    hwm_path = task_dir / ".highwatermark"
    if hwm_path.exists():
        try:
            return int(hwm_path.read_text().strip())
        except (ValueError, OSError):
            pass
    return 0


def _write_highwatermark(task_dir: Path, value: int) -> None:
    (task_dir / ".highwatermark").write_text(str(value))


def _next_id(task_dir: Path) -> str:
    """Determine the next task ID by scanning existing files and highwatermark."""
    hwm = _read_highwatermark(task_dir)
    # Also scan for the max numeric ID among existing files, in case the
    # highwatermark is stale (e.g. tasks created by another process).
    max_file_id = 0
    for f in task_dir.glob("*.json"):
        try:
            max_file_id = max(max_file_id, int(f.stem))
        except ValueError:
            pass
    next_val = max(hwm, max_file_id) + 1
    return str(next_val)


def resolve_task_list_id(project: str) -> str | None:
    """Map a project name to a task list directory.

    Tries exact match, then case-insensitive match against directory names
    in ~/.claude/tasks/.
    """
    if not TASKS_ROOT.exists():
        return None

    target = _task_dir(project)
    if target.is_dir():
        return project

    # Case-insensitive fallback
    project_lower = project.lower()
    for entry in TASKS_ROOT.iterdir():
        if entry.is_dir() and entry.name.lower() == project_lower:
            return entry.name

    return None


def list_tasks(task_list_id: str) -> list[dict]:
    """Return all tasks in a task list, sorted by numeric ID."""
    task_dir = _task_dir(task_list_id)
    if not task_dir.is_dir():
        return []

    tasks = []
    for f in task_dir.glob("*.json"):
        try:
            task = json.loads(f.read_text())
            tasks.append(task)
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by numeric ID when possible, fall back to string sort.
    def sort_key(t: dict) -> int:
        try:
            return int(t.get("id", "0"))
        except ValueError:
            return 0

    tasks.sort(key=sort_key)
    return tasks


def get_task(task_list_id: str, task_id: str) -> dict | None:
    """Read a single task by ID."""
    path = _task_dir(task_list_id) / f"{_sanitize_id(task_id)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def create_task(task_list_id: str, subject: str, description: str = "",
                active_form: str = "") -> dict:
    """Create a new task and return it. Creates the task list dir if needed."""
    task_dir = _task_dir(task_list_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    task_id = _next_id(task_dir)

    task = {
        "id": task_id,
        "subject": subject,
        "description": description,
        "activeForm": active_form or subject,
        "status": "pending",
        "blocks": [],
        "blockedBy": [],
    }

    (task_dir / f"{task_id}.json").write_text(json.dumps(task, indent=2))
    _write_highwatermark(task_dir, int(task_id))
    return task


def update_task(task_list_id: str, task_id: str, **updates: str) -> dict | None:
    """Update fields on an existing task. Returns the updated task or None."""
    path = _task_dir(task_list_id) / f"{_sanitize_id(task_id)}.json"
    if not path.exists():
        return None

    try:
        task = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    allowed_fields = {"status", "subject", "description", "activeForm"}
    for key, value in updates.items():
        if key in allowed_fields:
            task[key] = value

    path.write_text(json.dumps(task, indent=2))
    return task
