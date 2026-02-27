"""HTTP polling client for the VoxHerd bridge REST API.

Uses GLib.timeout_add_seconds for periodic polling so it integrates with
the GTK main loop. All HTTP is done via urllib (stdlib) with short timeouts
to avoid blocking the UI thread.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Callable

from gi.repository import GLib

from voxherd_panel.models import SessionInfo

_AUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".voxherd", "auth_token")


def _load_token() -> str:
    """Load auth token from ~/.voxherd/auth_token."""
    try:
        with open(_AUTH_TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


class BridgeClient:
    """Polls the bridge server REST API and notifies callbacks on changes."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7777) -> None:
        self.host = host
        self.port = port
        self._token = _load_token()
        self._poll_id: int | None = None
        self._session_listeners: list[Callable[[list[SessionInfo]], None]] = []
        self._status_listeners: list[Callable[[bool], None]] = []
        self.sessions: list[SessionInfo] = []
        self.bridge_running: bool = False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def reload_token(self) -> None:
        self._token = _load_token()

    def on_sessions(self, callback: Callable[[list[SessionInfo]], None]) -> None:
        """Register a listener for session updates."""
        self._session_listeners.append(callback)

    def on_status(self, callback: Callable[[bool], None]) -> None:
        """Register a listener for bridge status changes."""
        self._status_listeners.append(callback)

    def start_polling(self, interval_seconds: int = 2) -> None:
        """Begin periodic polling. Call from the GTK main thread."""
        self.stop_polling()
        # Fire immediately, then every interval_seconds
        self._poll()
        self._poll_id = GLib.timeout_add_seconds(interval_seconds, self._poll)

    def stop_polling(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def _poll(self) -> bool:
        """Fetch health + sessions. Returns True to keep the GLib timer alive."""
        # Health check (unauthenticated)
        running = False
        try:
            req = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                running = data.get("ok", False)
        except Exception:
            running = False

        if running != self.bridge_running:
            self.bridge_running = running
            for cb in self._status_listeners:
                cb(running)

        if not running:
            if self.sessions:
                self.sessions = []
                for cb in self._session_listeners:
                    cb([])
            return True  # keep polling

        # Sessions (authenticated)
        try:
            req = urllib.request.Request(f"{self.base_url}/api/sessions", method="GET")
            if self._token:
                req.add_header("Authorization", f"Bearer {self._token}")
            with urllib.request.urlopen(req, timeout=3) as resp:
                raw = json.loads(resp.read())
        except Exception:
            return True

        # Parse sessions dict: {session_id: {...}}
        new_sessions = []
        if isinstance(raw, dict):
            for sid, info in raw.items():
                if isinstance(info, dict):
                    info.setdefault("session_id", sid)
                    new_sessions.append(SessionInfo.from_dict(info))

        # Sort: active first, then attention, then idle
        def _sort_key(s: SessionInfo) -> tuple[int, str]:
            if s.is_active:
                return (0, s.project)
            if s.needs_attention:
                return (1, s.project)
            return (2, s.project)

        new_sessions.sort(key=_sort_key)
        self.sessions = new_sessions

        for cb in self._session_listeners:
            cb(new_sessions)

        return True  # keep polling
