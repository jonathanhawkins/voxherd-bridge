"""The name the bridge hands out has to be the one that resolves.

``/api/connection-info`` is the bridge's own answer to "where am I?", and
``/api/pair/finish`` embeds it, so it is the first thing a freshly paired
client is told. It built that name from ``socket.gethostname()``, which on
macOS is the **DHCP** name while the mDNS responder answers only for
``LocalHostName``. Measured 2026-10-25 on the development Mac, whose two
names differ in exactly this shape -- ``SomeMac-MBP`` against
``SomeMac-MacBook-Pro`` -- so the endpoint was handing out
``ws://SomeMac-MBP.local:7777/ws/ios`` -- a name nothing on the network
resolves.

The failure mode is the expensive part: the client reports that it cannot
reach the bridge, which reads as the bridge being down, while the bridge is
answering fine on its address. ``auth._local_names`` already knew this (it
pins both names for the ``Host`` check, and its docstring is where the DHCP
distinction is written down); this endpoint did not, so the bridge refused
nothing and simply misdirected every client that trusted its discovery answer.
"""

from __future__ import annotations

import subprocess
from urllib.parse import urlparse

import pytest

from bridge import auth, routes


class _Scutil:
    """Stands in for ``scutil --get LocalHostName``.

    Patched at ``subprocess.run`` rather than by faking ``bonjour_hostname``,
    so the argv is part of what is pinned: asking ``scutil`` for the wrong key
    would read as a pass against a mock of the function.
    """

    def __init__(self, answer: str, *, fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.fail:
            raise OSError("scutil is not here")
        return subprocess.CompletedProcess(argv, 0, stdout=self.answer, stderr="")


@pytest.fixture
def two_names(monkeypatch):
    """A Mac whose DHCP name and Bonjour name differ, as this one's do."""
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.socket, "gethostname", lambda: "SomeMac-MBP")
    scutil = _Scutil("SomeMac-MacBook-Pro\n")
    monkeypatch.setattr(auth.subprocess, "run", scutil)
    return scutil


# --- the name itself --------------------------------------------------------


def test_the_bonjour_name_is_the_one_asked_for(two_names):
    assert auth.bonjour_hostname() == "SomeMac-MacBook-Pro"
    assert two_names.calls == [["/usr/sbin/scutil", "--get", "LocalHostName"]]


def test_no_scutil_falls_back_to_the_system_name(monkeypatch):
    """Linux and Windows have no ``scutil``, and there the two names agree."""
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(auth.socket, "gethostname", lambda: "a-linux-box")
    monkeypatch.setattr(auth.subprocess, "run", _Scutil("", fail=True))

    assert auth.bonjour_hostname() == "a-linux-box"


def test_a_broken_scutil_falls_back_rather_than_raising(monkeypatch):
    """This is reached from an endpoint; raising would 500 the pairing answer."""
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.socket, "gethostname", lambda: "SomeMac-MBP")
    monkeypatch.setattr(auth.subprocess, "run", _Scutil("", fail=True))

    assert auth.bonjour_hostname() == "SomeMac-MBP"


def test_an_empty_answer_is_not_a_hostname(monkeypatch):
    """`scutil` exits 0 with nothing when LocalHostName is unset.

    Taking that at face value produced `ws://.local:7777/ws/ios`, which is a
    syntactically valid URL for no host at all -- so the client's failure moves
    from "cannot resolve" to "cannot parse", and no closer to the truth.
    """
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.socket, "gethostname", lambda: "SomeMac-MBP")
    monkeypatch.setattr(auth.subprocess, "run", _Scutil("\n"))

    assert auth.bonjour_hostname() == "SomeMac-MBP"


# --- what the endpoint hands out -------------------------------------------


async def test_connection_info_hands_out_the_resolvable_name(two_names, monkeypatch):
    """The bug, as an assertion.

    Reverting `connection_info` to `socket.gethostname()` makes this fail with
    `ws://SomeMac-MBP.local:7777/ws/ios` -- the exact string the headset was
    given and could not resolve.
    """
    monkeypatch.setattr(routes, "detect_tailscale", lambda *_a, **_k: None)
    monkeypatch.setattr(routes._state, "server_port", 7777, raising=False)

    info = await routes.connection_info()

    assert info["local"] == "ws://SomeMac-MacBook-Pro.local:7777/ws/ios"
    assert "mbp" not in info["local"].lower(), "the DHCP name resolves nowhere"


async def test_the_name_it_hands_out_is_one_it_answers_to(two_names, monkeypatch):
    """Offering a name the ``Host`` pin then 421s is the other half of this.

    ``_local_names`` adds the Bonjour name because this endpoint hands it out;
    the two have to be read together or the bridge refuses the address it
    advertised. This asserts the loop closes for whatever name is chosen,
    rather than for a name hardcoded here.
    """
    monkeypatch.setattr(routes, "detect_tailscale", lambda *_a, **_k: None)
    monkeypatch.setattr(routes._state, "server_port", 7777, raising=False)
    monkeypatch.setattr("bridge.tailscale.detect_tailscale", lambda *_a, **_k: None)

    offered = urlparse((await routes.connection_info())["local"]).hostname
    assert offered is not None

    # Recorded for teardown *before* `refresh_own_names` writes the global, or
    # this test leaves its fake names in `_OWN_NAMES` for the rest of the run.
    monkeypatch.setattr(auth, "_OWN_NAMES", auth._OWN_NAMES)
    auth.refresh_own_names()
    assert auth._is_allowed_host(offered), f"the bridge 421s the name it offered: {offered}"


async def test_an_mdns_fqdn_does_not_become_local_local(monkeypatch):
    """`scutil` can answer with a qualified name; `mdns_hostname` absorbs it."""
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.subprocess, "run", _Scutil("SomeMac-MacBook-Pro.local\n"))
    monkeypatch.setattr(routes, "detect_tailscale", lambda *_a, **_k: None)
    monkeypatch.setattr(routes._state, "server_port", 7777, raising=False)

    info = await routes.connection_info()

    assert info["local"] == "ws://SomeMac-MacBook-Pro.local:7777/ws/ios"
