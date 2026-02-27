"""Session state management for VoxHerd bridge server.

Sessions are kept in memory at runtime but persisted to a JSON file
(``~/.voxherd/sessions.json``) so they survive bridge restarts.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from bridge.env_utils import get_subprocess_env
from bridge.assistant import normalize_assistant
from bridge.validation import _validate_tmux_pane_target

_PERSIST_PATH = os.path.expanduser("~/.voxherd/sessions.json")


@dataclass
class Session:
    """A single coding-assistant session tracked by the bridge."""

    session_id: str
    project: str
    project_dir: str
    assistant: str = "claude"  # claude | codex | gemini
    status: str = "idle"
    last_summary: str = ""
    registered_at: str = ""
    last_activity: str = ""
    tmux_target: str | None = None  # e.g. "voxherd-dev:0.0" — dispatch via send-keys
    voice_dispatched: bool = False  # DEPRECATED: replaced by _pending_dispatch_count
    _pending_dispatch_count: int = 0  # number of in-flight voice dispatches; suppress listen until 0
    tmux_check_failures: int = 0  # consecutive tmux pane-check failures; remove after 3
    idle_since: float = 0.0  # monotonic timestamp when session last went idle; used for re-activation cooldown
    activity_snippet: str = ""  # last non-empty terminal line for live card preview
    terminal_preview: str = ""  # last 8 non-chrome terminal lines for hover tooltip
    activity_type: str = "sleeping"  # emoji status: thinking/writing/searching/running/building/testing/working/completed/sleeping/errored/stopped/approval/input/registered/dead
    stop_reason: str = ""  # why session stopped: end_turn, error, interrupted, etc.
    agent_number: int = 1  # ordinal within a project (1, 2, 3...) for multi-agent per project
    sub_agent_count: int = 0  # number of in-progress sub-agents (Task tool) for this session
    sub_agent_tasks: list[dict] | None = None  # summary of active sub-agent tasks [{id, subject, status}]
    queued_command: str | None = None  # voice command waiting for session to go idle
    # Hook-tracked sub-agents (SubagentStart/SubagentStop hooks)
    # Maps agent_id -> {agent_type, started_at}
    _live_subagents: dict[str, dict] | None = None

    @property
    def live_subagents(self) -> dict[str, dict]:
        if self._live_subagents is None:
            self._live_subagents = {}
        return self._live_subagents

    def add_subagent(self, agent_id: str, agent_type: str, timestamp: str) -> None:
        """Track a newly spawned sub-agent (from SubagentStart hook)."""
        self.live_subagents[agent_id] = {
            "agent_type": agent_type,
            "started_at": timestamp,
        }
        self._update_sub_agent_count()

    def remove_subagent(self, agent_id: str) -> None:
        """Remove a finished sub-agent (from SubagentStop hook)."""
        self.live_subagents.pop(agent_id, None)
        self._update_sub_agent_count()

    def _update_sub_agent_count(self) -> None:
        """Sync sub_agent_count and sub_agent_tasks from live_subagents."""
        self.sub_agent_count = len(self.live_subagents)
        if self.live_subagents:
            self.sub_agent_tasks = [
                {
                    "id": aid,
                    "subject": info["agent_type"],
                    "status": "in_progress",
                    "activeForm": f"Running {info['agent_type']}",
                    "agent_type": info["agent_type"],
                }
                for aid, info in self.live_subagents.items()
            ]
        else:
            self.sub_agent_tasks = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "project_dir": self.project_dir,
            "assistant": self.assistant,
            "status": self.status,
            "last_summary": self.last_summary,
            "registered_at": self.registered_at,
            "last_activity": self.last_activity,
            "tmux_target": self.tmux_target,
            "activity_snippet": self.activity_snippet,
            "terminal_preview": self.terminal_preview,
            "activity_type": self.activity_type,
            "stop_reason": self.stop_reason,
            "agent_number": self.agent_number,
            "sub_agent_count": self.sub_agent_count,
            "sub_agent_tasks": self.sub_agent_tasks or [],
        }


class SessionManager:
    """Manages session state for all registered assistant instances.

    On every mutation (register, update, remove), the full state is flushed
    to ``~/.voxherd/sessions.json``.  On init, any previously saved
    sessions are loaded back.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self.active_project: str | None = None
        self.last_announced_project: str | None = None
        self.last_announced_session_id: str | None = None
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load sessions from disk if the file exists."""
        if not os.path.isfile(_PERSIST_PATH):
            return
        try:
            with open(_PERSIST_PATH, "r") as f:
                data = json.load(f)
            for sid, d in data.get("sessions", {}).items():
                tmux_target, _ = _validate_tmux_pane_target(d.get("tmux_target"))
                self._sessions[sid] = Session(
                    session_id=d["session_id"],
                    project=d["project"],
                    project_dir=d["project_dir"],
                    assistant=normalize_assistant(d.get("assistant", "claude")),
                    status=d.get("status", "idle"),
                    last_summary=d.get("last_summary", ""),
                    registered_at=d.get("registered_at", ""),
                    last_activity=d.get("last_activity", ""),
                    tmux_target=tmux_target,
                    activity_type=d.get("activity_type", "sleeping"),
                    stop_reason=d.get("stop_reason", ""),
                    agent_number=d.get("agent_number", 1),
                    # sub_agent_count is transient — always starts at 0 on load
                    # (will be populated by task file monitoring)
                )
            self.active_project = data.get("active_project")
            self.last_announced_project = data.get("last_announced_project")
            self.last_announced_session_id = data.get("last_announced_session_id")
        except Exception:
            # Corrupt file — start fresh
            self._sessions.clear()

    def _save(self) -> None:
        """Flush current state to disk in a background thread to avoid
        blocking the event loop. Falls back to sync write if no loop is
        running (e.g. during __init__).
        """
        os.makedirs(os.path.dirname(_PERSIST_PATH), exist_ok=True)
        data = {
            "sessions": {sid: s.to_dict() for sid, s in self._sessions.items()},
            "active_project": self.active_project,
            "last_announced_project": self.last_announced_project,
            "last_announced_session_id": self.last_announced_session_id,
        }
        try:
            loop = asyncio.get_running_loop()
            # Schedule the write in a thread so it doesn't block the event loop.
            # _save() is always called from the event loop thread, so create_task is safe.
            loop.create_task(asyncio.to_thread(self._write_json, data))
        except RuntimeError:
            # No event loop running (e.g. during __init__) — write synchronously
            self._write_json(data)

    @staticmethod
    def _write_json(data: dict) -> None:
        """Write JSON atomically. Called from a thread to avoid blocking."""
        import tempfile
        dir_path = os.path.dirname(_PERSIST_PATH)
        fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".sessions-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, _PERSIST_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    @staticmethod
    async def _tmux_pane_exists(target: str) -> bool:
        """Check if a tmux target pane is alive.

        Uses ``list-panes`` which correctly errors on nonexistent targets.
        (``display-message -t`` silently falls back to the current session.)
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "list-panes", "-t", target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=get_subprocess_env(),
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                return False
            return proc.returncode == 0
        except Exception:
            return False

    async def prune_dead(self) -> list[tuple[str, str]]:
        """Remove sessions whose tmux pane no longer exists.

        Also deduplicates: if multiple sessions share the same tmux_target,
        keep only the one with the most recent last_activity.

        Called automatically on startup and can be called periodically.
        Returns list of (session_id, project) tuples for removed sessions.
        """
        removed: list[tuple[str, str]] = []

        # 1. Remove sessions with dead tmux targets.
        # Snapshot keys before iterating — await points let other coroutines
        # modify _sessions, so check membership before deleting.
        for sid in list(self._sessions):
            s = self._sessions.get(sid)
            if s is None:
                continue  # removed by another coroutine during await
            if s.tmux_target and not await self._tmux_pane_exists(s.tmux_target):
                if sid in self._sessions:
                    removed.append((sid, s.project))
                    del self._sessions[sid]

        # 2. Remove sessions without a tmux target (can't dispatch to them)
        #    or with a bridge: target (bogus — inherited from bridge tmux env).
        for sid in list(self._sessions):
            s = self._sessions.get(sid)
            if s is None:
                continue
            if not s.tmux_target or s.tmux_target.startswith("bridge:"):
                removed.append((sid, s.project))
                del self._sessions[sid]

        # 3. Deduplicate by tmux_target — keep only the newest per pane.
        # (Project-level dedup removed: register_session() already handles
        # tmux_target dedup, and pruning by project was orphaning terminal
        # subscriptions keyed on the old session_id.)
        by_target: dict[str, list[str]] = {}
        for sid, s in list(self._sessions.items()):
            if s.tmux_target:
                by_target.setdefault(s.tmux_target, []).append(sid)
        for target, sids in by_target.items():
            if len(sids) <= 1:
                continue
            # Filter to only sids still in _sessions
            sids = [sid for sid in sids if sid in self._sessions]
            if len(sids) <= 1:
                continue
            sids.sort(key=lambda sid: self._sessions[sid].last_activity, reverse=True)
            for sid in sids[1:]:
                if sid in self._sessions:
                    print(f"[prune] Dedup: removing tmux {target} session {sid[:12]}... (keeping {sids[0][:12]}...)")
                    removed.append((sid, self._sessions[sid].project))
                    del self._sessions[sid]

        if removed:
            self._save()
        return removed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_active_project(self, project: str | None) -> None:
        """Set the currently active (default) project for bare commands."""
        self.active_project = project
        self._save()

    def set_last_announced(self, project: str, session_id: str | None = None) -> None:
        """Track the most recently announced project and session (for react-to-announcement routing)."""
        self.last_announced_project = project
        self.last_announced_session_id = session_id
        self._save()

    def register_session(
        self,
        session_id: str,
        project: str,
        project_dir: str,
        *,
        tmux_target: str | None = None,
        assistant: str = "claude",
    ) -> tuple[Session, list[str]]:
        """Register a new session or re-register an existing one.

        Only deduplicates by tmux_target: if another session shares the same
        tmux pane, it's the same terminal and the old entry is replaced.
        Multiple sessions for the same project directory are allowed.

        Returns (session, removed_ids) where removed_ids lists session IDs
        that were evicted during deduplication.
        """
        removed: list[str] = []

        # Reject the bridge's own pane as a tmux_target — this means the hook
        # detected the wrong pane (Claude Code was dispatched from the bridge session
        # and inherited its $TMUX env).  Clear it so the activity poll doesn't read
        # the bridge terminal (which contains log output from ALL projects).
        if tmux_target and tmux_target.startswith("bridge:"):
            tmux_target = None
        tmux_target, _ = _validate_tmux_pane_target(tmux_target)

        # Remove stale sessions that share the same tmux pane (re-registration).
        if tmux_target:
            stale = [
                sid for sid, s in self._sessions.items()
                if sid != session_id and s.tmux_target == tmux_target
            ]
            for sid in stale:
                removed.append(sid)
                del self._sessions[sid]

        # Compute agent_number: max of existing same-project sessions + 1
        same_project = [
            s.agent_number for s in self._sessions.values()
            if s.project.lower() == project.lower()
        ]
        agent_number = max(same_project, default=0) + 1

        now = datetime.now(timezone.utc).isoformat()
        session = Session(
            session_id=session_id,
            project=project,
            project_dir=project_dir,
            assistant=normalize_assistant(assistant),
            status="active",
            last_summary="",
            registered_at=now,
            last_activity=now,
            tmux_target=tmux_target,
            activity_type="registered",
            agent_number=agent_number,
        )
        self._sessions[session_id] = session
        self._save()
        return session, removed

    def update_status(self, session_id: str, status: str, summary: str | None = None, *, activity_type: str | None = None, stop_reason: str | None = None) -> Session | None:
        """Update session status and optionally its summary. Returns None if not found."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.status = status
        session.last_activity = datetime.now(timezone.utc).isoformat()
        if summary is not None:
            session.last_summary = summary
        if activity_type is not None:
            session.activity_type = activity_type
        if stop_reason is not None:
            session.stop_reason = stop_reason
        if status == "idle":
            session.activity_snippet = ""
            session.idle_since = time.monotonic()
            # Clear sub-agent tracking — if session was interrupted,
            # SubagentStop hooks may not have fired.
            if session._live_subagents:
                session._live_subagents.clear()
            session.sub_agent_count = 0
            session.sub_agent_tasks = None
        self._save()
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Look up a session by its exact id."""
        return self._sessions.get(session_id)

    def get_all_sessions(self) -> dict[str, Session]:
        """Return a shallow copy of the sessions dict.

        Callers get a snapshot safe to iterate while the original may be
        mutated from another coroutine or thread (e.g. prune_dead).
        """
        return dict(self._sessions)

    def get_sessions_by_project(self, project_name: str, *, assistant: str | None = None) -> list[Session]:
        """Fuzzy-match all sessions for a project name.

        Matching is case-insensitive substring containment. When ``assistant``
        is provided, results are filtered to that assistant id.

        Returns all matches, sorted by most recently active first
        (prefer status=="active", then by last_activity descending).
        """
        needle = project_name.lower()
        assistant_filter = (assistant or "").strip().lower()
        matches = [
            s
            for s in self._sessions.values()
            if needle in s.project.lower()
            and (not assistant_filter or s.assistant == assistant_filter)
        ]
        # Sort: active sessions first, then by last_activity descending.
        # Using two stable sorts: first by time desc, then by status (active=0 first).
        matches.sort(key=lambda s: s.last_activity, reverse=True)
        matches.sort(key=lambda s: 0 if s.status == "active" else 1)
        return matches

    def get_session_by_project(
        self, project_name: str, *, assistant: str | None = None
    ) -> Session | None:
        """Fuzzy-match a session by project name.

        Returns the most recently active match: prefers status=="active",
        then by most recent last_activity. When ``assistant`` is provided,
        only sessions for that assistant are considered.
        """
        matches = self.get_sessions_by_project(project_name, assistant=assistant)
        return matches[0] if matches else None

    def get_session_by_project_and_number(
        self, project_name: str, agent_number: int, *, assistant: str | None = None
    ) -> Session | None:
        """Look up a specific agent by project name + agent number.

        When ``assistant`` is provided, only sessions for that assistant are
        considered.
        """
        needle = project_name.lower()
        assistant_filter = (assistant or "").strip().lower()
        for session in self._sessions.values():
            if needle not in session.project.lower():
                continue
            if session.agent_number != agent_number:
                continue
            if assistant_filter and session.assistant != assistant_filter:
                continue
            return session
        return None

    def remove_session(self, session_id: str) -> bool:
        """Remove a session. Returns True if it existed, False otherwise."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            self._save()
            return True
        return False

    def to_dict(self) -> dict:
        """Serializable snapshot of all sessions, suitable for WebSocket state_sync."""
        return {sid: s.to_dict() for sid, s in self._sessions.items()}
