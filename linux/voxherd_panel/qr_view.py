"""QR code generation and display for iOS app pairing."""

import json
import os
import socket
import subprocess
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

_AUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".voxherd", "auth_token")


def _load_token() -> str:
    try:
        with open(_AUTH_TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _detect_lan_ip() -> str:
    """Get the primary LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _detect_tailscale_ip() -> str | None:
    """Get the Tailscale IPv4 address, if available."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _build_qr_url(host: str, port: int) -> str:
    """Build the voxherd://connect URL for pairing."""
    token = _load_token()
    lan_ip = _detect_lan_ip()
    # Use lan_ip as default host if localhost was provided
    actual_host = lan_ip if host in ("127.0.0.1", "localhost") else host

    params = {"host": actual_host, "port": str(port), "token": token}
    ts_ip = _detect_tailscale_ip()
    if ts_ip:
        params["tailscale"] = ts_ip

    query = urllib.parse.urlencode(params)
    return f"voxherd://connect?{query}"


class QRPage(Gtk.Box):
    """QR code display for iOS app pairing."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7777) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._host = host
        self._port = port
        self._auto_hide_id: int | None = None

        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.set_valign(Gtk.Align.CENTER)

        # Title
        title = Gtk.Label(label="Pair iOS App")
        title.add_css_class("title-2")
        self.append(title)

        subtitle = Gtk.Label(label="Scan this QR code with the VoxHerd iOS app")
        subtitle.add_css_class("dim-label")
        self.append(subtitle)

        # QR image placeholder
        self._qr_picture = Gtk.Picture()
        self._qr_picture.set_size_request(250, 250)
        self._qr_picture.set_halign(Gtk.Align.CENTER)
        self.append(self._qr_picture)

        # Token display
        token = _load_token()
        if token:
            token_box = Gtk.Box(spacing=8)
            token_box.set_halign(Gtk.Align.CENTER)

            token_display = f"{token[:8]}...{token[-4:]}" if len(token) > 12 else token
            token_label = Gtk.Label(label=token_display)
            token_label.add_css_class("monospace")
            token_label.add_css_class("dim-label")
            token_box.append(token_label)

            copy_btn = Gtk.Button(label="Copy")
            copy_btn.add_css_class("flat")
            copy_btn.connect("clicked", self._on_copy_token)
            token_box.append(copy_btn)

            self.append(token_box)

        # Refresh button
        refresh_btn = Gtk.Button(label="Refresh QR Code")
        refresh_btn.add_css_class("pill")
        refresh_btn.set_halign(Gtk.Align.CENTER)
        refresh_btn.connect("clicked", lambda _: self._generate_qr())
        self.append(refresh_btn)

        # Auto-hide label
        self._hide_label = Gtk.Label(label="")
        self._hide_label.add_css_class("caption")
        self._hide_label.add_css_class("dim-label")
        self.append(self._hide_label)

        # Generate QR on init
        self._generate_qr()

    def _generate_qr(self) -> None:
        """Generate and display the QR code."""
        url = _build_qr_url(self._host, self._port)

        try:
            import qrcode
            from PIL import Image as PILImage
        except ImportError:
            self._qr_picture.set_paintable(None)
            self._hide_label.set_label(
                "Install python-qrcode and python-pillow for QR codes"
            )
            return

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        # Convert PIL Image -> GdkPixbuf -> Gtk.Picture
        img = img.convert("RGB")
        width, height = img.size
        data = img.tobytes()
        pixbuf = GdkPixbuf.Pixbuf.new_from_data(
            data,
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            width,
            height,
            width * 3,
        )
        # Scale up for visibility
        pixbuf = pixbuf.scale_simple(250, 250, GdkPixbuf.InterpType.NEAREST)
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        self._qr_picture.set_paintable(texture)

        # Auto-hide after 60s
        if self._auto_hide_id:
            GLib.source_remove(self._auto_hide_id)
        self._hide_label.set_label("QR code auto-hides in 60 seconds")
        self._auto_hide_id = GLib.timeout_add_seconds(60, self._auto_hide)

    def _auto_hide(self) -> bool:
        self._qr_picture.set_paintable(None)
        self._hide_label.set_label("QR code hidden. Click Refresh to show again.")
        self._auto_hide_id = None
        return False  # one-shot

    def _on_copy_token(self, _btn: Gtk.Button) -> None:
        token = _load_token()
        if not token:
            return
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(token)
        _btn.set_label("Copied!")
        # Reset label after 2s, and clear clipboard after 30s
        GLib.timeout_add_seconds(2, lambda: (_btn.set_label("Copy"), False)[-1])
        GLib.timeout_add_seconds(30, lambda: (clipboard.set(""), False)[-1])
