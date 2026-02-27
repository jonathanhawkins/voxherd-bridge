"""Network detection utilities for VoxHerd Windows tray app."""

import json
import socket
import subprocess
from pathlib import Path


def get_local_ip() -> str:
    """Get the primary LAN IP address using the UDP socket trick.

    Connects a UDP socket to an external address (no actual traffic)
    to determine which local interface the OS would use.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except (OSError, socket.error):
        return "127.0.0.1"


def get_tailscale_ip() -> str | None:
    """Get the Tailscale IPv4 address if available.

    Checks the standard Windows install path and PATH for the tailscale CLI.
    """
    tailscale_paths = [
        r"C:\Program Files\Tailscale\tailscale.exe",
        "tailscale",  # Fall back to PATH
    ]

    for tailscale_bin in tailscale_paths:
        try:
            result = subprocess.run(
                [tailscale_bin, "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=_creation_flags(),
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

    return None


def get_tailscale_hostname() -> str | None:
    """Get the Tailscale hostname from ``tailscale status --json``."""
    tailscale_paths = [
        r"C:\Program Files\Tailscale\tailscale.exe",
        "tailscale",
    ]

    for tailscale_bin in tailscale_paths:
        try:
            result = subprocess.run(
                [tailscale_bin, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=_creation_flags(),
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            self_node = data.get("Self", {})
            dns_name = self_node.get("DNSName", "")
            if dns_name:
                return dns_name.rstrip(".")
            return self_node.get("HostName") or None
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            continue

    return None


def _creation_flags() -> int:
    """Return CREATE_NO_WINDOW flag on Windows, 0 otherwise.

    Prevents console windows from flashing when spawning subprocesses.
    """
    try:
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except AttributeError:
        return 0
