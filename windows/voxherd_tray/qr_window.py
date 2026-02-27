"""QR code window for iOS device pairing.

Generates a QR code containing a voxherd:// deep link URL with connection
details (host, port, auth token, Tailscale IP) that the iOS app can scan.

Uses Toplevel (not Tk) so it can share the single Tk root managed by TrayApp.
"""

import tkinter as tk
from typing import Callable

import qrcode
from PIL import Image, ImageTk

from voxherd_tray.network_utils import get_local_ip, get_tailscale_ip


class QRWindow:
    """Tkinter Toplevel window displaying a QR code for iOS pairing."""

    def __init__(
        self,
        root: tk.Tk,
        port: int = 7777,
        auth_token: str | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._port = port
        self._auth_token = auth_token
        self._on_close = on_close
        self._window: tk.Toplevel | None = None
        self._auto_close_id: str | None = None

        self._create(root)

    def _create(self, root: tk.Tk) -> None:
        """Create and display the QR code window."""
        self._window = tk.Toplevel(root)
        self._window.title("VoxHerd - Pair iOS Device")
        self._window.resizable(False, False)
        self._window.configure(bg="#1e1e1e")
        self._window.protocol("WM_DELETE_WINDOW", self._close)

        # Build the connection URL
        host = get_local_ip()
        ts_ip = get_tailscale_ip()
        payload = self._build_url(host, ts_ip)

        # Generate QR code image
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        # Convert to tkinter-compatible image (must keep reference)
        self._tk_image = ImageTk.PhotoImage(qr_image)

        # Layout
        frame = tk.Frame(self._window, bg="#1e1e1e", padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        title = tk.Label(
            frame,
            text="Scan with VoxHerd iOS App",
            font=("Segoe UI", 14, "bold"),
            fg="white",
            bg="#1e1e1e",
        )
        title.pack(pady=(0, 10))

        qr_label = tk.Label(frame, image=self._tk_image, bg="#1e1e1e")
        qr_label.pack(pady=(0, 10))

        # Connection info
        info_frame = tk.Frame(frame, bg="#2d2d2d", padx=10, pady=8)
        info_frame.pack(fill=tk.X, pady=(0, 5))

        self._info_label(info_frame, "Host:", host)
        if ts_ip:
            self._info_label(info_frame, "Tailscale:", ts_ip)
        self._info_label(info_frame, "Port:", str(self._port))
        if self._auth_token:
            preview = self._auth_token[:8] + "..." + self._auth_token[-4:]
            self._info_label(info_frame, "Token:", preview)

        hint = tk.Label(
            frame,
            text="Auto-closes in 60 seconds",
            font=("Segoe UI", 9),
            fg="#888888",
            bg="#1e1e1e",
        )
        hint.pack(pady=(5, 0))

        # Auto-close after 60 seconds
        self._auto_close_id = self._window.after(60_000, self._close)

        # Center on screen
        self._window.update_idletasks()
        w = self._window.winfo_width()
        h = self._window.winfo_height()
        x = (self._window.winfo_screenwidth() - w) // 2
        y = (self._window.winfo_screenheight() - h) // 2
        self._window.geometry(f"+{x}+{y}")

    def focus(self) -> None:
        """Bring the window to front."""
        if self._window and self._window.winfo_exists():
            self._window.lift()
            self._window.focus_force()
        else:
            raise RuntimeError("Window closed")

    def _close(self) -> None:
        """Close the window and notify the parent."""
        if self._auto_close_id and self._window:
            self._window.after_cancel(self._auto_close_id)
        if self._window:
            self._window.destroy()
            self._window = None
        if self._on_close:
            self._on_close()

    def _build_url(self, host: str, tailscale_ip: str | None) -> str:
        """Build the voxherd://connect?... deep link URL."""
        import urllib.parse

        params: dict[str, str] = {
            "host": host,
            "port": str(self._port),
        }
        if self._auth_token:
            params["token"] = self._auth_token
        if tailscale_ip:
            params["tailscale"] = tailscale_ip

        query = urllib.parse.urlencode(params)
        return f"voxherd://connect?{query}"

    @staticmethod
    def _info_label(parent: tk.Frame, label: str, value: str) -> None:
        """Add a key: value row to the info panel."""
        row = tk.Frame(parent, bg="#2d2d2d")
        row.pack(fill=tk.X, pady=1)

        lbl = tk.Label(
            row,
            text=label,
            font=("Segoe UI", 9),
            fg="#aaaaaa",
            bg="#2d2d2d",
            anchor="w",
            width=10,
        )
        lbl.pack(side=tk.LEFT)

        val = tk.Label(
            row,
            text=value,
            font=("Consolas", 9),
            fg="white",
            bg="#2d2d2d",
            anchor="w",
        )
        val.pack(side=tk.LEFT, fill=tk.X, expand=True)
