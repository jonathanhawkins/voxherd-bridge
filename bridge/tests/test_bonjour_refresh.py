"""The advertised address has to follow the machine.

`register` captures this machine's addresses once. A laptop changes address
whenever it changes network, and nothing re-announced it — so the advert kept
naming wherever the Mac was when the bridge started.

Measured 2026-10-25: the Mac moved hotspot → LAN → another LAN across one
afternoon, and a headset discovering the bridge by name was handed
``172.20.10.3`` hours after that address stopped existing. The headset reported
"couldn't pair: can't reach VoxHerd Bridge", which reads as the bridge being
down rather than the advert being wrong — while the live address and the
``.local`` name both answered fine from the same device.
"""

from __future__ import annotations

import socket

import pytest

from bridge import bonjour
from zeroconf import Zeroconf


class _FakeZeroconf:
    """Records what was announced, so a test can ask what a client would see."""

    def __init__(self) -> None:
        self.registered: list[object] = []
        self.updates = 0
        self.closed = False
        self.unregistered = 0

    def register_service(self, info: object) -> None:
        self.registered.append(info)

    def update_service(self, _info: object) -> None:
        self.updates += 1

    def unregister_service(self, _info: object) -> None:
        self.unregistered += 1

    def close(self) -> None:
        self.closed = True


def _packed(*addrs: str) -> list[bytes]:
    return [socket.inet_aton(a) for a in addrs]


@pytest.fixture
def advertising(monkeypatch):
    """A registered advert on 192.168.1.5, with a fake zeroconf behind it."""
    fake = _FakeZeroconf()
    monkeypatch.setattr(bonjour, "Zeroconf", lambda: fake)
    # Patched whole rather than at one of its three sources: what these tests
    # are about is re-announcing when the answer changes, not how it is found.
    monkeypatch.setattr(bonjour, "_local_addresses", lambda: _packed("192.168.1.5"))
    bonjour.register(port=7777, auth_token="tok")
    yield fake
    monkeypatch.setattr(bonjour, "_local_addresses", lambda: _packed("192.168.1.5"))
    bonjour.unregister()


def test_the_advert_says_where_the_machine_is(advertising):
    assert bonjour.advertised_addresses() == ["192.168.1.5"]


def test_a_move_is_re_announced(advertising, monkeypatch):
    """The bug, as an assertion: the advert must follow the machine.

    The decisive assertion is on `published_addresses()`, which reads the DNS
    records rather than `ServiceInfo.addresses`. The attribute is only the
    intent; the record is what a client resolves. `addresses` has no setter on
    the installed zeroconf, so assigning it works by implementation detail --
    and `requirements.txt` allows `>=0.131,<1.0`. A version that cached its
    records at construction would silently restore the original bug, and a
    test that only checked the attribute (or that `update_service` was called)
    would stay green through it.
    """
    monkeypatch.setattr(bonjour, "_local_addresses", lambda: _packed("10.0.0.9"))

    moved = bonjour.refresh()

    assert moved == ["10.0.0.9"]
    assert advertising.updates == 1
    # Read the DNS records here rather than through a helper, so this cannot
    # be satisfied by a helper that reads the attribute instead. What is being
    # guarded is that mutating `addresses` *propagates into the record a
    # client resolves* -- which is an implementation detail of zeroconf, not a
    # documented setter, across a `>=0.131,<1.0` range.
    records = [socket.inet_ntoa(r.address) for r in bonjour._info.dns_addresses()]
    assert records == ["10.0.0.9"], "the A record on the wire must carry it"
    assert bonjour.published_addresses() == ["10.0.0.9"]
    assert bonjour.advertised_addresses() == ["10.0.0.9"]


def test_a_steady_machine_costs_nothing(advertising):
    """This runs every 30s forever; re-announcing unchanged records is noise."""
    assert bonjour.refresh() is None
    assert bonjour.refresh() is None
    assert advertising.updates == 0


def test_the_same_addresses_in_another_order_are_not_a_move(advertising, monkeypatch):
    """`getaddrinfo` applies RFC 6724 ordering, which shuffles as routing shifts.

    A Mac on Ethernet and Wi-Fi, or with a VPN registering an address for the
    host name, resolves to several. Comparing ordered lists made that machine
    re-announce every 30 seconds for the life of the process -- multicast
    traffic and a log line each tick, with nothing having changed.
    """
    both = _packed("192.168.1.5", "10.0.0.9")
    monkeypatch.setattr(bonjour, "_local_addresses", lambda: both)
    assert bonjour.refresh() is not None, "two addresses where there was one is a move"
    assert advertising.updates == 1

    monkeypatch.setattr(bonjour, "_local_addresses", lambda: list(reversed(both)))
    assert bonjour.refresh() is None, "the same set, reordered, is not"
    assert advertising.updates == 1


