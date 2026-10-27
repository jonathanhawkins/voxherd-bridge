"""Bonjour/mDNS service advertisement for the VoxHerd bridge.

Registers a ``_voxherd._tcp`` service so clients can auto-discover the bridge
without manual hostname/IP configuration.

Includes a truncated SHA-256 hash of the auth token in the TXT record so
clients can verify the discovered service before connecting.

### The advertised address goes stale, so it is refreshed
``register`` captures the machine's addresses *once*. A laptop changes address
whenever it changes network, and nothing re-announced it — so the advert kept
pointing at wherever the Mac was when the bridge started. Measured 2026-10-25:
the Mac moved hotspot -> LAN -> another LAN across one afternoon, and a headset
discovering the bridge by name was handed ``172.20.10.3`` hours after that
address stopped existing. It reported "couldn't pair: can't reach VoxHerd
Bridge", which reads as the bridge being down rather than the advert being
wrong, while the live address and the ``.local`` name both answered fine.

:func:`refresh` re-announces when the addresses actually change; the bridge
calls it on a timer. Sleep/wake and a VPN coming up are the same event as far
as this module is concerned.

An alternative considered and not taken: publish only an SRV record pointing at
``<host>.local`` and let the platform's own responder answer the A query, which
is self-maintaining because macOS keeps that record current. It would mean not
publishing our own A records at all, which changes the record shape every
existing client resolves against — a larger change than the bug warrants.
"""

from __future__ import annotations

import hashlib
import socket
from zeroconf import Zeroconf, ServiceInfo

_SERVICE_TYPE = "_voxherd._tcp.local."
_SERVICE_NAME = "VoxHerd Bridge._voxherd._tcp.local."

_zc: Zeroconf | None = None
_info: ServiceInfo | None = None

#: Kept from the first `register` attempt -- including one that *threw* -- so
#: `refresh` can establish an advert that never came up. The bridge starts at
#: login, which is routinely before the network is ready.
_port: int = 7777
_auth_token: str = ""


def _usable(addr: str) -> bool:
    """False for an address no other machine can reach us at.

    Loopback is the obvious one. Link-local (``169.254/16``) is an address an
    interface gave itself when DHCP failed: advertising it costs every client a
    connect timeout on a route that does not exist.
    """
    return not (addr.startswith(("127.", "169.254.")) or addr == "0.0.0.0")


def _interface_addresses() -> list[str]:
    """Every IPv4 address on every interface, straight from the OS.

    This used to be ``getaddrinfo(gethostname())``, which does not answer the
    question. It returns what the *resolver* maps this machine's name to --
    one address -- not what the machine has. Measured 2026-10-25: this Mac was
    on Wi-Fi (``192.168.1.127``) and a tailnet (``100.92.204.77``) and the
    advert carried only the first, so a headset that could not route to the
    Wi-Fi address had nothing else to try. The docstring claimed "every
    non-loopback IPv4 address this machine has" and had never been true.

    ``ifaddr`` is what ``zeroconf`` itself uses to enumerate interfaces, so it
    is already installed; it is pinned in ``requirements.txt`` all the same,
    rather than relied on as somebody else's transitive dependency.
    """
    try:
        import ifaddr

        return [
            ip.ip
            for adapter in ifaddr.get_adapters()
            for ip in adapter.ips
            if ip.is_IPv4 and isinstance(ip.ip, str) and _usable(ip.ip)
        ]
    except Exception:  # noqa: BLE001 - any failure here degrades, never raises
        return []


