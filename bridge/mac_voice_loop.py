"""Mac voice loop: TTS -> STT -> intent parse -> dispatch.

Two listening modes:
1. **Listen-after-speak** — after every TTS announcement, listens for a
   follow-up voice command.
2. **Wake word** (``--wake-word``) — continuously listens for "Hey Claude"
   (or similar trigger phrases), then processes the rest as a command.

Parses transcriptions using regex-based intent matching (ported from
iOS ``IntentRouter.swift``), resolves the target project, and dispatches
the command to the appropriate Claude Code session.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bridge.mac_stt import MacSTT
from bridge.mac_tts import MacTTS, SpeechItem
from bridge.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Wake word config
# ---------------------------------------------------------------------------

# Trigger phrases — matched case-insensitively at the start of transcription.
# The wake word prefix is stripped before processing the command.
WAKE_PHRASES = [
    "hey claude",
    "hey clod",      # common misrecognition
    "a claude",       # common misrecognition
    "hey cloud",      # common misrecognition
    "okay claude",
    "ok claude",
]

# How long each wake-word listen window lasts (seconds).
WAKE_LISTEN_TIMEOUT = 4

# Pause between wake-word listen cycles (seconds).
WAKE_CYCLE_PAUSE = 0.3


# ---------------------------------------------------------------------------
# Wake word matching
# ---------------------------------------------------------------------------


def _strip_wake_word(text: str) -> str | None:
    """If *text* starts with a wake phrase, return the remainder. Else ``None``.

    Returns empty string if the utterance is ONLY the wake word.
    """
    lowered = text.lower().strip()
    for phrase in WAKE_PHRASES:
        if lowered.startswith(phrase):
            remainder = text[len(phrase):].strip().lstrip(",").strip()
            return remainder
    return None


# ---------------------------------------------------------------------------
# Intent types
# ---------------------------------------------------------------------------


@dataclass
class ParsedIntent:
    """Result of parsing a voice transcription."""

    action: str  # status, tell, switch, approve, deny, what_did, pause, resume, bare
    project: str | None = None
    message: str | None = None
    agent_number: int | None = None  # explicit agent number for multi-agent routing


# ---------------------------------------------------------------------------
# Regex patterns (ported from iOS IntentRouter.swift)
# ---------------------------------------------------------------------------

_STATUS = re.compile(r"^(status|what'?s running)$", re.IGNORECASE)
_TELL_AGENT = re.compile(r"^tell (.+?) (?:agent |number )(\d+) to (.+)$", re.IGNORECASE)
_TELL = re.compile(r"^tell (.+?) to (.+)$", re.IGNORECASE)
_SWITCH = re.compile(r"^switch to (.+)$", re.IGNORECASE)
_APPROVE = re.compile(r"^approve (.+)$", re.IGNORECASE)
_DENY = re.compile(r"^deny (.+)$", re.IGNORECASE)
_WHAT_DID = re.compile(r"^what did (.+?) do(?: last)?\??$", re.IGNORECASE)
_PAUSE = re.compile(r"^pause (.+)$", re.IGNORECASE)
_RESUME = re.compile(r"^resume (.+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Fuzzy project matching (ported from IntentRouter.matchProject)
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    """Standard DP Levenshtein distance."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            temp = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
            prev = temp
    return dp[n]


