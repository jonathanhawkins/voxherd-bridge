"""Pairing a headset (bridge/pairing.py): a code on the Mac, for the bridge's token.

These are the only endpoints that answer without the token, so what they refuse
matters more than what they do: a guessed code, a stale request, a flood, a
device name built to become AppleScript. The limits are pinned first, then the
routes, then that the exemption stayed exactly two paths wide.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bridge import pairing
from bridge.pairing import PairingError
from bridge.tests.conftest import TEST_AUTH_TOKEN, auth_headers

CSRF = {"X-VoxHerd": "1"}


@pytest.fixture(autouse=True)
def _fresh():
    """An empty book and nobody watching, before and after each test.

    ``pairing.set_watcher`` is module state that ``reset()`` deliberately
    leaves alone, so unhooking it is this fixture's job -- otherwise a watcher
    installed here reaches the next test, and the one in
    ``test_a_watcher_that_throws_cannot_stop_a_pairing`` raises.
    """
    pairing.reset()
    pairing.set_watcher(lambda event: None)
    yield
    pairing.reset()
    pairing.set_watcher(lambda event: None)


def refused(call) -> PairingError:
    with pytest.raises(PairingError) as caught:
        call()
    return caught.value


class TestCodes:

    def test_a_request_has_six_random_digits_and_an_unguessable_id(self):
        a = pairing.start("Test Headset", "10.0.0.5", now=0)
        b = pairing.start("Test Headset", "10.0.0.5", now=0)
        assert len(a.code) == 6 and a.code.isdigit()
        assert a.request_id != b.request_id and len(a.request_id) >= 16

    def test_the_right_code_buys_the_token_once(self):
        r = pairing.start("Test Headset", "10.0.0.5", now=0)
        assert pairing.finish(r.request_id, r.code, "tok", now=10) == "tok"
        assert refused(lambda: pairing.finish(r.request_id, r.code, "tok", now=11)).status == 410

    def test_five_wrong_codes_burn_the_request(self):
        r = pairing.start("Test Headset", "10.0.0.5", now=0)
        wrong = "000000" if r.code != "000000" else "111111"
        left = [refused(lambda: pairing.finish(r.request_id, wrong, "tok", now=1)).extra["attempts_left"]
                for _ in range(pairing.MAX_ATTEMPTS)]
        assert left == [4, 3, 2, 1, 0]
        assert refused(lambda: pairing.finish(r.request_id, r.code, "tok", now=2)).status == 410

    def test_a_code_expires(self):
        r = pairing.start("Test Headset", "10.0.0.5", now=0)
        assert pairing.finish(r.request_id, r.code, "tok", now=pairing.CODE_TTL_S - 1) == "tok"
        late = pairing.start("Test Headset", "10.0.0.5", now=0)
        assert refused(lambda: pairing.finish(late.request_id, late.code, "tok", now=pairing.CODE_TTL_S + 1)).status == 410

    def test_only_a_few_requests_wait_at_once(self):
        for _ in range(pairing.MAX_PENDING):
            pairing.start("x", "10.0.0.5", now=0)
        assert refused(lambda: pairing.start("x", "10.0.0.5", now=0)).status == 429
        # Expired ones make room.
        assert pairing.start("x", "10.0.0.5", now=pairing.CODE_TTL_S + 1)

    @staticmethod
    def wrong_codes(count: int, t: float) -> list[PairingError]:
        """[count] wrong codes at time [t], each request used for all its tries, as a flood would."""
        errors: list[PairingError] = []
        request = None
        while len(errors) < count:
            if request is None or request.request_id not in pairing._book.pending:
                request = pairing.start("x", "10.0.0.5", now=t)
            bad = "000000" if request.code != "000000" else "111111"
            rid = request.request_id
            errors.append(refused(lambda rid=rid, bad=bad: pairing.finish(rid, bad, "tok", now=t)))
        return errors

    def test_a_flood_of_wrong_codes_locks_pairing(self):
        errors = self.wrong_codes(pairing.LOCKOUT_FAILURES, t=0)
        assert [e.status for e in errors[:-1]] == [403] * (pairing.LOCKOUT_FAILURES - 1)
        assert errors[-1].status == 429
        # Locked, for a new request and a late answer too, until the lockout ends.
        assert refused(lambda: pairing.start("x", "10.0.0.5", now=100)).status == 429
        assert refused(lambda: pairing.finish("any", "000000", "tok", now=100)).status == 429
        assert pairing.start("x", "10.0.0.5", now=pairing.LOCKOUT_S + 1)

    def test_old_failures_do_not_count_towards_the_lock(self):
        self.wrong_codes(pairing.LOCKOUT_FAILURES - 1, t=0)
        later = pairing.LOCKOUT_WINDOW_S + 1
        assert [e.status for e in self.wrong_codes(1, t=later)] == [403]

    def test_a_code_that_is_not_a_string_is_just_wrong(self):
        r = pairing.start("x", "10.0.0.5", now=0)
        assert refused(lambda: pairing.finish(r.request_id, 123456, "tok", now=1)).status == 403
        assert refused(lambda: pairing.finish(None, r.code, "tok", now=1)).status == 410


class TestTheMacsSide:

    def test_a_device_name_is_cut_to_plain_characters(self):
        assert pairing.device_name('Headset" & do shell script "rm -rf ~') == "Headset do shell script rm -rf"
        assert pairing.device_name("x" * 100) == "x" * pairing.MAX_DEVICE_CHARS
        assert pairing.device_name(None) == pairing.DEFAULT_DEVICE
        assert pairing.device_name("   ") == pairing.DEFAULT_DEVICE

    def test_the_notification_reads_the_code_as_a_screen_shows_it(self):
        r = pairing.start("Test Headset", "10.0.0.5", now=0)
        title, body = pairing.notification(r)
        assert title == "VoxHerd pairing"
        assert f"{r.code[:3]} {r.code[3:]}" in body and "Test Headset" in body

    async def test_the_device_name_is_an_argument_never_script(self, monkeypatch):
        monkeypatch.setattr(pairing.sys, "platform", "darwin")
        r = pairing.start('Headset"; beep', "10.0.0.5", now=0)
        proc = AsyncMock()
        proc.wait = AsyncMock(return_value=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
            await pairing.notify(r)
        args = spawn.await_args.args
        assert args[0] == "/usr/bin/osascript"
        script = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert script == ["on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run"]
        assert all("Headset" not in part for part in script)
        assert args[-1] == pairing.notification(r)[1]


class TestRoutes:

    @pytest.fixture(autouse=True)
    def _no_real_notification(self, monkeypatch):
        """The route shows a real Mac notification; a test run must not."""
        monkeypatch.setattr(pairing, "notify", AsyncMock())

    async def test_a_headset_pairs_without_the_token(self, client):
        started = await client.post("/api/pair/start", headers=CSRF, json={"device": "Test Headset"})
        assert started.status_code == 200
        body = started.json()
        assert set(body) == {"request_id", "expires_in", "digits"} and body["digits"] == 6
        code = pairing._book.pending[body["request_id"]].code
        finished = await client.post("/api/pair/finish", headers=CSRF,
                                     json={"request_id": body["request_id"], "code": code})
        assert finished.status_code == 200
        paired = finished.json()
        assert paired["token"] == TEST_AUTH_TOKEN
        assert paired["local"].startswith("ws://")
        # The *only* place a headset is told the Mac's Bonjour name and its
        # tailnet name: nothing asks again, and the Android client keeps them
        # as the addresses to fall back on when the one it paired at stops
        # working (`PairingClient.otherHosts`, `BridgeRoutes`). A field dropped
        # from here is a headset that cannot reach the bridge the day its DHCP
        # lease changes -- and the tailnet name is the one that survives
        # changing network at all, so it is the one that matters most.
        assert "tailscale" in paired, "the client reads this; absent is not the same as null"
        assert paired["tailscale"] is None or set(paired["tailscale"]) == {"ip", "hostname"}

    async def test_a_wrong_code_says_how_many_tries_are_left(self, client):
        body = (await client.post("/api/pair/start", headers=CSRF, json={})).json()
        code = pairing._book.pending[body["request_id"]].code
        wrong = "000000" if code != "000000" else "111111"
        resp = await client.post("/api/pair/finish", headers=CSRF, json={"request_id": body["request_id"], "code": wrong})
        assert resp.status_code == 403
        assert resp.json() == {"error": "wrong code", "attempts_left": 4}

    async def test_the_code_is_logged_on_the_mac_and_the_token_never_is(self, client):
        lines = []
        with patch("bridge.routes.log_event", lambda level, project, msg: lines.append(msg)):
            body = (await client.post("/api/pair/start", headers=CSRF, json={"device": "Test Headset"})).json()
            code = pairing._book.pending[body["request_id"]].code
            await client.post("/api/pair/finish", headers=CSRF, json={"request_id": body["request_id"], "code": code})
        assert any(f"{code[:3]} {code[3:]}" in line for line in lines)
        assert not any(TEST_AUTH_TOKEN in line for line in lines)

    async def test_pairing_still_needs_the_csrf_header(self, client):
        assert (await client.post("/api/pair/start", json={})).status_code == 403
        assert (await client.post("/api/pair/finish", json={})).status_code == 403

    async def test_the_exemption_is_exactly_two_paths(self, client):
        # Neighbours of the exempt paths still need the token.
        for path in ("/api/pair", "/api/pair/startx", "/api/pair/start/", "/api/sessions"):
            assert (await client.get(path, headers=CSRF)).status_code == 401, path
        assert (await client.get("/api/sessions", headers=auth_headers())).status_code == 200

    async def test_the_mac_is_told(self, client):
        await client.post("/api/pair/start", headers=CSRF, json={"device": "Test Headset"})
        await asyncio.sleep(0)
        notify = pairing.notify
        notify.assert_awaited_once()
        assert notify.await_args.args[0].device == "Test Headset"


class TestWhatTheMacShows:
    """Every change to the book reaches the Mac, so no card is ever stale.

    A notification is transient and a log line scrolls, so the menu bar app
    keeps a pending request on screen for the two minutes it is good for.
    That card is only as trustworthy as these announcements: one that is not
    sent leaves the wearer keying in a code that stopped working, which is
    strictly worse than showing them nothing.
    """

    @pytest.fixture
    def shown(self) -> list[dict]:
        """Everything the Mac is told, in order. ``_fresh`` takes it away again."""
        events: list[dict] = []
        pairing.set_watcher(events.append)
        return events

    def test_a_new_request_announces_the_code_the_device_and_who_asked(self, shown):
        r = pairing.start("Test Headset", "192.168.1.50", now=0)
        assert shown == [{
            "state": "pending",
            "request_id": r.request_id,
            # Spaced on this side, so the Mac has no second opinion about
            # where the gap goes.
            "code": pairing.spaced(r.code),
            "device": "Test Headset",
            # The one part of a request a stranger cannot choose, and so the
            # one the wearer can check against what their own headset says.
            "host": "192.168.1.50",
            "expires_in": pairing.CODE_TTL_S,
            "attempts": 0,
        }]

    def test_a_device_name_is_cut_before_it_is_announced(self, shown):
        """The Mac renders this name, so it leaves here as plain characters."""
        pairing.start('Headset" & beep & "', "10.0.0.5", now=0)
        assert shown[0]["device"] == "Headset beep"

    def test_redeeming_the_code_takes_the_card_away(self, shown):
        r = pairing.start("Headset", "10.0.0.5", now=0)
        pairing.finish(r.request_id, r.code, "tok", now=5)
        assert shown[-1] == {"state": "resolved", "request_id": r.request_id, "reason": "paired"}

    def test_the_card_never_carries_the_token(self, shown):
        r = pairing.start("Headset", "10.0.0.5", now=0)
        pairing.finish(r.request_id, r.code, TEST_AUTH_TOKEN, now=5)
        assert TEST_AUTH_TOKEN not in str(shown)

    def test_a_wrong_try_shows_on_the_card_until_the_tries_run_out(self, shown):
        r = pairing.start("Headset", "10.0.0.5", now=0)
        wrong = "000000" if r.code != "000000" else "111111"
        for _ in range(pairing.MAX_ATTEMPTS):
            refused(lambda: pairing.finish(r.request_id, wrong, "tok", now=10))
        pending = [e for e in shown if e["state"] == "pending"]
        # A wrong try the wearer did not make is the thing they most need to
        # see, so the card is re-announced rather than left as it was.
        assert [e["attempts"] for e in pending] == [0, 1, 2, 3, 4]
        assert [e["expires_in"] for e in pending[1:]] == [pairing.CODE_TTL_S - 10] * 4
        assert shown[-1] == {"state": "resolved", "request_id": r.request_id, "reason": "failed"}

    def test_a_request_nobody_answered_is_swept_when_the_next_one_arrives(self, shown):
        stale = pairing.start("Headset", "10.0.0.5", now=0)
        pairing.start("Headset", "10.0.0.5", now=pairing.CODE_TTL_S + 1)
        assert pairing.resolved_event(stale.request_id, "expired") in shown

    def test_a_lockout_takes_every_card_away(self, shown):
        TestCodes.wrong_codes(pairing.LOCKOUT_FAILURES - 1, t=0)
        burning = next(iter(pairing._book.pending))
        wrong = "000000" if pairing._book.pending[burning].code != "000000" else "111111"
        bystander = pairing.start("Headset", "10.0.0.5", now=0)
        shown.clear()
        assert refused(lambda: pairing.finish(burning, wrong, "tok", now=0)).status == 429
        assert {e["request_id"] for e in shown} == {burning, bystander.request_id}
        assert {e["reason"] for e in shown} == {"locked"}

    def test_a_watcher_that_throws_cannot_stop_a_pairing(self):
        """Showing the code is not allowed to be load-bearing."""
        def boom(event: dict) -> None:
            raise RuntimeError("no screen")
        pairing.set_watcher(boom)
        r = pairing.start("Headset", "10.0.0.5", now=0)
        assert pairing.finish(r.request_id, r.code, "tok", now=1) == "tok"

    def test_a_bridge_nobody_is_watching_announces_nothing(self):
        """The default is a no-op: in a terminal the log line is already visible."""
        r = pairing.start("Headset", "10.0.0.5", now=0)
        assert pairing.finish(r.request_id, r.code, "tok", now=1) == "tok"
