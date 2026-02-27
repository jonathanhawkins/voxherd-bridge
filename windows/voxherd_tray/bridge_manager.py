"""Manages the VoxHerd bridge server as a subprocess.

Mirrors the macOS BridgeProcessManager.swift: spawns ``python -m bridge run
--headless --tts``, monitors stdout for the ready signal, polls /api/sessions
for health checks, and auto-restarts on unexpected exit.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable

import requests

from voxherd_tray.config import AUTH_TOKEN_FILE, Preferences, load_auth_token

logger = logging.getLogger("voxherd.bridge")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class BridgeState(Enum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    ERROR = auto()


@dataclass
class LogEntry:
    """A single log event from the bridge's JSON-lines output."""
    timestamp: str
    level: str
    project: str
    message: str


@dataclass
class SessionInfo:
    """A Claude Code session tracked by the bridge."""
    session_id: str
    project: str = "unknown"
    status: str = "active"
    activity_type: str = "registered"
    last_summary: str = ""
    agent_number: int = 1
    sub_agent_count: int = 0
    last_activity: str = ""


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class BridgeManager:
    """Manages the bridge server subprocess lifecycle.

    All I/O runs on background threads to avoid blocking the tray's main loop.
    """

    def __init__(self) -> None:
        self.state: BridgeState = BridgeState.STOPPED
        self.error_message: str = ""
        self.port: int = 7777
        self.auth_token: str | None = None
        self.sessions: list[SessionInfo] = []
        self.recent_events: list[LogEntry] = []

        self._process: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._restart_timer: threading.Timer | None = None

        self._restart_delay: float = 1.0
        self._consecutive_poll_failures: int = 0
        self._max_poll_failures: int = 5
        self._max_events: int = 500
        self._stop_event: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()

        # Saved preferences for auto-restart
        self._last_port: int = 7777
        self._last_enable_tts: bool = True

        # Callbacks for UI updates
        self.on_state_change: Callable[[], None] | None = None
        self.on_log_event: Callable[[LogEntry], None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, port: int = 7777, enable_tts: bool = True) -> None:
        """Start the bridge server subprocess."""
        if self.state not in (BridgeState.STOPPED, BridgeState.ERROR):
            return

        if not (1024 <= port <= 65535):
            self._set_error(f"Invalid port: {port} (must be 1024-65535)")
            return

        self._set_state(BridgeState.STARTING)
        self._restart_delay = 1.0
        self._consecutive_poll_failures = 0
        self._stop_event.clear()
        self.port = port
        self._last_port = port
        self._last_enable_tts = enable_tts

        bridge_main = self._find_bridge()
        if bridge_main is None:
            self._set_error("Cannot find bridge/__main__.py. Is the repo available?")
            return

        repo_root = bridge_main.parent.parent

        python_exe = sys.executable
        args = [python_exe, "-m", "bridge", "run", "--headless"]
        if enable_tts:
            args.append("--tts")
        args.extend(["--port", str(port)])

        env = os.environ.copy()
        # Ensure the repo root is in PYTHONPATH so ``import bridge`` works
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) + (os.pathsep + existing if existing else "")

        try:
            creation_flags = 0
            try:
                creation_flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            except AttributeError:
                pass

            self._process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(repo_root),
                env=env,
                creationflags=creation_flags,
            )
            logger.info("Bridge process started (PID: %d)", self._process.pid)
            self._start_reading_stdout()
            self._start_reading_stderr()
        except OSError as exc:
            self._set_error(f"Failed to start: {exc}")

    def stop(self) -> None:
        """Stop the bridge server subprocess."""
        self._stop_event.set()

        if self._restart_timer is not None:
            self._restart_timer.cancel()
            self._restart_timer = None

        proc = self._process
        self._process = None
        self.sessions = []

        if proc is None or proc.poll() is not None:
            # Already dead — just reap to release handles
            if proc is not None:
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
            self._set_state(BridgeState.STOPPED)
            return

        self._set_state(BridgeState.STOPPED)

        # Graceful shutdown: terminate, then reap on a background thread
        proc.terminate()

        def _wait_and_reap() -> None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                logger.warning("Force-killed bridge after timeout")
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass

        reap_thread = threading.Thread(target=_wait_and_reap, daemon=True)
        reap_thread.start()

    def restart(self, port: int = 7777, enable_tts: bool = True) -> None:
        """Stop and restart the bridge."""
        self.stop()
        # Brief pause for port release
        time.sleep(0.5)
        self.start(port=port, enable_tts=enable_tts)

    # ------------------------------------------------------------------
    # Output reading
    # ------------------------------------------------------------------

    def _start_reading_stdout(self) -> None:
        """Spawn a daemon thread to read JSON lines from the bridge's stdout."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return

        def _reader() -> None:
            assert proc is not None and proc.stdout is not None
            for raw_line in proc.stdout:
                if self._stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self._process_stdout_line(line)

            # Process exited
            if not self._stop_event.is_set():
                exit_code = proc.poll()
                if self.state in (BridgeState.RUNNING, BridgeState.STARTING):
                    logger.warning("Bridge exited unexpectedly (code %s)", exit_code)
                    self._set_error(f"Bridge exited (code {exit_code})")
                    self._schedule_restart()

        self._stdout_thread = threading.Thread(target=_reader, daemon=True)
        self._stdout_thread.start()

    def _start_reading_stderr(self) -> None:
        """Spawn a daemon thread to read stderr for error reporting."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return

        def _reader() -> None:
            assert proc is not None and proc.stderr is not None
            for raw_line in proc.stderr:
                if self._stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self._process_stderr_line(line)

        self._stderr_thread = threading.Thread(target=_reader, daemon=True)
        self._stderr_thread.start()

    def _process_stdout_line(self, line: str) -> None:
        """Parse a JSON line from bridge stdout."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        # Check for ready signal: {"status": "ready", "port": 7777}
        if data.get("status") == "ready":
            self.port = data.get("port", self.port)
            self._set_state(BridgeState.RUNNING)
            logger.info("Bridge ready on port %d", self.port)
            self._load_auth_token()
            self._start_session_polling()
            return

        # Parse as log entry
        ts = data.get("ts", "")
        level = data.get("level", "info")
        project = data.get("project", "bridge")
        message = data.get("message", "")

        entry = LogEntry(timestamp=ts, level=level, project=project, message=message)
        with self._lock:
            self.recent_events.append(entry)
            if len(self.recent_events) > self._max_events:
                self.recent_events = self.recent_events[-self._max_events:]

        if self.on_log_event:
            self.on_log_event(entry)

        # Trigger immediate session refresh on registration events
        if any(kw in message for kw in ("registered", "removed", "deregistered", "Cleared")):
            self._fetch_sessions()

    def _process_stderr_line(self, line: str) -> None:
        """Surface critical stderr lines as error log entries."""
        logger.debug("Bridge stderr: %s", line)
        if any(kw in line for kw in ("Error", "Traceback", "error")):
            from datetime import datetime
            entry = LogEntry(
                timestamp=datetime.now().astimezone().isoformat(),
                level="error",
                project="bridge",
                message=line,
            )
            with self._lock:
                self.recent_events.append(entry)
                if len(self.recent_events) > self._max_events:
                    self.recent_events = self.recent_events[-self._max_events:]
            if self.on_log_event:
                self.on_log_event(entry)

    # ------------------------------------------------------------------
    # Session polling / health check
    # ------------------------------------------------------------------

    def _start_session_polling(self) -> None:
        """Poll /api/sessions every 2 seconds for health + session data."""

        def _poll_loop() -> None:
            while not self._stop_event.is_set():
                self._stop_event.wait(2.0)
                if self._stop_event.is_set():
                    break
                self._fetch_sessions()

        self._poll_thread = threading.Thread(target=_poll_loop, daemon=True)
        self._poll_thread.start()

    def _fetch_sessions(self) -> None:
        """Fetch sessions from the bridge REST API."""
        if self.state != BridgeState.RUNNING:
            return

        url = f"http://127.0.0.1:{self.port}/api/sessions"
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code != 200:
                self._record_poll_failure()
                return
        except (requests.RequestException, OSError):
            self._record_poll_failure()
            return

        # Healthy response
        self._consecutive_poll_failures = 0

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return

        sessions: list[SessionInfo] = []
        for sid, info in data.items():
            if not isinstance(info, dict):
                continue
            sessions.append(SessionInfo(
                session_id=info.get("session_id", sid),
                project=info.get("project", "unknown"),
                status=info.get("status", "active"),
                activity_type=info.get("activity_type", "registered"),
                last_summary=info.get("last_summary", ""),
                agent_number=info.get("agent_number", 1),
                sub_agent_count=info.get("sub_agent_count", 0),
                last_activity=info.get("last_activity", ""),
            ))

        # Sort: active first, then needs-attention, then by project name
        def _sort_key(s: SessionInfo) -> tuple[int, int, str]:
            is_active = s.activity_type in (
                "thinking", "writing", "searching", "running",
                "building", "testing", "working", "registered",
            )
            needs_attention = s.activity_type in ("approval", "input")
            return (0 if is_active else 1, 0 if needs_attention else 1, s.project.lower())

        sessions.sort(key=_sort_key)
        self.sessions = sessions

        if self.on_state_change:
            self.on_state_change()

    def _record_poll_failure(self) -> None:
        """Track consecutive health check failures; force-restart on threshold.

        Must not call blocking stop/sleep/start on the poll thread — doing so
        would deadlock because stop() sets _stop_event which the poll thread
        is waiting on.  Instead, schedule the restart on a separate thread.
        """
        if self.state != BridgeState.RUNNING:
            return
        self._consecutive_poll_failures += 1
        logger.warning(
            "Health check failed (%d/%d)",
            self._consecutive_poll_failures,
            self._max_poll_failures,
        )
        if self._consecutive_poll_failures >= self._max_poll_failures:
            logger.error("Bridge unresponsive, scheduling restart")
            self._consecutive_poll_failures = 0

            def _do_restart() -> None:
                self.stop()
                time.sleep(1.0)
                self.start(port=self._last_port, enable_tts=self._last_enable_tts)

            restart_thread = threading.Thread(target=_do_restart, daemon=True)
            restart_thread.start()

    # ------------------------------------------------------------------
    # Auth token
    # ------------------------------------------------------------------

    def _load_auth_token(self) -> None:
        """Load the auth token written by the bridge on startup."""
        # Give the bridge a moment to write the token file
        for _ in range(5):
            token = load_auth_token()
            if token:
                self.auth_token = token
                logger.info("Auth token loaded")
                return
            time.sleep(0.3)
        logger.warning("Auth token not found at %s", AUTH_TOKEN_FILE)

    # ------------------------------------------------------------------
    # Auto-restart
    # ------------------------------------------------------------------

    def _schedule_restart(self) -> None:
        """Schedule an auto-restart with exponential backoff."""
        delay = self._restart_delay
        logger.info("Restarting bridge in %.1fs...", delay)

        def _do_restart() -> None:
            self._restart_delay = min(self._restart_delay * 2, 30.0)
            self.start(port=self._last_port, enable_tts=self._last_enable_tts)

        self._restart_timer = threading.Timer(delay, _do_restart)
        self._restart_timer.daemon = True
        self._restart_timer.start()

    # ------------------------------------------------------------------
    # Bridge discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_bridge() -> Path | None:
        """Locate bridge/__main__.py relative to this file or the project root.

        Search order:
        1. VOXHERD_PROJECT_DIR environment variable
        2. Two levels up from this file (windows/voxherd_tray -> repo root)
        3. Three levels up (in case of nested layout)
        """
        # Environment override
        project_dir = os.environ.get("VOXHERD_PROJECT_DIR")
        if project_dir:
            candidate = Path(project_dir) / "bridge" / "__main__.py"
            if candidate.exists():
                return candidate

        # Relative to this file
        this_dir = Path(__file__).resolve().parent
        for levels_up in (2, 3):
            root = this_dir
            for _ in range(levels_up):
                root = root.parent
            candidate = root / "bridge" / "__main__.py"
            if candidate.exists():
                return candidate

        return None

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _set_state(self, state: BridgeState) -> None:
        self.state = state
        self.error_message = ""
        if self.on_state_change:
            self.on_state_change()

    def _set_error(self, message: str) -> None:
        self.state = BridgeState.ERROR
        self.error_message = message
        logger.error(message)
        if self.on_state_change:
            self.on_state_change()
