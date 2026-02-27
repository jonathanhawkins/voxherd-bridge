"""Main application window with view switcher and four pages."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from voxherd_panel.bridge_client import BridgeClient
from voxherd_panel.dashboard import DashboardPage
from voxherd_panel.qr_view import QRPage
from voxherd_panel.log_view import LogPage
from voxherd_panel.settings_view import SettingsPage
from voxherd_panel.controls import ControlsBar


class VoxHerdWindow(Adw.ApplicationWindow):
    """Primary window: header bar + view stack with four pages + controls."""

    def __init__(self, host: str, port: int, **kwargs) -> None:
        super().__init__(
            default_width=480,
            default_height=700,
            title="VoxHerd",
            **kwargs,
        )

        self._client = BridgeClient(host=host, port=port)

        # Build UI
        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Header bar with view switcher
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher()
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        root_box.append(header)

        # View stack
        stack = Adw.ViewStack()
        switcher.set_stack(stack)

        # Pages
        self._dashboard = DashboardPage(client=self._client)
        stack.add_titled_with_icon(
            self._dashboard, "sessions", "Sessions", "view-list-symbolic"
        )

        self._qr_page = QRPage(host=host, port=port)
        stack.add_titled_with_icon(
            self._qr_page, "pair", "Pair", "qr-code-symbolic"
        )

        self._log_page = LogPage()
        stack.add_titled_with_icon(
            self._log_page, "log", "Log", "utilities-terminal-symbolic"
        )

        self._settings_page = SettingsPage(client=self._client)
        stack.add_titled_with_icon(
            self._settings_page, "settings", "Settings", "emblem-system-symbolic"
        )

        root_box.append(stack)

        # Controls bar at bottom
        controls = ControlsBar(client=self._client)
        root_box.append(controls)

        self.set_content(root_box)

        # Start polling
        self._client.start_polling()

    def do_close_request(self) -> bool:
        self._client.stop_polling()
        self._log_page.stop()
        return False  # allow close
