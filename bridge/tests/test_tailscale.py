"""Finding Tailscale, and what depends on finding it.

Tailscale is how the headset reaches the bridge off the LAN, and two things
silently stop working when this module answers ``None``:

  - ``/api/connection-info`` stops offering the tailnet URL, so a client is
    never told the remote address;
  - ``auth.py`` stops adding the tailnet name to the pinned-``Host`` set, so a
    request arriving under that name is answered **421** — the bridge refusing
    the address it is meant to hand out.

Both failures are silent, which is why they survived: Tailscale was installed,
online, and holding a 100.x address on ``utun10`` while the bridge reported it
absent, because the Mac App Store build keeps its CLI inside the app bundle and
puts nothing on ``PATH``.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from bridge import tailscale


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    """Nothing here may depend on whether this machine runs Tailscale."""
    monkeypatch.setattr(tailscale.shutil, "which", lambda _name: None)
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", ())


def _fake_cli(tmp_path, ip: str = "100.92.204.77", dns: str = "mac.tailnet.ts.net."):
    """An executable that answers the two subcommands this module runs."""
    script = tmp_path / "tailscale"
    payload = json.dumps({"Self": {"DNSName": dns, "HostName": "mac"}})
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "ip" ]; then printf "%s\\n" ' + f"'{ip}'" + "; exit 0; fi\n"
        'if [ "$1" = "status" ]; then cat <<\'EOF\'\n' + payload + "\nEOF\nexit 0; fi\n"
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


# --- finding it -------------------------------------------------------------


def test_path_wins_over_a_bundle_we_guessed_at(monkeypatch, tmp_path):
    """A deliberate install belongs ahead of a hardcoded guess."""
    chosen = _fake_cli(tmp_path)
    monkeypatch.setattr(tailscale.shutil, "which", lambda _n: chosen)
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",))
    assert tailscale.cli_path() == chosen


def test_the_app_bundle_is_found_when_path_has_nothing(monkeypatch, tmp_path):
    """The case that was broken: installed, online, and invisible to the bridge."""
    bundled = _fake_cli(tmp_path)
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", (bundled,))
    assert tailscale.cli_path() == bundled
    assert tailscale.is_installed() is True


def test_a_candidate_that_is_not_executable_is_not_the_cli(monkeypatch, tmp_path):
    plain = tmp_path / "tailscale"
    plain.write_text("not executable\n")
    plain.chmod(plain.stat().st_mode & ~stat.S_IEXEC & ~stat.S_IXGRP & ~stat.S_IXOTH)
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", (str(plain),))
    assert tailscale.cli_path() is None


def test_a_candidate_that_does_not_exist_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", (str(tmp_path / "absent"),))
    assert tailscale.cli_path() is None
    assert tailscale.is_installed() is False


def test_not_installed_is_an_answer_rather_than_an_error():
    """This runs in the lifespan, where raising costs the whole server."""
    assert tailscale.cli_path() is None
    assert tailscale.is_installed() is False
    assert tailscale.detect_tailscale() is None


def test_a_broken_lookup_degrades_to_not_installed(monkeypatch):
    def boom(_name):
        raise OSError("the filesystem said no")

    monkeypatch.setattr(tailscale.shutil, "which", boom)
    assert tailscale.cli_path() is None
    assert tailscale.detect_tailscale() is None


# --- using it ---------------------------------------------------------------


def test_the_url_it_hands_out_names_the_tailnet_not_the_ip(monkeypatch, tmp_path):
    """The hostname is the point: it is what survives a DHCP lease changing."""
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", (_fake_cli(tmp_path),))
    info = tailscale.detect_tailscale()
    assert info is not None
    assert info["ip"] == "100.92.204.77"
    assert info["hostname"] == "mac.tailnet.ts.net", "the trailing dot is stripped"
    assert info["url"] == "ws://mac.tailnet.ts.net:7777/ws/ios"


def test_the_port_reaches_the_url(monkeypatch, tmp_path):
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", (_fake_cli(tmp_path),))
    assert tailscale.detect_tailscale(port=8888)["url"].endswith(":8888/ws/ios")


def test_no_hostname_falls_back_to_the_address(monkeypatch, tmp_path):
    """An address still reaches the bridge; the IP-literal Host rule allows it."""
    monkeypatch.setattr(tailscale, "CLI_CANDIDATES", (_fake_cli(tmp_path, dns=""),))
    info = tailscale.detect_tailscale()
    # DNSName empty -> HostName, which the fake reports as "mac".
    assert info["hostname"] == "mac"


# --- what depends on it: the pinned Host set --------------------------------


def test_a_tailnet_name_does_not_get_an_invented_local_sibling(monkeypatch):
    """`<host>.<tailnet>.ts.net.local` is a name this machine does not answer to.

    The `.local` pair exists for Bonjour, where the bare name and the `.local`
    name are the same machine. Synthesising one for an already-qualified name
    puts a name in the pin that an mDNS responder on the LAN can claim --
    exactly the suffix-wildcard door `_local_names` documents closing. It was
    unreachable until the tailnet name started being added at all.
    """
    from bridge import auth

    monkeypatch.setattr(auth.socket, "gethostname", lambda: "somemac-mbp")
    monkeypatch.setattr(
        auth, "detect_tailscale", lambda: {"hostname": "mac.tailnet.ts.net"}, raising=False
    )
    monkeypatch.setattr(
        "bridge.tailscale.detect_tailscale",
        lambda: {"hostname": "mac.tailnet.ts.net", "ip": "100.1.1.1", "url": ""},
    )
    names = auth._local_names()

    assert "mac.tailnet.ts.net" in names, "the real tailnet name is still pinned"
    assert "mac.tailnet.ts.net.local" not in names
    # And the Bonjour pair it exists for is untouched.
    assert "somemac-mbp" in names
    assert "somemac-mbp.local" in names