def match_project(name: str, known_projects: list[str]) -> str | None:
    """Fuzzy-match a spoken project name against known projects.

    1. Case-insensitive substring (prefer shortest match)
    2. Levenshtein distance with threshold of 3
    """
    lowered = name.lower()

    # Pass 1: substring containment (bidirectional)
    substring_matches = [
        p for p in known_projects
        if lowered in p.lower() or p.lower() in lowered
    ]
    if substring_matches:
        return min(substring_matches, key=len)

    # Pass 2: Levenshtein distance, threshold scaled by name length.
    # Short names (<=4 chars) use threshold 1, medium (5-8) use 2, long use 3.
    best: str | None = None
    best_distance = 999
    for p in known_projects:
        threshold = max(1, min(3, len(p) // 3))
        d = _levenshtein(lowered, p.lower())
        if d <= threshold and d < best_distance:
            best_distance = d
            best = p
    return best


# ---------------------------------------------------------------------------
# Intent parsing
# ---------------------------------------------------------------------------


_MAX_VOICE_INPUT_LEN = 2000  # Max chars for a single voice transcription


def parse_intent(
    text: str,
    known_projects: list[str],
    active_project: str | None,
    last_announced_project: str | None,
) -> ParsedIntent:
    """Parse a single transcription segment into a ``ParsedIntent``."""
    stripped = text.strip()[:_MAX_VOICE_INPUT_LEN]

    # Status
    if _STATUS.match(stripped):
        return ParsedIntent(action="status")

    # Tell {project} agent {N} to {message}
    m = _TELL_AGENT.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="tell", project=resolved, message=m.group(3), agent_number=int(m.group(2)))
        return ParsedIntent(action="ambiguous", message=f"Unknown project '{m.group(1)}' — did you mean one of: {', '.join(known_projects) or '(none registered)'}")

    # Tell {project} to {message}
    m = _TELL.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="tell", project=resolved, message=m.group(2))
        return ParsedIntent(action="ambiguous", message=f"Unknown project '{m.group(1)}' — did you mean one of: {', '.join(known_projects) or '(none registered)'}")

    # Switch to {project}
    m = _SWITCH.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="switch", project=resolved)

    # Approve {project}
    m = _APPROVE.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="approve", project=resolved)

    # Deny {project}
    m = _DENY.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="deny", project=resolved)

    # What did {project} do
    m = _WHAT_DID.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="what_did", project=resolved)

    # Pause {project}
    m = _PAUSE.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="pause", project=resolved)

    # Resume {project}
    m = _RESUME.match(stripped)
    if m:
        resolved = match_project(m.group(1), known_projects)
        if resolved:
            return ParsedIntent(action="resume", project=resolved)

    # Bare command — resolve project via priority chain.
    # Only treat as a command if the text looks intentional (>3 words).
    # Short noise fragments from ambient pickup should be ignored.
    word_count = len(stripped.split())
    project = active_project or last_announced_project
    if project and word_count >= 2:
        return ParsedIntent(action="tell", project=project, message=stripped)

    # Not enough context to act — stay silent rather than nagging.
    return ParsedIntent(action="ignore", message=stripped)


def parse_compound(
    text: str,
    known_projects: list[str],
    active_project: str | None,
    last_announced_project: str | None,
) -> list[ParsedIntent]:
    """Parse a compound utterance into one or more intents.

    Splits on sentence boundaries (". " or ".\\n") and processes
    each segment sequentially. A "switch" intent updates the active
    project for subsequent segments.
    """
    segments = re.split(r"\.\s+", text)
    intents: list[ParsedIntent] = []
    current_active = active_project

    for segment in segments:
        cleaned = segment.strip().rstrip(".")
        if not cleaned:
            continue
        intent = parse_intent(cleaned, known_projects, current_active, last_announced_project)
        intents.append(intent)
        if intent.action == "switch" and intent.project:
            current_active = intent.project

    return intents


# ---------------------------------------------------------------------------
# Voice loop
# ---------------------------------------------------------------------------


