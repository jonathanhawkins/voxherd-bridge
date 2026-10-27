"""Tailscale detection utility for VoxHerd bridge server.

### Why the CLI is looked up rather than just run
The Mac App Store and direct-download builds of Tailscale keep their CLI
*inside the app bundle* and put nothing on ``PATH``. On this machine
``/Applications/Tailscale.app`` is installed and online with a 100.x address on
``utun10``, and bare ``tailscale`` is still not resolvable — so a plain
``subprocess.run(["tailscale", ...])`` raised ``FileNotFoundError``, this module
answered ``None``, and two things silently followed:

  - ``/api/connection-info`` never offered the tailnet URL, so a headset was
    never told how to reach the bridge off the LAN;
  - ``auth.py`` never added the tailnet name to the pinned-``Host`` set, so a
    request arriving as ``<name>.ts.net`` was answered **421** — the bridge
    refusing the very address it is supposed to hand out.

``shutil.which`` is the right tool *here*, unlike the rule in ``CLAUDE.md``
about not probing guaranteed system binaries: ``/usr/bin/say`` always exists
and Tailscale genuinely may not, so "is it there" is a real question rather
than fake defence.

The candidate list is deliberately the same one ``NetworkInfo.swift``'s
``isTailscaleInstalled()`` uses, plus the in-bundle executable, so the Swift
app and the Python bridge cannot disagree about whether Tailscale is present.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from bridge.env_utils import get_subprocess_env

#: Where the CLI lives, in the order worth trying. ``PATH`` first, because a
#: deliberate install (Homebrew, the standalone package) belongs ahead of a
#: bundle we guessed at.
CLI_CANDIDATES: tuple[str, ...] = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/local/bin/tailscale",
)


def cli_path() -> str | None:
    """The Tailscale CLI to run, or ``None`` when Tailscale is not installed.

    Never raises: this runs inside the lifespan, where an exception costs the
    whole server rather than one hostname (see ``auth.py``'s note).
    """
    try:
        on_path = shutil.which("tailscale")
        if on_path:
            return on_path
        for candidate in CLI_CANDIDATES:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    except Exception:
        return None
    return None


def is_installed() -> bool:
    """Whether a Tailscale CLI exists at all, regardless of it being up.

    The distinction the onboarding needs: *not installed* wants an install
    prompt, *installed but no address* wants "sign in and connect".
    """
    return cli_path() is not None


def detect_tailscale(port: int = 7777) -> dict | None:
    """Detect Tailscale and return connection info.

    Returns a dict with ``ip``, ``hostname``, and ``url`` keys, or ``None``
    if Tailscale is not installed or not running.
    """
    try:
        cli = cli_path()
        if cli is None:
            return None
        ip_result = subprocess.run(
            [cli, "ip", "-4"],
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
        cli = cli_path()
        if cli is None:
            return None
        result = subprocess.run(
            [cli, "status", "--json"],
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
