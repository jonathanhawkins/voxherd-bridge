"""Entry point for the VoxHerd panel app.

Usage:
    python3 -m voxherd_panel [--host HOST] [--port PORT]
"""

import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from voxherd_panel import APP_ID  # noqa: E402


class VoxHerdPanelApp(Adw.Application):
    """Single-instance GTK4/libadwaita application."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._host = host
        self._port = port

    def do_activate(self) -> None:
        win = self.props.active_window
        if win:
            win.present()
            return

        from voxherd_panel.window import VoxHerdWindow

        win = VoxHerdWindow(application=self, host=self._host, port=self._port)
        win.present()


def main() -> None:
    parser = argparse.ArgumentParser(description="VoxHerd Panel")
    parser.add_argument("--host", default="127.0.0.1", help="Bridge host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7777, help="Bridge port (default: 7777)")
    args = parser.parse_args()

    app = VoxHerdPanelApp(host=args.host, port=args.port)
    app.run(None)


if __name__ == "__main__":
    main()