def test_a_second_network_is_advertised_too(advertising, monkeypatch):
    """Both addresses, so a client on either one can reach us."""
    monkeypatch.setattr(bonjour, "_local_addresses", lambda: _packed("192.168.1.5", "100.92.204.77"))

    assert bonjour.refresh() == ["192.168.1.5", "100.92.204.77"]
    assert bonjour.advertised_addresses() == ["192.168.1.5", "100.92.204.77"]


def test_losing_every_address_keeps_the_last_known_one(advertising, monkeypatch):
    """Mid-transition the resolver answers nothing; that is not a new address.

    Replacing the advert with an empty address set would make the service
    undiscoverable until the next tick, which is worse than briefly stale.
    """
    monkeypatch.setattr(bonjour, "_local_addresses", list)

    assert bonjour.refresh() is None
    assert advertising.updates == 0
    assert bonjour.advertised_addresses() == ["192.168.1.5"]


def test_a_refresh_never_raises(advertising, monkeypatch):
    """It runs on a timer for the life of the process; raising would end it."""
    def boom() -> list[bytes]:
        raise OSError("the resolver gave up")

    monkeypatch.setattr(bonjour, "_local_addresses", boom)
    assert bonjour.refresh() is None

    def boom_update(_info: object) -> None:
        raise RuntimeError("zeroconf is mid-teardown")

    monkeypatch.setattr(bonjour, "_local_addresses", lambda: _packed("10.0.0.9"))
    monkeypatch.setattr(advertising, "update_service", boom_update)
    assert bonjour.refresh() is None


def test_a_registration_that_failed_is_retried_by_the_timer(monkeypatch):
    """The bridge starts at login, routinely before the network is up.

    `register` raising there used to be permanent: nothing re-attempted it, so
    the bridge stayed undiscoverable by name until a restart, while the refresh
    timer ran every 30 seconds returning early because there was nothing to
    update. A timer that cannot recover the thing it watches is worse than
    none, because it reads as though it could.
    """
    attempts: list[int] = []
    fake = _FakeZeroconf()

    def flaky() -> Zeroconf:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no network yet")
        return fake

    monkeypatch.setattr(bonjour, "Zeroconf", flaky)
    monkeypatch.setattr(bonjour, "_local_addresses", lambda: _packed("192.168.1.5"))
    monkeypatch.setattr(bonjour, "_zc", None)
    monkeypatch.setattr(bonjour, "_info", None)
    monkeypatch.setattr(bonjour, "_port", 7777)
    monkeypatch.setattr(bonjour, "_auth_token", "tok")

    # Startup: the network is not up, and this is caught by the lifespan.
    with pytest.raises(OSError):
        bonjour.register(port=7777, auth_token="tok")
    assert bonjour.published_addresses() == [], "nothing is advertised"

    # The next tick, once the network is there.
    assert bonjour.refresh() == ["192.168.1.5"]
    assert bonjour.published_addresses() == ["192.168.1.5"]
    assert len(fake.registered) == 1, "it registered rather than updating nothing"
    try:
        bonjour.unregister()
    except Exception:
        pass


def test_refreshing_with_no_stored_params_is_not_an_error(monkeypatch):
    """Nothing has ever tried to register, so there is nothing to retry."""
    def refuse() -> Zeroconf:
        raise OSError("no responder")

    monkeypatch.setattr(bonjour, "Zeroconf", refuse)
    monkeypatch.setattr(bonjour, "_zc", None)
    monkeypatch.setattr(bonjour, "_info", None)
    assert bonjour.refresh() is None
    assert bonjour.advertised_addresses() == []
    assert bonjour.published_addresses() == []


# The resolver is now the *second* source: interfaces are asked first, because
# `getaddrinfo(gethostname())` returns one address rather than the machine's
# (see `test_bonjour_addresses.py`). These two keep covering the resolver path,
# which is still what answers when interface enumeration finds nothing -- the
# bridge starts at login and can be up before the network is.


def test_loopback_is_not_advertised(monkeypatch):
    """A client cannot reach us at 127.0.0.1, so it must never be the answer."""
    monkeypatch.setattr(bonjour, "_interface_addresses", list)
    monkeypatch.setattr(
        bonjour.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (None, None, None, None, ("127.0.0.1", 0)),
            (None, None, None, None, ("192.168.1.5", 0)),
        ],
    )
    assert [socket.inet_ntoa(a) for a in bonjour._local_addresses()] == ["192.168.1.5"]


def test_a_duplicated_address_is_advertised_once(monkeypatch):
    """`getaddrinfo` returns one entry per socket type, so duplicates are normal."""
    monkeypatch.setattr(bonjour, "_interface_addresses", list)
    monkeypatch.setattr(
        bonjour.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("192.168.1.5", 0))] * 3,
    )
    assert [socket.inet_ntoa(a) for a in bonjour._local_addresses()] == ["192.168.1.5"]
