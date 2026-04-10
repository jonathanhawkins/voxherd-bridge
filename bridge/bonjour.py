"""Bonjour/mDNS service advertisement for the VoxHerd bridge.

Registers a ``_voxherd._tcp`` service so iOS clients can
auto-discover the bridge without manual hostname/IP configuration.

Includes a truncated SHA-256 hash of the auth token in the TXT record
so iOS clients can verify the discovered service before connecting.
"""

from __future__ import annotations

import hashlib
import socket
from zeroconf import Zeroconf, ServiceInfo

_zc: Zeroconf | None = None
_info: ServiceInfo | None = None


def register(port: int = 7777, auth_token: str = "") -> None:
    """Advertise the bridge on the local network via Bonjour.

    When *auth_token* is provided, a truncated SHA-256 hash is included
    in the TXT record (``tk`` key) so iOS clients can verify the service.
    """
    global _zc, _info

    hostname = socket.gethostname()
    # Get all local IPv4 addresses (skip loopback)
    addrs: list[bytes] = []
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                addrs.append(socket.inet_aton(addr))
    except socket.gaierror:
        pass
    # Fallback: try connecting to a public IP to find the default interface
    if not addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            addrs.append(socket.inet_aton(s.getsockname()[0]))
            s.close()
        except Exception:
            addrs.append(socket.inet_aton("127.0.0.1"))

    props: dict[str, str] = {"path": "/ws/ios"}
    if auth_token:
        # First 16 hex chars of SHA-256 — enough to verify, not enough to brute-force
        token_hash = hashlib.sha256(auth_token.encode()).hexdigest()[:16]
        props["tk"] = token_hash

    _info = ServiceInfo(
        "_voxherd._tcp.local.",
        f"VoxHerd Bridge._voxherd._tcp.local.",
        addresses=addrs,
        port=port,
        properties=props,
    )
    _zc = Zeroconf()
    _zc.register_service(_info)


def unregister() -> None:
    """Remove the Bonjour advertisement."""
    global _zc, _info
    if _zc and _info:
        _zc.unregister_service(_info)
        _zc.close()
    _zc = None
    _info = None
