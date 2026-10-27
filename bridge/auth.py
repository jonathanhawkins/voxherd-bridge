"""Authentication token management and HTTP middleware for the bridge server."""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys

from fastapi import Request
from fastapi.responses import JSONResponse

from bridge.server_state import log_event

# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

_AUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".voxherd", "auth_token")

# Module-level token — set on import and updated by ensure_auth_token().
_AUTH_TOKEN: str | None = None


def _load_auth_token() -> str | None:
    """Load auth token from env var or file. Returns None if auth is disabled."""
    token = os.environ.get("VOXHERD_AUTH_TOKEN")
    if token:
        return token
    try:
        with open(_AUTH_TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    return None


def persist_auth_token(token: str) -> None:
    """Write token to ~/.voxherd/auth_token with mode 0600."""
    os.makedirs(os.path.dirname(_AUTH_TOKEN_FILE), mode=0o700, exist_ok=True)
    fd = os.open(_AUTH_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)


def ensure_auth_token() -> str:
    """Ensure an auth token exists. Auto-generates one if missing.

    Returns the active token.
    """
    global _AUTH_TOKEN
    token = _load_auth_token()
    if not token:
        token = secrets.token_hex(32)
        persist_auth_token(token)
    _AUTH_TOKEN = token
    return token


def get_auth_token() -> str | None:
    """Return the current auth token (or None if auth is disabled)."""
    return _AUTH_TOKEN


# Initialize on import
_AUTH_TOKEN = _load_auth_token()


# ---------------------------------------------------------------------------
# Auth checking
# ---------------------------------------------------------------------------


def _check_auth(request: Request) -> bool:
    """Return True if request is authorized. Always True when token is unset."""
    if not _AUTH_TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        # Compared as bytes: secrets.compare_digest refuses a str with any
        # non-ASCII character and raises TypeError, which propagated out of the
        # middleware as an unhandled 500 plus a traceback. `Authorization: Bearer
        # \xff` from any unauthenticated host was therefore a free way to flood
        # the log and bury the real "Auth rejected" lines.
        return secrets.compare_digest(auth[7:].encode("utf-8", "surrogateescape"),
                                      _AUTH_TOKEN.encode("utf-8", "surrogateescape"))
    return False


# Hosts the bridge will answer to. The Host header is checked because the
# ``X-VoxHerd`` CSRF defence below rests on a browser being unable to send a
# custom header cross-origin -- which stops being true under DNS rebinding. An
# attacker serves a page from a name with a 1 s TTL, repoints it at the bridge's
# address, and from then on the browser considers the requests same-origin: it
# sets any header it likes and reads every response. That path reached the
# token-free pairing endpoints, where a six-digit code is all that stands between
# a web page and the bearer token.
#
# Rebinding cannot forge the Host header, so pinning it closes the hole.
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})

# Extra names this deployment answers to, comma-separated — a reverse proxy
# (`voxherd.example.com`), an /etc/hosts alias, a `.home.arpa` name. Without an
# escape hatch the check below rejects every such deployment with a 421.
_EXTRA_HOSTS_ENV = "VOXHERD_ALLOWED_HOSTS"


#: Names resolved once at startup (see ``refresh_own_names``). Read on every
#: request, so nothing here may block or shell out at request time.
_OWN_NAMES: frozenset[str] = frozenset({"localhost"})


