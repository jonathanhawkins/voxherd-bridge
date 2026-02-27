"""Narration engine — decides when and how to announce events via TTS.

Sits between event sources (routes.py stop/notification events, activity.py
state transitions) and output sinks (mac_tts, broadcast_to_ios).  Replaces
the direct ``mac_tts.speak()`` calls scattered across routes.py.

Key features:
- Batches rapid completions within a 5-second window
- Per-project cooldown (20s) to avoid being annoying
- Three verbosity levels: quiet, normal, verbose
- Errors and approvals always bypass cooldown
"""

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
    last_activity_change: float = field(default_factory=time.monotonic)
    stall_warned: bool = False


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
    ):
        self.tts = tts
        self.broadcast_fn = broadcast_fn
        self.log_fn = log_fn
        self.sessions = sessions
        self.stt = stt
        self.listen_after_enabled = listen_after_enabled

        self.verbosity = Verbosity.NORMAL
        self._cooldown = 20.0        # seconds between narrations per project
        self._batch_window = 5.0     # seconds to coalesce rapid completions
        self._stall_threshold = 60.0 # seconds of no activity = stalled

        self._project_state: dict[str, _ProjectState] = {}
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
            if self.verbosity.value != "quiet" or True:  # errors always narrate
                tts_text = f"{self._tts_prefix(project, session_id, agent_number)} errored. {summary}"
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
        tts_text = f"{self._tts_prefix(project, session_id, agent_number)} is waiting. {message}"
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
    ) -> None:
        """Track activity changes. Only announces stalls in VERBOSE mode."""
        ps = self._get_project_state(project)
        ps.last_activity_change = time.monotonic()
        ps.stall_warned = False

        # Stall detection only in VERBOSE mode
        if self.verbosity != Verbosity.VERBOSE:
            return

        # We don't actively poll here — stall detection happens in check_stalls()

    async def check_stalls(self) -> None:
        """Check for stalled sessions. Call periodically from activity poll loop."""
        if self.verbosity != Verbosity.VERBOSE:
            return

        now = time.monotonic()
        for session in list(self.sessions.get_all_sessions().values()):
            if session.status != "active":
                continue
            ps = self._get_project_state(session.project)
            if ps.stall_warned:
                continue
            if now - ps.last_activity_change > self._stall_threshold:
                ps.stall_warned = True
                tts_text = f"{session.project} appears stalled."
                await self._emit(NarrationEvent(
                    event_type="stalled",
                    project=session.project,
                    session_id=session.session_id,
                    summary="No activity detected",
                    tts_text=tts_text,
                    agent_number=getattr(session, 'agent_number', 1),
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
        """Return TTS prefix like 'voxherd agent 2' if multiple agents, else just project."""
        same_project = self.sessions.get_sessions_by_project(project)
        if len(same_project) > 1:
            return f"{project} agent {agent_number}"
        return project

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
                tts_text = f"{prefix} finished. {c.summary}"
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
                tts_text = f"{project}: {len(completions)} tasks done. Last: {last.summary}"
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
