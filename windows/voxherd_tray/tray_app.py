"""VoxHerd Windows system tray application using pystray.

Provides a system tray icon with context menu for controlling the bridge
server, viewing logs, showing QR codes for iOS pairing, and managing settings.

Tkinter windows (QR, logs, settings) all run on a single dedicated thread
with one Tk root. Secondary windows use Toplevel, not Tk(), to avoid
cross-thread COM crashes on Windows.
"""

import logging
import queue
import sys
import threading
import tkinter as tk
from typing import Any

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from voxherd_tray.bridge_manager import BridgeManager, BridgeState, LogEntry
from voxherd_tray.config import Preferences

logger = logging.getLogger("voxherd.tray")


# ---------------------------------------------------------------------------
# Icon generation
# ---------------------------------------------------------------------------

def _make_icon(color: str = "green", size: int = 64) -> Image.Image:
    """Generate a simple tray icon: colored circle on transparent background.

    Args:
        color: Fill color for the circle. "green" = running, "red" = stopped,
               "yellow" = starting/error.
        size: Icon dimensions in pixels.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = size // 8
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color,
        outline="white",
        width=max(1, size // 32),
    )
    return img


_ICON_COLORS: dict[BridgeState, str] = {
    BridgeState.STOPPED: "red",
    BridgeState.STARTING: "yellow",
    BridgeState.RUNNING: "green",
    BridgeState.ERROR: "orange",
}


# ---------------------------------------------------------------------------
# Tray Application
# ---------------------------------------------------------------------------

class TrayApp:
    """Main system tray application.

    Creates a pystray Icon with a context menu that mirrors the macOS menu
    bar app's feature set: start/stop bridge, QR code, log viewer, settings.

    All tkinter windows share a single Tk root on a dedicated thread.
    """

    def __init__(self) -> None:
        self.prefs = Preferences.load()
        self.bridge = BridgeManager()
        self.bridge.on_state_change = self._on_bridge_state_change
        self.bridge.on_log_event = self._on_log_event

        self._icon: Icon | None = None

        # Tkinter thread: single Tk root, windows dispatched via queue
        self._tk_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._tk_thread: threading.Thread | None = None
        self._tk_root: tk.Tk | None = None
        self._qr_window: Any = None
        self._log_window: Any = None
        self._settings_window: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the tray icon and enter the main loop. Blocks until quit."""
        # Start the dedicated tkinter thread
        self._tk_thread = threading.Thread(target=self._tk_main_loop, daemon=True)
        self._tk_thread.start()

        self._icon = Icon(
            name="VoxHerd",
            icon=_make_icon(_ICON_COLORS[self.bridge.state]),
            title=self._tooltip_text(),
            menu=self._build_menu(),
        )

        # Auto-start bridge if configured
        if self.prefs.auto_start:
            threading.Thread(target=self._auto_start_bridge, daemon=True).start()

        self._icon.run()

    def shutdown(self) -> None:
        """Graceful shutdown: stop bridge, close tk windows, stop tray."""
        self.bridge.stop()
        # Ask the tk thread to quit
        self._tk_queue.put(("quit", {}))
        if self._icon:
            self._icon.stop()

    # ------------------------------------------------------------------
    # Tkinter thread
    # ------------------------------------------------------------------

    def _tk_main_loop(self) -> None:
        """Run the single Tk root on a dedicated thread.

        All tkinter windows are created here as Toplevel children of this root.
        Commands arrive via self._tk_queue and are dispatched every 100ms.
        """
        self._tk_root = tk.Tk()
        self._tk_root.withdraw()  # Hidden root — only Toplevel windows are shown

        def _poll_queue() -> None:
            try:
                while True:
                    cmd, kwargs = self._tk_queue.get_nowait()
                    if cmd == "quit":
                        self._tk_root.destroy()
                        return
                    elif cmd == "show_qr":
                        self._do_show_qr()
                    elif cmd == "show_logs":
                        self._do_show_logs()
                    elif cmd == "show_settings":
                        self._do_show_settings()
                    elif cmd == "append_log":
                        if self._log_window is not None:
                            try:
                                self._log_window.append_entry(kwargs["entry"])
                            except Exception:
                                pass
            except queue.Empty:
                pass
            self._tk_root.after(100, _poll_queue)

        self._tk_root.after(100, _poll_queue)
        self._tk_root.mainloop()

    def _do_show_qr(self) -> None:
        """Create or focus the QR code window (runs on tk thread)."""
        from voxherd_tray.qr_window import QRWindow

        if self._qr_window is not None:
            try:
                self._qr_window.focus()
                return
            except Exception:
                self._qr_window = None

        self._qr_window = QRWindow(
            root=self._tk_root,
            port=self.bridge.port,
            auth_token=self.bridge.auth_token,
            on_close=lambda: setattr(self, "_qr_window", None),
        )

    def _do_show_logs(self) -> None:
        """Create or focus the log viewer window (runs on tk thread)."""
        from voxherd_tray.log_window import LogWindow

        if self._log_window is not None:
            try:
                self._log_window.focus()
                return
            except Exception:
                self._log_window = None

        self._log_window = LogWindow(
            root=self._tk_root,
            initial_entries=list(self.bridge.recent_events),
            on_close=lambda: setattr(self, "_log_window", None),
        )

    def _do_show_settings(self) -> None:
        """Create or focus the settings window (runs on tk thread)."""
        from voxherd_tray.settings_window import SettingsWindow

        if self._settings_window is not None:
            try:
                self._settings_window.focus()
                return
            except Exception:
                self._settings_window = None

        self._settings_window = SettingsWindow(
            root=self._tk_root,
            prefs=self.prefs,
            bridge=self.bridge,
            on_close=lambda: setattr(self, "_settings_window", None),
        )

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> Menu:
        return Menu(
            MenuItem(
                self._status_text,
                action=None,
                enabled=False,
            ),
            Menu.SEPARATOR,
            MenuItem(
                self._toggle_bridge_text,
                self._toggle_bridge,
            ),
            Menu.SEPARATOR,
            MenuItem("Show QR Code", self._show_qr),
            MenuItem("View Logs", self._show_logs),
            MenuItem("Settings", self._show_settings),
            Menu.SEPARATOR,
            MenuItem("Quit", self._quit),
        )

    # Dynamic menu text callables -- pystray calls these on every menu open
    def _status_text(self, _item: Any = None) -> str:
        if self.bridge.state == BridgeState.RUNNING:
            count = len(self.bridge.sessions)
            return f"Running on :{self.bridge.port} ({count} session{'s' if count != 1 else ''})"
        if self.bridge.state == BridgeState.STARTING:
            return "Starting..."
        if self.bridge.state == BridgeState.ERROR:
            return f"Error: {self.bridge.error_message[:50]}"
        return "Stopped"

    def _toggle_bridge_text(self, _item: Any = None) -> str:
        if self.bridge.state in (BridgeState.RUNNING, BridgeState.STARTING):
            return "Stop Bridge"
        return "Start Bridge"

    # ------------------------------------------------------------------
    # Menu actions (dispatch to tk thread via queue)
    # ------------------------------------------------------------------

    def _toggle_bridge(self, icon: Icon, item: MenuItem) -> None:
        if self.bridge.state in (BridgeState.RUNNING, BridgeState.STARTING):
            self.bridge.stop()
        else:
            self.bridge.start(
                port=self.prefs.port,
                enable_tts=self.prefs.enable_tts,
            )

    def _show_qr(self, icon: Icon, item: MenuItem) -> None:
        self._tk_queue.put(("show_qr", {}))

    def _show_logs(self, icon: Icon, item: MenuItem) -> None:
        self._tk_queue.put(("show_logs", {}))

    def _show_settings(self, icon: Icon, item: MenuItem) -> None:
        self._tk_queue.put(("show_settings", {}))

    def _quit(self, icon: Icon, item: MenuItem) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_bridge_state_change(self) -> None:
        """Update tray icon appearance when bridge state changes."""
        if self._icon is None:
            return
        color = _ICON_COLORS.get(self.bridge.state, "red")
        self._icon.icon = _make_icon(color)
        self._icon.title = self._tooltip_text()

    def _on_log_event(self, entry: LogEntry) -> None:
        """Forward log events to the log window via the tk thread queue."""
        self._tk_queue.put(("append_log", {"entry": entry}))

    def _tooltip_text(self) -> str:
        if self.bridge.state == BridgeState.RUNNING:
            return f"VoxHerd - Running on :{self.bridge.port}"
        if self.bridge.state == BridgeState.STARTING:
            return "VoxHerd - Starting..."
        if self.bridge.state == BridgeState.ERROR:
            return "VoxHerd - Error"
        return "VoxHerd - Stopped"

    # ------------------------------------------------------------------
    # Auto-start
    # ------------------------------------------------------------------

    def _auto_start_bridge(self) -> None:
        """Start the bridge after a brief delay to let the tray settle."""
        import time
        time.sleep(0.5)
        if self.bridge.state == BridgeState.STOPPED:
            self.bridge.start(
                port=self.prefs.port,
                enable_tts=self.prefs.enable_tts,
            )
