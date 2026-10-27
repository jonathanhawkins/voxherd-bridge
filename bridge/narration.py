"""Narration engine — decides when and how to announce events via TTS.

Sits between event sources (routes.py stop/notification events, activity.py
state transitions) and output sinks (mac_tts, broadcast_to_ios).  Replaces
the direct ``mac_tts.speak()`` calls scattered across routes.py.

Key features:
- Batches rapid completions within a 5-second window
- Per-project cooldown (20s) to avoid being annoying
- Three verbosity levels: quiet, normal, verbose
- Errors and approvals always bypass cooldown
- Stalls (verbose only): a working session with no new activity for 60s
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verbosity(Enum):
    QUIET = "quiet"       # errors + approvals only
    NORMAL = "normal"     # completions + errors + approvals  (default)
    VERBOSE = "verbose"   # all transitions including stalls


@dataclass
class NarrationEvent:
    event_type: str          # "completed" | "errored" | "stalled" | "approval" | "batch_completed"
    project: str
    session_id: str
    summary: str             # raw summary text
    tts_text: str            # pre-formatted for TTS
    agent_number: int = 1
    batch_count: int = 1     # >1 for batched completions
    listen_after: bool = False


@dataclass
class _PendingCompletion:
    project: str
    session_id: str
    summary: str
    agent_number: int
    listen_after: bool
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class _ProjectState:
    last_narration_time: float = 0.0


@dataclass
class _StallClock:
    """How long one working session has gone without showing progress."""
    turn: float | None   # the session's _activity_started_at: a new turn is a new clock
    since: float         # the last progress seen: the turn starting, or an activity change
    warned: bool = False


class NarrationEngine:
    """Event-driven narration with batching, cooldowns, and verbosity control."""

    def __init__(
        self,
        tts: Any,
        broadcast_fn: Callable[[dict], Coroutine],
        log_fn: Callable[[str, str, str], None],
        sessions: Any,
        stt: Any = None,
        listen_after_enabled: bool = False,
        floor_held: Callable[[], bool] = lambda: False,
    ):
        self.tts = tts
        self.broadcast_fn = broadcast_fn
        self.log_fn = log_fn
        self.sessions = sessions
        self.stt = stt
        self.listen_after_enabled = listen_after_enabled
        # A headset holding the speech floor says it all itself (see _emit).
        self.floor_held = floor_held

        self.verbosity = Verbosity.NORMAL
        self._cooldown = 20.0        # seconds between narrations per project
        self._batch_window = 5.0     # seconds to coalesce rapid completions
        self._stall_threshold = 60.0 # seconds of no activity = stalled

        self._project_state: dict[str, _ProjectState] = {}
        self._stall_clocks: dict[str, _StallClock] = {}  # by session id
        self._pending_completions: list[_PendingCompletion] = []
        self._batch_timer: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def on_stop(
        self,
        project: str,
        session_id: str,
        summary: str,
        stop_reason: str,
        agent_number: int = 1,
        listen_after: bool = False,
    ) -> None:
        """Handle a stop event from a Claude Code session."""
        ps = self._get_project_state(project)

        # Errors emit immediately, bypass cooldown
        if stop_reason == "error":
            tts_text = f"{self._tts_prefix(project, session_id, agent_number)} hit an error. {summary}"
            await self._emit(NarrationEvent(
                event_type="errored",
                project=project,
                session_id=session_id,
                summary=summary,
                tts_text=tts_text,
                agent_number=agent_number,
                listen_after=listen_after,
            ))
            return

        # Skip completions in QUIET mode
        if self.verbosity == Verbosity.QUIET:
            return

        # Add to pending completions and start/reset batch timer
        self._pending_completions.append(_PendingCompletion(
            project=project,
            session_id=session_id,
            summary=summary,
            agent_number=agent_number,
            listen_after=listen_after,
        ))

        # Cancel existing timer and start a new one
        if self._batch_timer is not None:
            self._batch_timer.cancel()

        loop = asyncio.get_event_loop()
        self._batch_timer = loop.call_later(
            self._batch_window,
            lambda: asyncio.ensure_future(self._flush_batch()),
        )

    async def on_notification(
        self,
        project: str,
        session_id: str,
        message: str,
        agent_number: int = 1,
    ) -> None:
        """Handle a notification (approval request). Always emits immediately."""
        tts_text = f"{self._tts_prefix(project, session_id, agent_number)} needs approval. {message}"
        listen_after = self.stt is not None and getattr(self.stt, 'enabled', False) and self.listen_after_enabled
        await self._emit(NarrationEvent(
            event_type="approval",
            project=project,
            session_id=session_id,
            summary=message,
            tts_text=tts_text,
            agent_number=agent_number,
            listen_after=listen_after,
        ))

    async def on_activity_change(
        self,
        session_id: str,
        project: str,
        old_type: str,
        new_type: str,
        now: float | None = None,
    ) -> None:
        """A session's activity changed. That is progress: its stall clock
        starts over, and a stall can be announced again."""
        clock = self._stall_clocks.get(session_id)
        if clock is not None:
            clock.since = time.monotonic() if now is None else now
            clock.warned = False

    async def check_stalls(self, now: float | None = None) -> None:
        """Announce, once, each working session that has shown no progress for
        ``_stall_threshold`` seconds. Only in VERBOSE mode.

        Called after every activity poll, which is what reports progress
        (``on_activity_change``). A clock is per session and per turn: a new
        command never inherits the silence of the last one, and one agent's
        progress never vouches for another on the same project. Only sessions
        with a tmux pane are watched -- nothing else is polled, so their
        silence means nothing.

        The clocks run whatever the verbosity, so switching to VERBOSE hears
        at once about a session that has been stuck for a while.
        """
        now = time.monotonic() if now is None else now
        working = {
            sid: s for sid, s in self.sessions.get_all_sessions().items()
            if s.status == "active" and s.tmux_target
        }
        # Stopped, waiting on the user, or gone: nothing to stall on, so no
        # clock to keep. (Coming back is a new turn, and a new clock, anyway.)
        for sid in self._stall_clocks.keys() - working.keys():
            del self._stall_clocks[sid]

        for sid, session in working.items():
            clock = self._stall_clocks.get(sid)
            if clock is None or clock.turn != session._activity_started_at:
                self._stall_clocks[sid] = _StallClock(turn=session._activity_started_at, since=now)
                continue
            if clock.warned or now - clock.since < self._stall_threshold:
                continue
            if self.verbosity != Verbosity.VERBOSE or session.quiet:
                continue
            # The project's cooldown defers a stall rather than dropping it:
            # the stall is still there at a later check.
            if now - self._get_project_state(session.project).last_narration_time < self._cooldown:
                continue
            clock.warned = True
            prefix = self._tts_prefix(session.project, sid, session.agent_number)
            await self._emit(NarrationEvent(
                event_type="stalled",
                project=session.project,
                session_id=sid,
                summary="No activity detected",
                tts_text=f"{prefix} appears stalled.",
                agent_number=session.agent_number,
            ))

    def set_verbosity(self, level: Verbosity) -> None:
        """Change the narration verbosity level."""
        self.verbosity = level

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_project_state(self, project: str) -> _ProjectState:
        if project not in self._project_state:
            self._project_state[project] = _ProjectState()
        return self._project_state[project]

    def _tts_prefix(self, project: str, session_id: str, agent_number: int) -> str:
        """Return TTS prefix like 'agent 2 on voxherd' if multiple agents, else just project."""
        same_project = self.sessions.get_sessions_by_project(project)
        if len(same_project) > 1:
            return f"agent {agent_number} on {project}"
        return project

    @staticmethod
    def _starts_with_past_verb(text: str) -> bool:
        """Check if text starts with a past-tense verb (self-describing summary)."""
        past_verbs = [
            "Fixed", "Updated", "Added", "Removed", "Refactored", "Implemented",
            "Configured", "Deployed", "Created", "Resolved", "Applied", "Installed",
            "Built", "Changed", "Moved", "Renamed", "Merged", "Set", "Enabled",
            "Disabled", "Cleaned", "Optimized", "Simplified", "Replaced",
            "Wrote", "Deleted", "Restored", "Upgraded", "Downgraded", "Converted",
            "Migrated", "Reverted", "Wrapped", "Extracted", "Formatted", "Debugged",
            "Tested", "Verified", "Confirmed", "Connected", "Started", "Stopped",
            "Initialized", "Published", "Documented", "Pushed", "Committed",
            "Patched", "Rewrote", "Restructured",
        ]
        return any(text.startswith(v) for v in past_verbs)

    async def _flush_batch(self) -> None:
        """Flush all pending completions as individual or batched narrations."""
        self._batch_timer = None
        if not self._pending_completions:
            return

        pending = self._pending_completions[:]
        self._pending_completions.clear()

        now = time.monotonic()

        # Group by project
        by_project: dict[str, list[_PendingCompletion]] = {}
        for p in pending:
            by_project.setdefault(p.project, []).append(p)

        # Determine if this is a multi-project batch
        projects_involved = list(by_project.keys())

        if len(projects_involved) == 1:
            # Single project
            project = projects_involved[0]
            completions = by_project[project]
            ps = self._get_project_state(project)

            # Cooldown check — defer if too recent (but still announce)
            in_cooldown = (now - ps.last_narration_time) < self._cooldown
            if in_cooldown and len(completions) == 1:
                # Single completion within cooldown — skip narration
                self.log_fn("info", project, f"Narration cooldown: skipping '{completions[0].summary[:40]}'")
                return

            if len(completions) == 1:
                c = completions[0]
                prefix = self._tts_prefix(c.project, c.session_id, c.agent_number)
                # If summary starts with a past-tense verb, it's self-describing
                # — just speak it directly (no "finished" needed).
                if self._starts_with_past_verb(c.summary):
                    tts_text = c.summary
                else:
                    tts_text = f"{prefix}. {c.summary}"
                await self._emit(NarrationEvent(
                    event_type="completed",
                    project=c.project,
                    session_id=c.session_id,
                    summary=c.summary,
                    tts_text=tts_text,
                    agent_number=c.agent_number,
                    listen_after=c.listen_after,
                ))
            else:
                # Multiple completions from same project
                last = completions[-1]
                tts_text = f"{len(completions)} tasks done on {project}. {last.summary}"
                await self._emit(NarrationEvent(
                    event_type="batch_completed",
                    project=project,
                    session_id=last.session_id,
                    summary=last.summary,
                    tts_text=tts_text,
                    agent_number=last.agent_number,
                    batch_count=len(completions),
                    listen_after=last.listen_after,
                ))
        else:
            # Multiple projects
            total = sum(len(v) for v in by_project.values())
            parts = []
            last_completion = pending[-1]
            for proj, completions in by_project.items():
                last = completions[-1]
                parts.append(f"{proj}: {last.summary}")

            count_word = {2: "Two", 3: "Three", 4: "Four", 5: "Five"}.get(total, str(total))
            tts_text = f"{count_word} agents finished. " + ". ".join(parts) + "."
            await self._emit(NarrationEvent(
                event_type="batch_completed",
                project=last_completion.project,
                session_id=last_completion.session_id,
                summary=last_completion.summary,
                tts_text=tts_text,
                agent_number=last_completion.agent_number,
                batch_count=total,
                listen_after=last_completion.listen_after,
            ))

    async def _emit(self, event: NarrationEvent) -> None:
        """Send narration to TTS and broadcast to iOS."""
        # A headset holds the speech floor. It hears the
        # same hook events every client gets and says them itself, through
        # Earshot; speaking here as well, or broadcasting for the phones to
        # speak, would make one summary three voices. The cooldown still runs,
        # so the Mac does not catch up in a burst when the floor comes back.
        if self.floor_held():
            self._get_project_state(event.project).last_narration_time = time.monotonic()
            self.log_fn("info", event.project, f"Narration left to the headset [{event.event_type}]")
            return

        # TTS
        try:
            self.tts.speak(
                event.tts_text,
                project=event.project,
                session_id=event.session_id,
                listen_after=event.listen_after,
            )
        except Exception:
            pass

        # Broadcast to iOS
        try:
            await self.broadcast_fn({
                "type": "narration",
                "event_type": event.event_type,
                "project": event.project,
                "session_id": event.session_id,
                "summary": event.summary,
                "tts_text": event.tts_text,
                "agent_number": event.agent_number,
                "batch_count": event.batch_count,
                "listen_after": event.listen_after,
            })
        except Exception:
            pass

        # Update project state
        ps = self._get_project_state(event.project)
        ps.last_narration_time = time.monotonic()

        self.log_fn("info", event.project, f"Narration [{event.event_type}]: {event.tts_text[:80]}")
