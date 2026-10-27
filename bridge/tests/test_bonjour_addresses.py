"""The advert has to carry every address, or a client has nothing to fall back to.

``_local_addresses`` was ``getaddrinfo(gethostname())``, which does not answer
the question it was asked: it returns what the *resolver* maps this machine's
name to -- one address -- not what the machine has. Its docstring claimed
"every non-loopback IPv4 address this machine has" and had never been true.

Measured 2026-10-25: the Mac was on Wi-Fi (``192.168.1.127``) and a tailnet
(``100.92.204.77``); ``dns-sd`` showed the advert carrying only the first. The
headset had just been taught to walk the addresses an advert gives it, and
there was only ever one to walk -- so the fall-through worked and had nothing
to work with. Third appearance of one root cause: ``gethostname()`` is not a
question about this machine's addresses, and on macOS it is not even a name
that resolves.
"""

from __future__ import annotations

import socket

import pytest

from bridge import bonjour


class _Ip:
    def __init__(self, ip: str, v4: bool = True) -> None:
        self.ip = ip
        self.is_IPv4 = v4


class _Adapter:
    def __init__(self, name: str, *ips: _Ip) -> None:
        self.nice_name = name
        self.ips = list(ips)


def _adapters(*adapters: _Adapter):
    return lambda: list(adapters)


def _dotted(packed: list[bytes]) -> list[str]:
    return [socket.inet_ntoa(a) for a in packed]


@pytest.fixture(autouse=True)
def no_real_machine(monkeypatch):
    """Nothing here may depend on the addresses this machine happens to have."""
    def refuse(*_a, **_k):
        raise AssertionError("the resolver must not be consulted when interfaces answered")

    monkeypatch.setattr(bonjour.socket, "getaddrinfo", refuse)


def _with_adapters(monkeypatch, *adapters: _Adapter):
    ifaddr = pytest.importorskip("ifaddr")
    monkeypatch.setattr(ifaddr, "get_adapters", _adapters(*adapters))


# --- what is advertised -----------------------------------------------------


def test_a_multi_homed_mac_advertises_both_addresses(monkeypatch):
    """The bug, as an assertion: this used to be ``['192.168.1.127']``."""
    _with_adapters(
        monkeypatch,
        _Adapter("lo0", _Ip("127.0.0.1")),
        _Adapter("utun10", _Ip("100.92.204.77")),
        _Adapter("en0", _Ip("192.168.1.127")),
    )
    assert _dotted(bonjour._local_addresses()) == ["192.168.1.127", "100.92.204.77"]


def test_a_tailnet_address_goes_after_an_ordinary_one(monkeypatch):
    """A client that takes the first should get the one most likely to work.

    100.64/10 is shared address space, which a tailnet hands out: reachable
    only from the tailnet. Interface order is arbitrary -- ``utun10`` came
    before ``en0`` on this Mac -- so the order has to be chosen, not inherited.
    """
    _with_adapters(monkeypatch, _Adapter("utun10", _Ip("100.64.0.1")), _Adapter("en0", _Ip("10.1.2.3")))
    assert _dotted(bonjour._local_addresses()) == ["10.1.2.3", "100.64.0.1"]

    # 100.128.x merely starts with 100 and is an ordinary address.
    _with_adapters(monkeypatch, _Adapter("en0", _Ip("100.128.0.1")), _Adapter("utun10", _Ip("100.127.255.1")))
    assert _dotted(bonjour._local_addresses()) == ["100.128.0.1", "100.127.255.1"]


def test_loopback_is_not_advertised(monkeypatch):
    """A client cannot reach us at 127.0.0.1, so it must never be the answer."""
    _with_adapters(monkeypatch, _Adapter("lo0", _Ip("127.0.0.1"), _Ip("127.94.0.2")), _Adapter("en0", _Ip("10.0.0.5")))
    assert _dotted(bonjour._local_addresses()) == ["10.0.0.5"]


def test_a_link_local_address_is_not_advertised(monkeypatch):
    """169.254/16 is what an interface gave itself when DHCP failed.

    Nothing routes there, so advertising it spends a client's connect timeout
    for certain -- and the client cannot tell that from a bridge being down.
    """
    _with_adapters(monkeypatch, _Adapter("en1", _Ip("169.254.3.4")), _Adapter("en0", _Ip("10.0.0.5")))
    assert _dotted(bonjour._local_addresses()) == ["10.0.0.5"]


def test_ipv6_is_not_packed_as_ipv4(monkeypatch):
    """`is_IPv4` is false for them; packing one would raise."""
    _with_adapters(
        monkeypatch,
        _Adapter("en0", _Ip("fd7a:115c::1", v4=False), _Ip("192.168.1.127")),
    )
    assert _dotted(bonjour._local_addresses()) == ["192.168.1.127"]


def test_the_same_address_on_two_interfaces_is_advertised_once(monkeypatch):
    _with_adapters(monkeypatch, _Adapter("en0", _Ip("10.0.0.5")), _Adapter("bridge0", _Ip("10.0.0.5")))
    assert _dotted(bonjour._local_addresses()) == ["10.0.0.5"]


# --- the fallbacks, which the bridge starting before the network needs ------


def test_without_interface_enumeration_the_resolver_is_asked(monkeypatch):
    """`ifaddr` is a dependency, not a guarantee -- a frozen bundle can miss it."""
    monkeypatch.setattr(bonjour, "_interface_addresses", list)
    monkeypatch.setattr(bonjour.socket, "gethostname", lambda: "somemac")
    monkeypatch.setattr(
        bonjour.socket, "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("192.168.1.127", 0))],
    )
    assert _dotted(bonjour._local_addresses()) == ["192.168.1.127"]


def test_with_no_resolver_either_the_default_route_answers(monkeypatch):
    """The bridge starts at login, which is routinely before DNS is up."""
    monkeypatch.setattr(bonjour, "_interface_addresses", list)
    monkeypatch.setattr(bonjour.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror()))
    monkeypatch.setattr(bonjour, "_default_route_address", lambda: ["192.168.1.127"])
    assert _dotted(bonjour._local_addresses()) == ["192.168.1.127"]


def test_nothing_at_all_still_returns_an_address(monkeypatch):
    """An empty set would unregister the service in effect.

    `refresh` reads an empty answer as a transition rather than a move and
    keeps the previous addresses, so this only matters at `register` -- where
    `ServiceInfo` with no addresses is a service nobody can resolve.
    """
    monkeypatch.setattr(bonjour, "_interface_addresses", list)
    monkeypatch.setattr(bonjour.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror()))
    monkeypatch.setattr(bonjour, "_default_route_address", list)
    assert _dotted(bonjour._local_addresses()) == ["127.0.0.1"]


def test_a_broken_enumeration_degrades_rather_than_raising(monkeypatch):
    """This runs in the lifespan and on a timer; raising costs the advert."""
    ifaddr = pytest.importorskip("ifaddr")
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: (_ for _ in ()).throw(OSError("no netlink")))
    assert bonjour._interface_addresses() == []
