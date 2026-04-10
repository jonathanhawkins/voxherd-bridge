"""Tailscale detection utility for VoxHerd bridge server."""

from __future__ import annotations

import json
import subprocess

from bridge.env_utils import get_subprocess_env


def detect_tailscale(port: int = 7777) -> dict | None:
    """Detect Tailscale and return connection info.

    Returns a dict with ``ip``, ``hostname``, and ``url`` keys, or ``None``
    if Tailscale is not installed or not running.
    """
    try:
        ip_result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
            env=get_subprocess_env(),
        )
        if ip_result.returncode != 0 or not ip_result.stdout.strip():
            return None
        ip = ip_result.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    hostname = _get_hostname()
    url = f"ws://{hostname or ip}:{port}/ws/ios"

    return {"ip": ip, "hostname": hostname or ip, "url": url}


def _get_hostname() -> str | None:
    """Extract the Tailscale hostname from ``tailscale status --json``."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
            env=get_subprocess_env(),
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        # Self node info contains the DNS name
        self_node = data.get("Self", {})
        dns_name = self_node.get("DNSName", "")
        # DNSName looks like "macbook.tail12345.ts.net." — strip trailing dot
        if dns_name:
            return dns_name.rstrip(".")
        # Fallback to HostName field
        return self_node.get("HostName") or None
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