class MacVoiceLoop:
    """Orchestrates the TTS -> STT -> intent -> dispatch cycle.

    After every TTS announcement completes, listens on the Mac mic for
    a follow-up voice command, parses it, and dispatches it.
    """

    def __init__(
        self,
        tts: MacTTS,
        stt: MacSTT,
        sessions: SessionManager,
        dispatch: Callable[[str, str, str], Awaitable[None]],
        log: Callable[[str, str, str], None],
    ) -> None:
        self.tts = tts
        self.stt = stt
        self.sessions = sessions
        self._dispatch = dispatch
        self._log = log
        self._listening = False
        self._wake_word_enabled = False
        self._wake_word_task: asyncio.Task | None = None

    def wire(self) -> None:
        """Connect this loop as the TTS ``on_speech_complete`` callback."""
        self.tts.on_speech_complete = self._on_speech_complete

    # ------------------------------------------------------------------
    # Wake word
    # ------------------------------------------------------------------

    def start_wake_word(self) -> None:
        """Start the background wake-word listener loop."""
        if self._wake_word_task is not None:
            return
        self._wake_word_enabled = True
        self._wake_word_task = asyncio.create_task(self._wake_word_loop())
        self._log("success", "bridge", "Wake word listener started — say 'Hey Claude' to activate")

    def stop_wake_word(self) -> None:
        """Stop the background wake-word listener."""
        self._wake_word_enabled = False
        if self._wake_word_task:
            self._wake_word_task.cancel()
            self._wake_word_task = None

    async def _wake_word_loop(self) -> None:
        """Continuously listen for the wake word, then process the command."""
        while self._wake_word_enabled:
            # Don't listen while TTS is speaking or we're already handling a command
            if self._listening:
                await asyncio.sleep(1)
                continue

            try:
                text = await self.stt.listen(timeout=WAKE_LISTEN_TIMEOUT, quiet=True)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[wake] STT error: {exc}", file=sys.stderr)
                await asyncio.sleep(2)
                continue

            if not text:
                await asyncio.sleep(WAKE_CYCLE_PAUSE)
                continue

            # Check for wake word
            command = _strip_wake_word(text)
            if command is None:
                # Not a wake-word utterance — ignore
                await asyncio.sleep(WAKE_CYCLE_PAUSE)
                continue

            self._log("info", "bridge", f"Wake word detected! Command: {command}")

            if not command:
                # They said "Hey Claude" but nothing else — do a full listen
                self.tts.speak("Yes?", listen_after=True)
                # The listen-after-speak callback will handle the rest
                await asyncio.sleep(3)  # wait for TTS + STT cycle
                continue

            # Process the command directly
            self._listening = True
            try:
                await self._handle_transcription(command)
            finally:
                self._listening = False

            await asyncio.sleep(WAKE_CYCLE_PAUSE)

    async def _on_speech_complete(self, item: SpeechItem) -> None:
        """Called when a TTS utterance finishes. Starts listening if appropriate."""
        if not item.listen_after or not self.stt.enabled:
            return

        if self._listening:
            return  # don't overlap listens
        self._listening = True

        try:
            self._log("info", "bridge", "TTS done. Waiting for audio settle...")
            await asyncio.sleep(0.3)
            self._log("info", "bridge", "Listening for voice command... (speak after beep)")
            text = await self.stt.listen()
            if not text:
                self._log("info", "bridge", "No speech detected, returning to idle.")
                return

            self._log("info", "bridge", f"Heard: {text}")
            await self._handle_transcription(text)
        finally:
            self._listening = False

    async def _handle_transcription(self, text: str) -> None:
        """Parse transcription and dispatch the resulting intents."""
        known_projects = [s.project for s in self.sessions.get_all_sessions().values()]
        intents = parse_compound(
            text,
            known_projects,
            self.sessions.active_project,
            self.sessions.last_announced_project,
        )

        for intent in intents:
            await self._execute_intent(intent)

    async def _execute_intent(self, intent: ParsedIntent) -> None:
        """Execute a single parsed intent."""
        if intent.action == "status":
            await self._handle_status()

        elif intent.action == "switch":
            if intent.project:
                self.sessions.set_active_project(intent.project)
                self._log("info", intent.project, "Switched active project")
                self.tts.speak(
                    f"Switched to {intent.project}.",
                    listen_after=True,
                )

        elif intent.action == "tell":
            if intent.project and intent.message:
                if intent.agent_number is not None:
                    session = self.sessions.get_session_by_project_and_number(intent.project, intent.agent_number)
                else:
                    session = self.sessions.get_session_by_project(intent.project)
                if session is None:
                    self._log("error", intent.project or "unknown", "No session found")
                    self.tts.speak(
                        f"No session found for {intent.project}.",
                        listen_after=True,
                    )
                    return
                self.sessions.update_status(session.session_id, "active", activity_type="working")
                await self._dispatch(session.session_id, session.project_dir, intent.message)
                self._log("warning", session.project, f"Voice command dispatched: {intent.message}")

        elif intent.action in ("approve", "deny"):
            if intent.project:
                session = self.sessions.get_session_by_project(intent.project)
                if session and session.status == "waiting":
                    msg = "yes, proceed" if intent.action == "approve" else "no, deny"
                    self.sessions.update_status(session.session_id, "active", activity_type="working")
                    await self._dispatch(session.session_id, session.project_dir, msg)
                    self._log("warning", session.project, f"Permission {intent.action}d via voice")

        elif intent.action == "what_did":
            if intent.project:
                session = self.sessions.get_session_by_project(intent.project)
                if session and session.last_summary:
                    msg = f"{session.project}: {session.last_summary}"
                    if session.sub_agent_count > 0:
                        msg += f" Currently has {session.sub_agent_count} sub-agent{'s' if session.sub_agent_count != 1 else ''} running."
                    self.tts.speak(msg, listen_after=True)
                else:
                    self.tts.speak(
                        f"No recent activity for {intent.project}.",
                        listen_after=True,
                    )

        elif intent.action == "ambiguous":
            self._log("info", "bridge", f"Ambiguous command: {intent.message}")
            if intent.message:
                self.tts.speak(
                    "Which project did you mean?",
                    listen_after=True,
                )

        elif intent.action == "ignore":
            self._log("info", "bridge", f"Ignored: {intent.message}")

    async def _handle_status(self) -> None:
        """Announce status of all sessions, including sub-agent counts."""
        all_sessions = self.sessions.get_all_sessions()
        if not all_sessions:
            self.tts.speak("No sessions registered.", listen_after=True)
            return

        lines = []
        for s in all_sessions.values():
            status_text = f"{s.project} is {s.status}"
            if s.sub_agent_count > 0:
                status_text += f" with {s.sub_agent_count} sub-agent{'s' if s.sub_agent_count != 1 else ''}"
            lines.append(status_text)
        self.tts.speak(". ".join(lines) + ".", listen_after=True)
