"""Entry point for the VoxHerd Windows system tray application.

Usage:
    python -m voxherd_tray
    python -m windows.voxherd_tray
"""

import signal
import sys


def main() -> None:
    from voxherd_tray.tray_app import TrayApp

    app = TrayApp()

    def _handle_sigint(sig: int, frame: object) -> None:
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)

    app.run()


if __name__ == "__main__":
    main()
