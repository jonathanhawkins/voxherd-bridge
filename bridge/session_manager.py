"""Session state management for VoxHerd bridge server.

Sessions are kept in memory at runtime but persisted to a JSON file
(``~/.voxherd/sessions.json``) so they survive bridge restarts.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bridge.env_utils import get_subprocess_env
from bridge.assistant import normalize_assistant
from bridge.validation import (
    _sanitize_model,
    _validate_project_dir,
    _validate_tmux_pane_target,
)

_PERSIST_PATH = os.path.expanduser("~/.voxherd/sessions.json")


@dataclass
class Session:
    """A single coding-assistant session tracked by the bridge."""

    session_id: str
    project: str
    project_dir: str
    assistant: str = "claude"  # claude | codex | gemini
    # The model the assistant is running, e.g. "claude-opus-5-5". A separate
    # axis from `assistant`: one assistant runs many models (Antigravity is
    # multi-provider; Claude Code switches with /model). "" until known.
    model: str = ""
    status: str = "idle"
    last_summary: str = ""
    window_name: str = ""  # human display name (e.g. swarm role: planner, worker-1); overrides "project #N" in the UI
    registered_at: str = ""
    last_activity: str = ""
    tmux_target: str | None = None  # e.g. "voxherd-dev:0.0" — dispatch via send-keys
    quiet: bool = False  # VOXHERD_QUIET worker: never speak for this session (still visible on dashboard/lens)
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
    # ---- Lens-footer status state (transient; not persisted) -----------
    # Ring buffer of recent stream-json events captured from the
    # subprocess stdout of a ``claude --resume -p`` dispatch. The lens
    # footer extractor walks these to derive a live status line.
    # ``None`` until the first dispatch populates the buffer.
    _stream_json_buffer: "deque[dict] | None" = field(default=None, repr=False)
    # Reader task that pumps stream-json events into the buffer above.
    # Strong reference kept here so the asyncio GC can't reap the task
    # mid-stream. ``remove_session`` cancels it; on natural EOF the
    # task self-terminates inside ``_consume_stream_json``.
    _stream_json_task: "object | None" = field(default=None, repr=False)
    # Last status dict broadcast over WebSocket for this session — used
    # by the activity poll loop to suppress duplicate sends.
    _last_broadcast_status: dict | None = field(default=None, repr=False)
    # monotonic() timestamp when the session most recently transitioned
    # to ``active``. Used by ``session_status.derive_status`` to compute
    # the elapsed-seconds field that goes into the lens footer.
    _activity_started_at: float | None = field(default=None, repr=False)
    # Last context-window percent (0-100) successfully parsed from the
    # pane chrome. Held so a frame that lacks a parseable gauge (width
    # truncation, mid-redraw) reuses it instead of blanking the lens
    # header chip. See ``session_status.sticky_context_percent``.
    _last_context_pct: int | None = field(default=None, repr=False)
    # Id of the last edit broadcast as ``agent_focus`` (Follow mode), so the
    # activity poll announces each edit once. See ``bridge/focus.py``.
    _last_focus_id: str | None = field(default=None, repr=False)

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
            "model": self.model,
            "status": self.status,
            "last_summary": self.last_summary,
            "window_name": self.window_name,
            "registered_at": self.registered_at,
            "last_activity": self.last_activity,
            "tmux_target": self.tmux_target,
            "quiet": self.quiet,
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
                # Re-validated on the way back in, like every other field here.
                # A root that was accepted by an older build (or edited into the
                # file) is the Reader's root and the dispatch cwd, so restoring
                # it unchecked would outlive the fix that refused it.
                project_dir, dir_err = _validate_project_dir(d.get("project_dir", ""))
                if dir_err:
                    continue
                self._sessions[sid] = Session(
                    session_id=d["session_id"],
                    project=d["project"],
                    project_dir=project_dir,
                    assistant=normalize_assistant(d.get("assistant", "claude")),
                    model=_sanitize_model(d.get("model")),
                    status=d.get("status", "idle"),
                    last_summary=d.get("last_summary", ""),
                    window_name=d.get("window_name", ""),
                    registered_at=d.get("registered_at", ""),
                    last_activity=d.get("last_activity", ""),
                    tmux_target=tmux_target,
                    quiet=bool(d.get("quiet", False)),
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

    def _drop(self, session_id: str) -> "Session | None":
        """Remove *session_id* from the registry and forget everything keyed to it.

        THE removal primitive. Every other path goes through here, because the
        per-session bookkeeping lives in other modules and each caller forgetting
        it independently is a bug that has now been written five times:

        * four of the eight ``remove_session`` call sites never forgot the books;
        * ``prune_dead``'s three ``del``s bypassed the lot;
        * and ``register_session``'s tmux-dedup eviction -- the commonest way an
          id changes, since Claude Code restarting or compacting in the same pane
          re-registers it -- popped the session with a bare ``pop`` and left all
          nine books behind permanently. ``_LAST_CHOICE_FOR_SESSION`` is what
          ``server_state._state_sync_msg`` builds ``active_choice_sessions``
          from, and that set is documented as authoritative and is what clients
          prune stale decision cards against. The activity loop only visits
          registered sessions, so a dead id is never revisited: the lens kept a
          decision card for a session that no longer existed, for the life of
          the process.

        Also cancels the stream-json reader, so an evicted session does not leak
        a coroutine and a subprocess pipe.

        NOT included: ``_cancel_terminal_subs_for_session``. That is deliberately
        the caller's job -- it is async, it has to notify the subscriber so it can
        re-subscribe under the replacement id, and it releases the geometry lease.
        Three call sites do not do it; see the note in ``bridge/activity.py``.
        """
        from bridge.activity import _forget_session_state

        session = self._sessions.pop(session_id, None)
        _forget_session_state(session_id)
        reader = getattr(session, "_stream_json_task", None) if session else None
        if reader is not None and hasattr(reader, "cancel"):
            try:
                reader.cancel()
            except Exception:
                pass
        return session

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
                    self._drop(sid)

        # 2. Remove sessions without a tmux target (can't dispatch to them)
        #    or with a bridge: target (bogus — inherited from bridge tmux env).
        for sid in list(self._sessions):
            s = self._sessions.get(sid)
            if s is None:
                continue
            if not s.tmux_target or s.tmux_target.startswith("bridge:"):
                removed.append((sid, s.project))
                self._drop(sid)

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
                    self._drop(sid)

        if removed:
            # ``_drop`` already forgot each one's books.
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
        quiet: bool = False,
        model: str = "",
    ) -> tuple[Session | None, list[str]]:
        """Register a new session or re-register an existing one.

        Only deduplicates by tmux_target: if another session shares the same
        tmux pane, it's the same terminal and the old entry is replaced.
        Multiple sessions for the same project directory are allowed.

        Returns (session, removed_ids) where removed_ids lists session IDs
        that were evicted during deduplication. ``session`` is None when
        ``project_dir`` is refused.

        ``project_dir`` is validated HERE because this is the one point every
        producer shares, and they did not all check: the REST register, the
        ``/api/events`` auto-register, ``spawn_session`` and the persisted
        reload each called ``_validate_project_dir`` themselves, while tmux
        discovery took a pane's ``#{pane_current_path}`` verbatim — so an
        assistant started with ``tmux new-session -c /etc`` gave the Reader a
        root of ``/etc`` within 30 seconds, which is the confinement bypass the
        other call sites exist to prevent. Validating at the choke point means a
        sixth producer cannot reintroduce it.
        """
        removed: list[str] = []
        # Containment only: see _validate_project_dir. A caller that also
        # wants existence (the REST register) checks it itself.
        clean_dir, dir_err = _validate_project_dir(
            project_dir, require_exists=False
        )
        if dir_err:
            # Imported here, not at module scope: server_state imports this
            # module, so a top-level import would be a cycle.
            from bridge.server_state import log_event

            log_event(
                "warning", project or "bridge",
                f"Refused session {session_id[:12]}...: invalid project_dir: {dir_err}",
            )
            return None, removed
        project_dir = clean_dir
        assistant = normalize_assistant(assistant)
        model = _sanitize_model(model)
        # Same session id re-registering (a compact, a bridge restart): what
        # it was running is the best guess at what it is running.
        predecessors: list[Session] = [
            s for s in (self._sessions.get(session_id),) if s is not None
        ]

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
                # Through ``_drop``: this pop used to leave all nine per-session
                # books behind, and it is the path that fires whenever Claude
                # Code re-registers the same pane under a new id.
                evicted = self._drop(sid)
                if evicted is not None:
                    predecessors.append(evicted)

        # A registration that does not say which model -- an older hook, or a
        # SessionStart that leaves it out -- keeps the one this terminal was
        # already running, rather than blanking it until the next Stop. Only
        # from the same assistant: a pane that went from Claude to Codex did
        # not keep its model.
        if not model:
            model = next(
                (s.model for s in predecessors if s.model and s.assistant == assistant),
                "",
            )

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
            assistant=assistant,
            model=model,
            status="active",
            last_summary="",
            registered_at=now,
            last_activity=now,
            tmux_target=tmux_target,
            quiet=quiet,
            activity_type="registered",
            agent_number=agent_number,
            # Registration is the first active edge — anchor the
            # lens-footer elapsed clock here so footer counters start
            # from 0 the moment the session shows up.
            _activity_started_at=time.monotonic(),
        )
        self._sessions[session_id] = session
        self._save()
        return session, removed

    def set_model(self, session_id: str, model: str) -> bool:
        """Record the model a session is running. True when it changed.

        An unknown or unrecognisable reading never clears what is known: a
        transcript with no assistant turn yet says nothing about the model.
        """
        session = self._sessions.get(session_id)
        model = _sanitize_model(model)
        if session is None or not model or session.model == model:
            return False
        session.model = model
        self._save()
        return True

    def set_window_name(self, session_id: str, name: str) -> bool:
        s = self._sessions.get(session_id)
        if not s:
            return False
        s.window_name = name
        self._save()
        return True

    def update_status(self, session_id: str, status: str, summary: str | None = None, *, activity_type: str | None = None, stop_reason: str | None = None) -> Session | None:
        """Update session status and optionally its summary. Returns None if not found."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        prev_status = session.status
        session.status = status
        session.last_activity = datetime.now(timezone.utc).isoformat()
        if summary is not None:
            session.last_summary = summary
        if activity_type is not None:
            session.activity_type = activity_type
        if stop_reason is not None:
            session.stop_reason = stop_reason
        # Lens footer: anchor "elapsed_s" to the latest idle→active edge
        # so the footer counter resets every turn. Without this, a long-
        # running session would show ever-growing elapsed time even
        # between turns where Claude is sitting idle. Clear on going
        # idle so the activity poll doesn't re-broadcast a fresh
        # session_status every tick just because elapsed_s ticked up by
        # one (idle sessions have no meaningful "started X seconds ago"
        # anchor; that's a working-state concept).
        if status == "active" and prev_status != "active":
            session._activity_started_at = time.monotonic()
        elif status == "idle":
            session._activity_started_at = None
        if status == "idle":
            session.activity_snippet = ""
            # Two idle paths converge here:
            #   1. Activity poller auto-idle — caller passes no activity_type
            #      kwarg. The prior "testing"/"building"/etc. label is now
            #      stale; reset to "sleeping" so state_sync doesn't report
            #      it. (Bug fixed 2026-05-19: previously the field kept its
            #      stale label and iOS rendered "still testing" forever.)
            #   2. Stop-hook idle — caller passes an explicit label like
            #      "completed"/"errored"/"stopped" carrying semantic info
            #      about how the turn ended. Don't clobber it.
            # The `activity_type` parameter was already applied above (line
            # ~371) when non-None, so we only need to handle the None case.
            if activity_type is None:
                session.activity_type = "sleeping"
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
        (prefer status=="active", then by last_activity descending) -- but an
        EXACT name match always outranks a substring one.

        That last rule is the whole point. With pure containment, "voxherd"
        matched both ``voxherd`` and ``voxherd-notes``, and the tie-break was
        "active first, then most recent" -- so the longer, busier,
        name-shadowing session won, and a command meant for ``voxherd`` was
        typed into ``voxherd-notes``. Said out loud about a permission prompt,
        that is an approval delivered to the wrong agent.
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
        # Applied last so it wins over both: a name the speaker said exactly is
        # not a guess, and must not lose to a busier near-miss.
        matches.sort(key=lambda s: 0 if s.project.lower() == needle else 1)
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
        """Remove a session and everything keyed by its id, and persist.

        Returns True if the session existed, False otherwise. The removal and
        the bookkeeping are ``_drop``'s; this adds the save.

        Forgetting the books happens for an id that named no session too:
        nothing should hold state keyed to an id that is not a session. A stale
        ``_LAST_CHOICE_FOR_SESSION`` entry is not merely garbage -- it is what
        the choice-commit gate compares an answer against, and what
        ``state_sync``'s ``active_choice_sessions`` is built from.

        NOT done here: ``_cancel_terminal_subs_for_session``. It is async, it
        has to tell the subscriber so it can re-subscribe under the replacement
        id, and it is what releases the geometry lease -- so it stays the
        caller's, and three callers still skip it (pre-existing; see the note in
        ``bridge/activity.py``). Removing a session is therefore not by itself
        total teardown.
        """
        session = self._drop(session_id)
        if session is None:
            return False
        self._save()
        return True

    def to_dict(self) -> dict:
        """Serializable snapshot of all sessions, suitable for WebSocket state_sync."""
        return {sid: s.to_dict() for sid, s in self._sessions.items()}