def _default_route_address() -> list[str]:
    """The address that reaches the internet, as a last resort.

    No packet is sent -- ``connect()`` on a UDP socket only selects a route.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return [s.getsockname()[0]]
        finally:
            s.close()
    except OSError:
        return []


def _local_addresses() -> list[bytes]:
    """Every IPv4 address a client could reach this machine at, packed.

    Three sources, in order of how well they answer the question, each used
    only when the one before it found nothing:

      1. the interfaces themselves (:func:`_interface_addresses`);
      2. the resolver's answer for this machine's name, which is one address
         and the only thing this function used to consult;
      3. the default route, which is also one address but needs no resolver at
         all -- the case this must still work in, since the bridge starts at
         login and may be up before DNS is.

    Ordinary addresses come before CGNAT (``100.64/10``, which is what a
    tailnet hands out): zeroconf publishes all of them and the headset reorders
    for itself (`BridgeRoutes`), but a client that simply takes the first
    should get the one most likely to work.
    """
    found = _interface_addresses()
    if not found:
        try:
            found = [
                info[4][0]
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                if _usable(info[4][0])
            ]
        except (socket.gaierror, OSError):
            found = []
    if not found:
        found = [a for a in _default_route_address() if _usable(a)]

    def cgnat_last(addr: str) -> int:
        octets = addr.split(".")
        try:
            return 1 if octets[0] == "100" and 64 <= int(octets[1]) <= 127 else 0
        except (IndexError, ValueError):
            return 0

    addrs: list[bytes] = []
    for addr in sorted(dict.fromkeys(found), key=cgnat_last):
        try:
            packed = socket.inet_aton(addr)
        except OSError:
            continue
        if packed not in addrs:
            addrs.append(packed)
    # Never return nothing: an empty address set unregisters the service in
    # effect, and `refresh` reads "no addresses" as a transition rather than a
    # move, so it would hold the previous ones anyway.
    return addrs or [socket.inet_aton("127.0.0.1")]


def register(port: int = 7777, auth_token: str = "") -> None:
    """Advertise the bridge on the local network via Bonjour.

    When *auth_token* is provided, a truncated SHA-256 hash is included
    in the TXT record (``tk`` key) so clients can verify the service.
    """
    global _zc, _info, _port, _auth_token

    # Stored before anything can raise: a failed registration still has to be
    # retryable, and these are what a retry needs.
    _port, _auth_token = port, auth_token

    props: dict[str, str] = {"path": "/ws/ios"}
    if auth_token:
        # First 16 hex chars of SHA-256 — enough to verify, not enough to brute-force
        token_hash = hashlib.sha256(auth_token.encode()).hexdigest()[:16]
        props["tk"] = token_hash

    # Built into locals and only published on success. Assigning `_info`
    # first meant a `Zeroconf()` that threw -- no responder, no network yet --
    # left the module claiming to advertise addresses that were never
    # announced, so `published_addresses()` reported a service nobody could
    # find. Its own test caught that.
    info = ServiceInfo(
        _SERVICE_TYPE,
        _SERVICE_NAME,
        addresses=_local_addresses(),
        port=port,
        properties=props,
    )
    zc = Zeroconf()
    try:
        zc.register_service(info)
    except Exception:
        zc.close()
        raise
    _zc, _info = zc, info


def advertised_addresses() -> list[str]:
    """What the advert currently claims, as dotted quads. Empty when not registered."""
    if _info is None:
        return []
    try:
        return [socket.inet_ntoa(a) for a in _info.addresses]
    except (OSError, ValueError):
        return []


def published_addresses() -> list[str]:
    """What a client resolving us right now would actually get.

    Read from the DNS records rather than from ``ServiceInfo.addresses``,
    because those are the thing on the wire and the attribute is only the
    intent. See :func:`refresh` on why the difference matters.
    """
    if _info is None:
        return []
    try:
        return [socket.inet_ntoa(r.address) for r in _info.dns_addresses()]
    except Exception:
        return []


def refresh() -> list[str] | None:
    """Keep the advert true: establish one if absent, re-announce if moved.

    Returns the addresses when it announced something, ``None`` when nothing
    needed doing.

    Never raises. This runs on a timer for the life of the process, and a
    stale advert is a worse outcome than a missed refresh but a far better one
    than a bridge that stopped.

    **It also retries a registration that failed.** The bridge starts at
    login, routinely before the network is up, and `register` raising there
    used to be permanent: nothing re-attempted it, so the bridge stayed
    undiscoverable by name until someone restarted it, while a timer ran every
    30 seconds doing nothing. A timer that cannot recover the thing it is
    watching is worse than no timer, because it reads as if it could.
    """
    if _zc is None or _info is None:
        try:
            register(port=_port, auth_token=_auth_token)
        except Exception:
            return None
        return published_addresses() or None
    try:
        current = _local_addresses()
        # Unordered: `getaddrinfo` applies RFC 6724 ordering, which can shuffle
        # the same addresses as routing shifts. Comparing ordered lists made a
        # multi-homed machine re-announce every tick for no reason.
        if not current or sorted(current) == sorted(_info.addresses):
            return None
        _info.addresses = current
        _zc.update_service(_info)
        return [socket.inet_ntoa(a) for a in current]
    except Exception:
        return None


def unregister() -> None:
    """Remove the Bonjour advertisement."""
    global _zc, _info
    if _zc and _info:
        try:
            _zc.unregister_service(_info)
        finally:
            _zc.close()
    _zc = None
    _info = None
