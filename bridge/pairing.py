"""Pairing a headset: the bridge's token, for a code only the Mac shows.

The iPhone pairs by scanning the Mac's QR code, which carries the token. The
headset does not use its cameras, so it pairs the
way a television pairs a remote. It asks; the bridge shows a six-digit code on
the Mac, as a notification, in its log, and as a card in the menu bar app's
window; the wearer says or keys the code on the headset. The right code within
two minutes returns the bridge's token.

The menu bar card is the one of those three a wearer can be sure of finding.
A notification is transient — a glance away and it is gone — and the log line
scrolls: under ``/Applications/VoxHerdBridge.app`` the bridge runs
``--headless``, so its log is a pipe into the app's 200-line event list, where
the next ten events push the code out of sight. So every change to the book
below is also announced through :func:`set_watcher`, which the headless CLI
wires to a ``{"pairing": {...}}`` line on stdout — the same private pipe that
already carries the ready signal and the log. The app holds the newest state
per request and drops it as soon as the request leaves this book, because a
code still on screen after it stopped working is worse than no code at all:
the wearer will key it in.

Expiry is the one transition the app must not wait to be told about. Nothing
here runs on a timer — ``_expire`` only sweeps when some other pairing call
arrives — so a request that nobody follows up on sits in the book until the
next caller, and a card that waited for its ``resolved`` line would outlive
the code it shows. ``expires_in`` is therefore a deadline, not a hint: the app
counts it down and drops the card when it reaches zero, and the ``expired``
line, when it comes, is only confirmation.

These two endpoints are the only ones that answer without the token, which
makes them the ones to be careful with:

- a code is six random digits, compared in constant time, good for two
  minutes and five tries;
- at most three requests are pending at once;
- twenty wrong codes in fifteen minutes lock pairing for fifteen minutes;
- the requesting device's name reaches the Mac's notification only as an
  ``osascript`` argument, never as script text, and only after being cut to a
  safe alphabet;
- they still need the ``X-VoxHerd`` header, so a web page cannot drive them.

The token itself is never logged. The code is: the log is on the Mac, which is
where the code is meant to be read.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from bridge.env_utils import get_subprocess_env

CODE_DIGITS = 6
CODE_TTL_S = 120.0
MAX_ATTEMPTS = 5
MAX_PENDING = 3
LOCKOUT_FAILURES = 20
LOCKOUT_WINDOW_S = 15 * 60.0
LOCKOUT_S = 15 * 60.0

_DEVICE_RE = re.compile(r"[^A-Za-z0-9 ._-]")
MAX_DEVICE_CHARS = 40
DEFAULT_DEVICE = "a headset"


class PairingError(Exception):
    """A pairing call refused; ``status`` is its HTTP status, ``extra`` goes in the body."""

    def __init__(self, status: int, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


@dataclass
class Request:
    request_id: str
    code: str
    device: str
    client_host: str
    created: float
    attempts: int = 0


@dataclass
class _Book:
    pending: dict[str, Request] = field(default_factory=dict)
    failures: deque = field(default_factory=deque)
    locked_until: float = 0.0


_book = _Book()

# Where book changes go so the Mac can show them. ``cli.py`` installs the
# headless stdout writer; everywhere else this stays a no-op, because a bridge
# run in a terminal already prints the code where the user is looking.
Watcher = Callable[[dict], None]
_watcher: Watcher = lambda event: None


def set_watcher(watcher: Watcher) -> None:
    """Send every change to this book to [watcher] as well."""
    global _watcher
    _watcher = watcher


def _tell(event: dict) -> None:
    """Announce [event], whatever the watcher does with it.

    Swallows everything the watcher raises for the same reason the hooks do:
    nothing about showing a code may stop a headset from pairing.
    """
    try:
        _watcher(event)
    except Exception:
        pass


def pending_event(request: Request, now: float) -> dict:
    """[request] as the Mac shows it while it waits.

    The code is already spaced, so the Mac has no second opinion about how to
    group the digits, and ``host`` travels because it is the only part of a
    request a stranger cannot choose — see :func:`notification`. ``attempts``
    is here so a wearer who has typed nothing can see that somebody has.
    """
    return {
        "state": "pending",
        "request_id": request.request_id,
        "code": spaced(request.code),
        "device": request.device,
        "host": request.client_host,
        "expires_in": max(0.0, CODE_TTL_S - (now - request.created)),
        "attempts": request.attempts,
    }


def resolved_event(request_id: str, reason: str) -> dict:
    """[request_id] is out of the book — ``paired``, ``expired``, ``failed`` or ``locked``."""
    return {"state": "resolved", "request_id": request_id, "reason": reason}


def reset() -> None:
    """Forget every request and lockout (tests).

    Leaves the watcher alone: the Mac's card outlives any one request, and a
    reset that quietly unhooked it would take the screen down for good.
    """
    global _book
    _book = _Book()


def device_name(raw: object) -> str:
    """[raw] as something safe to show: a short run of plain characters."""
    if not isinstance(raw, str):
        return DEFAULT_DEVICE
    name = " ".join(_DEVICE_RE.sub("", raw).split())[:MAX_DEVICE_CHARS].strip()
    return name or DEFAULT_DEVICE


def spaced(code: str) -> str:
    """"471902" as "471 902": how a code is read off a screen."""
    half = len(code) // 2
    return f"{code[:half]} {code[half:]}"


def _expire(now: float) -> None:
    for rid in [rid for rid, r in _book.pending.items() if now - r.created > CODE_TTL_S]:
        del _book.pending[rid]
        _tell(resolved_event(rid, "expired"))
    while _book.failures and now - _book.failures[0] > LOCKOUT_WINDOW_S:
        _book.failures.popleft()


def _check_lock(now: float) -> None:
    if now < _book.locked_until:
        raise PairingError(
            429, "pairing is locked after too many wrong codes; try again later",
            retry_after=int(_book.locked_until - now) + 1,
        )


def start(device: object, client_host: str, now: float) -> Request:
    """A new request, with a code for the Mac to show. Refuses while locked or full."""
    _expire(now)
    _check_lock(now)
    if len(_book.pending) >= MAX_PENDING:
        raise PairingError(429, "too many pairing requests are waiting; try again in a minute")
    request = Request(
        request_id=secrets.token_urlsafe(16),
        code=f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}",
        device=device_name(device),
        client_host=client_host,
        created=now,
    )
    _book.pending[request.request_id] = request
    _tell(pending_event(request, now))
    return request


def finish(request_id: object, code: object, token: str | None, now: float) -> str:
    """The bridge's token for the right code; a :class:`PairingError` otherwise.

    A wrong code costs the request one of its tries and counts towards the
    lockout. A request is gone once used, out of tries, or expired.
    """
    _expire(now)
    _check_lock(now)
    request = _book.pending.get(request_id) if isinstance(request_id, str) else None
    if request is None:
        raise PairingError(410, "that pairing request has expired; start again")
    given = code if isinstance(code, str) else ""
    if secrets.compare_digest(given.encode(), request.code.encode()):
        del _book.pending[request.request_id]
        _tell(resolved_event(request.request_id, "paired"))
        return token or ""
    request.attempts += 1
    _book.failures.append(now)
    if len(_book.failures) >= LOCKOUT_FAILURES:
        _book.locked_until = now + LOCKOUT_S
        stranded = list(_book.pending)
        _book.pending.clear()
        for rid in stranded:
            _tell(resolved_event(rid, "locked"))
        raise PairingError(429, "pairing is locked after too many wrong codes; try again later",
                           retry_after=int(LOCKOUT_S))
    left = MAX_ATTEMPTS - request.attempts
    if left <= 0:
        del _book.pending[request.request_id]
        _tell(resolved_event(request.request_id, "failed"))
        raise PairingError(403, "wrong code, and no tries left; start again", attempts_left=0)
    # Still pending, but with one more wrong try against it than the Mac is
    # showing — and a wrong try the wearer did not make is the thing they most
    # need to see, so re-announce rather than wait for the request to end.
    _tell(pending_event(request, now))
    raise PairingError(403, "wrong code", attempts_left=left)


def notification(request: Request) -> tuple[str, str]:
    """The Mac notification's (title, body) for [request].

    The asking address is in the body because it is the only part of this a
    stranger cannot choose. ``device`` is attacker-supplied, so a notification
    naming only the device looks identical whether the request came from the
    wearer's headset or from someone else on the network — which is what makes a
    relay invisible: an attacker advertising itself over mDNS can forward a
    pairing request to the real bridge, let the wearer read a genuine code off
    their own Mac, and redeem it for the bearer token. The address is captured
    in ``Request.client_host`` and was previously never read again.

    A wearer who can see "asked from 192.168.1.50" has something to compare
    against what their own headset says.
    """
    return (
        "VoxHerd pairing",
        (
            f"Code {spaced(request.code)} pairs {request.device}, "
            f"asked from {request.client_host}. It expires in two minutes."
        ),
    )


async def notify(request: Request) -> None:
    """Show the code on the Mac. Never raises: the log line is the fallback.

    The title and body are arguments to the script, not part of it, so no
    device name can turn into AppleScript.
    """
    if sys.platform != "darwin":
        return
    title, body = notification(request)
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript",
            "-e", "on run argv",
            "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e", "end run",
            title, body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


def now() -> float:
    return time.monotonic()