def bonjour_hostname() -> str:
    """The name an mDNS ``.local`` lookup for this machine actually resolves.

    On macOS that is ``LocalHostName``, which is what the system's own
    responder answers for -- **not** ``gethostname()``, which returns the DHCP
    name. The two routinely differ -- ``SomeMac-MBP`` against
    ``SomeMac-MacBook-Pro`` is the usual shape, and is what the development
    Mac here has. Falls back to ``gethostname()`` where there is
    no ``scutil`` (Linux, Windows), where the two are generally the same name.

    ``_local_names`` pins *both*, because the bridge should answer to whatever
    a client ends up sending. This function is for the other direction: when
    the bridge **hands out** a name -- ``/api/connection-info``, and
    ``/api/pair/finish`` which embeds it -- only the resolvable one is any use.
    A client told ``ws://jonathans-mbp.local:7777/ws/ios`` cannot resolve it,
    and the failure reads as the bridge being down. That is what the headset
    reported, with the bridge answering fine on its address the whole time.

    Not cached. ``LocalHostName`` changes when the Mac is renamed, and mDNS
    renames it itself on a name collision (appending ``-2``), so a value read
    once at startup can go wrong while the process runs. Callers on the event
    loop must run this in a thread -- it is a subprocess.
    """
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["/usr/sbin/scutil", "--get", "LocalHostName"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            name = out.stdout.strip().rstrip(".")
            if name:
                return name
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        return socket.gethostname()
    except OSError:
        return "localhost"


def _local_names() -> set[str]:
    """Every name this machine actually answers to, lowercased.

    Note what is NOT here: ``.local`` / ``.internal`` / ``.ts.net`` as *suffix
    wildcards*, which is how this started. The reasoning that an attacker cannot
    make a browser send the victim's address as Host is sound for an IP literal
    and does not transfer to a suffix class: an mDNS responder on the LAN
    controls both halves of ``evil.local``, so it can serve a page on port 7777,
    re-announce the name at the bridge's address, and the resulting requests are
    same-origin with a Host that passes.

    ``socket.getfqdn()`` is not here either, and was: it is
    ``gethostbyaddr(gethostname())``, a DNS round trip the *network* decides. On
    a hostile resolver it returns a name the attacker chose, which lands straight
    in this set -- the same door, reopened one line below where it was closed.
    On this machine it already returns a reverse-DNS artifact, which is the tell.

    ``gethostname()`` alone is also not enough on macOS. It returns the DHCP
    name (e.g. ``SomeMac-MBP``) while Bonjour advertises ``LocalHostName``
    (e.g. ``SomeMac-MacBook-Pro``) -- so ``somemac-mbp.local`` does not resolve,
    ``somemac-macbook-pro.local`` does, and pinning only the former refused the
    name clients actually reach the bridge by. The WebSocket is exempt from this
    middleware, so the symptom was the dashboard working while every REST route
    -- the Reader, pairing, /api/command -- answered 421.
    """
    names: set[str] = {"localhost"}

    def add(raw: object) -> None:
        if not isinstance(raw, str):
            return
        name = raw.strip().rstrip(".").lower()
        if not name:
            return
        names.add(name)
        base = name
        while base.endswith(".local"):
            base = base[: -len(".local")]
        # Only a *bare* host name gets a `.local` sibling synthesised. The pair
        # exists for Bonjour, where `somemac-macbook-pro` and
        # `somemac-macbook-pro.local` are the same machine. Applied to a name
        # that is already qualified -- a tailnet `<host>.<tailnet>.ts.net` --
        # it invents `<host>.<tailnet>.ts.net.local`, which this machine does
        # not answer to and which an mDNS responder on the LAN can claim: the
        # same door the docstring above says was closed for suffix wildcards.
        # Harmless until the tailnet name began being added at all, which is
        # recent.
        if base and "." not in base:
            names.add(base)
            names.add(f"{base}.local")

    try:
        add(socket.gethostname())
    except OSError:
        pass

    # The Bonjour name, which is what a `.local` lookup actually resolves, and
    # the one `/api/connection-info` hands out. Both are pinned: a client may
    # send either, and refusing the one it was told to use is the 421 this
    # whole docstring is about.
    add(bonjour_hostname())

    # The tailnet name, because /api/connection-info hands it to the client as
    # the way to reach the bridge remotely -- refusing it made the bridge
    # contradict its own discovery answer.
    try:
        from bridge.tailscale import detect_tailscale

        # Only the hostname is wanted; `port` just builds a `url` field this
        # ignores, so the default stands rather than a meaningless 0.
        info = detect_tailscale()
        if info:
            add(info.get("hostname"))
    except Exception:  # noqa: BLE001 - see below
        # Deliberately broad. This is a best-effort lookup that shells out to
        # `tailscale`, and it runs during the lifespan: anything it raises that
        # reaches the caller stops the bridge from starting at all. A missing
        # tailnet name costs a 421 for one hostname; an exception here costs the
        # whole server.
        pass

    return names


def refresh_own_names() -> frozenset[str]:
    """Resolve the local names and cache them. Called once, from the lifespan.

    Deliberately not lazy-on-first-request: this shells out to ``scutil`` and
    may consult Tailscale, and the middleware runs on the event loop.
    """
    global _OWN_NAMES
    _OWN_NAMES = frozenset(_local_names())
    return _OWN_NAMES


def _own_names() -> frozenset[str]:
    """The allowed host names: those resolved at startup plus the env override.

    The env var is read fresh rather than baked into the cache, so setting it
    does not depend on having set it before the first request.
    """
    extra = {
        name.strip().rstrip(".").lower()
        for name in os.environ.get(_EXTRA_HOSTS_ENV, "").split(",")
    }
    return _OWN_NAMES | frozenset(n for n in extra if n)


def _host_of(request: Request) -> str:
    """The request's Host header, lowercased, without its port or trailing dot.

    The trailing dot is the fully-qualified form of a name and is what some
    resolvers and proxies pass through: ``Host: localhost.`` means exactly
    ``localhost``. Every name in the allowlist is ``rstrip(".")``-ed, so without
    stripping it here the comparison was asymmetric and a legitimate client
    using the rooted form got a 421 on every REST route while its WebSocket --
    exempt from this middleware -- worked.
    """
    host = (request.headers.get("host") or "").strip().lower()
    if host.startswith("["):  # bracketed IPv6, with or without a port
        return host.split("]")[0] + "]"
    bare = host.split(":")[0]
    # Only when something is left. `Host: .` (or `..`) rstrips to "", which
    # `_is_allowed_host` ACCEPTS as "HTTP/1.0 sent none" -- so the hardening
    # that let the rooted form through also turned a dot into a pass. Not
    # exploitable from a browser (the root zone does not resolve to the victim)
    # but it is one character to not have to argue about.
    stripped = bare.rstrip(".")
    return stripped or bare


def _is_allowed_host(host: str) -> bool:
    """True when *host* is this machine, by a name or address it really has.

    An IP literal is accepted because that is how a phone on the LAN and a
    tailnet peer address the bridge, and an attacker cannot point a hostname they
    control at the victim's address *and* have the browser send that address as
    the Host header.
    """
    if not host:
        # HTTP/1.0 and some local tools send none. Not a browser, so not
        # reachable by rebinding.
        return True
    if host in _ALLOWED_HOSTS:
        return True
    # An IP literal is safe to accept: it is how a phone on the LAN and a
    # tailnet peer address the bridge, and an attacker cannot both point a name
    # they control at the victim's address AND have the browser send that
    # address as the Host header.
    if _IP_LITERAL_RE.match(host):
        return True
    return host in _own_names()


_IP_LITERAL_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$|^\[[0-9a-f:]+\]$")


# The two endpoints that answer without the token (bridge/pairing.py).
_PAIRING_PATHS = frozenset({"/api/pair/start", "/api/pair/finish"})


def _has_csrf_header(request: Request) -> bool:
    return bool(request.headers.get("x-voxherd") or request.headers.get("x-voxheard"))


async def auth_middleware(request: Request, call_next):
    """Reject unauthenticated HTTP requests when auth token is configured.

    Also enforces CSRF protection: POST/PUT/PATCH/DELETE requests must include
    an ``X-VoxHerd`` header. Browsers cannot send custom headers in simple
    CORS requests, so this blocks cross-site request forgery from malicious
    websites.
    """
    # WebSocket upgrades are handled in the WebSocket handler, not here
    if request.scope.get("type") == "websocket":
        return await call_next(request)
    # Host check first, and before the exemptions below: it is what makes the
    # X-VoxHerd header a real CSRF defence, and the endpoints that skip the token
    # are exactly the ones that most need it.
    host = _host_of(request)
    if not _is_allowed_host(host):
        client_host = request.client.host if request.client else "unknown"
        log_event("warning", "bridge", f"Host rejected: {client_host} asked for {host!r}")
        return JSONResponse(status_code=421, content={"error": "Unrecognized Host"})
    # Health check is unauthenticated for monitoring/quick-test
    if request.url.path == "/health":
        return await call_next(request)
    # Brand assets: must be publicly fetchable because Meta's DAT SDK
    # image loader on the lens has no way to attach a bearer token. The
    # PNGs are static, non-sensitive mascot art.
    if request.url.path.startswith("/api/assets/"):
        return await call_next(request)
    # Pairing is how a headset gets the token, so it cannot need it. It is
    # still held to the CSRF header, so a web page cannot drive it; its own
    # limits are in bridge/pairing.py.
    if request.url.path in _PAIRING_PATHS:
        if not _has_csrf_header(request):
            return JSONResponse(status_code=403, content={"error": "Missing X-VoxHerd header"})
        return await call_next(request)
    if not _check_auth(request):
        client_host = request.client.host if request.client else "unknown"
        log_event("warning", "bridge", f"Auth rejected: {client_host} {request.method} {request.url.path}")
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    # CSRF: require custom header on state-changing methods
    # Accept both X-VoxHerd (current) and X-VoxHeard (legacy) for
    # backwards compatibility with older hooks / macOS app.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _has_csrf_header(request):
            client_host = request.client.host if request.client else "unknown"
            log_event("warning", "bridge", f"CSRF rejected: {client_host} {request.method} {request.url.path} (missing X-VoxHerd header)")
            return JSONResponse(status_code=403, content={"error": "Missing X-VoxHerd header"})
    return await call_next(request)
