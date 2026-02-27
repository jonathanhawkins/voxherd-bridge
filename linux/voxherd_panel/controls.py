"""Start/stop/restart controls bar using systemctl --user."""

import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk

from voxherd_panel.bridge_client import BridgeClient


class ControlsBar(Gtk.Box):
    """Bottom bar with status indicator and bridge lifecycle buttons."""

    def __init__(self, client: BridgeClient) -> None:
        super().__init__(spacing=8)
        self._client = client
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(8)
        self.set_margin_bottom(8)

        # Status dot + label
        self._dot = Gtk.DrawingArea()
        self._dot.set_content_width(10)
        self._dot.set_content_height(10)
        self._dot.set_valign(Gtk.Align.CENTER)
        self.append(self._dot)

        self._status_label = Gtk.Label(label="Checking...")
        self._status_label.set_halign(Gtk.Align.START)
        self.append(self._status_label)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.append(spacer)

        # Session count
        self._count_label = Gtk.Label(label="")
        self._count_label.add_css_class("caption")
        self._count_label.add_css_class("dim-label")
        self.append(self._count_label)

        # Start button
        self._start_btn = Gtk.Button(label="Start")
        self._start_btn.add_css_class("suggested-action")
        self._start_btn.connect("clicked", self._on_start)
        self.append(self._start_btn)

        # Stop button
        self._stop_btn = Gtk.Button(label="Stop")
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.connect("clicked", self._on_stop)
        self.append(self._stop_btn)

        # Restart button
        self._restart_btn = Gtk.Button(label="Restart")
        self._restart_btn.connect("clicked", self._on_restart)
        self.append(self._restart_btn)

        # Register as listeners
        client.on_status(self._update_status)
        client.on_sessions(self._on_sessions_update)

        self._update_status(False)

    def _on_sessions_update(self, sessions) -> None:
        self._count_label.set_label(
            f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}"
            if sessions else ""
        )

    def _update_status(self, running: bool) -> None:
        color = "#a6e3a1" if running else "#f38ba8"  # Green / Red
        provider = Gtk.CssProvider()
        provider.load_from_string(
            f"drawingarea {{ background: {color}; border-radius: 50%; "
            f"min-width: 10px; min-height: 10px; }}"
        )
        self._dot.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._status_label.set_label("Running" if running else "Stopped")
        self._start_btn.set_sensitive(not running)
        self._stop_btn.set_sensitive(running)
        self._restart_btn.set_sensitive(running)

    def _run_systemctl(self, action: str) -> None:
        """Run systemctl --user {action} voxherd-bridge in background."""
        try:
            subprocess.Popen(
                ["systemctl", "--user", action, "voxherd-bridge"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

        if action in ("start", "restart"):
            # Poll for readiness
            self._poll_ready(attempts=20)

    def _poll_ready(self, attempts: int = 20, interval_ms: int = 500) -> None:
        """Poll /health every interval_ms until bridge responds or attempts exhausted."""
        import json
        import urllib.request
        import urllib.error

        def _check() -> bool:
            nonlocal attempts
            attempts -= 1
            if attempts <= 0:
                return False
            try:
                req = urllib.request.Request(
                    f"{self._client.base_url}/health", method="GET"
                )
                with urllib.request.urlopen(req, timeout=1) as resp:
                    data = json.loads(resp.read())
                    if data.get("ok"):
                        self._update_status(True)
                        return False  # stop polling
            except Exception:
                pass
            return True  # keep polling

        GLib.timeout_add(interval_ms, _check)

    def _on_start(self, _btn: Gtk.Button) -> None:
        self._status_label.set_label("Starting...")
        self._run_systemctl("start")

    def _on_stop(self, _btn: Gtk.Button) -> None:
        self._status_label.set_label("Stopping...")
        self._run_systemctl("stop")
        # Give it a moment then update status
        GLib.timeout_add(1500, lambda: (self._update_status(False), False)[-1])

    def _on_restart(self, _btn: Gtk.Button) -> None:
        self._status_label.set_label("Restarting...")
        self._run_systemctl("restart")
