"""The WebSocket's Origin check -- the only browser-facing control on it.

WebSockets are exempt from CORS, and ``/ws/ios`` is exempt from
``auth_middleware`` (``bridge/auth.py`` returns early for ``websocket``
scopes), so neither the ``X-VoxHerd`` CSRF header nor the Host pinning applies
here. Origin is what is left, and it had no tests at all.

Two holes have already been found in it by reading:

* ``"null"`` was in the allowed set. That is what a sandboxed iframe, a
  ``data:`` URL and a ``file://`` page all send, so any page on the internet
  could pass by framing itself with ``<iframe sandbox="allow-scripts">``.
* the test was ``origin.startswith("http://localhost")``, which also accepted
  ``http://localhost.attacker.com``.

Both are regressions waiting to happen as long as nothing asserts them.
"""

from __future__ import annotations

import pytest

from bridge.ws_handler import _is_allowed_origin


class TestNativeClientsAreAllowed:
    """iOS ``URLSessionWebSocketTask`` and the Android client send no Origin."""

    @pytest.mark.parametrize("origin", [None, ""])
    def test_an_absent_origin_is_allowed(self, origin):
        assert _is_allowed_origin(origin)


class TestLoopbackBrowsersAreAllowed:
    @pytest.mark.parametrize("origin", [
        "http://localhost",
        "http://localhost:7777",
        "https://localhost:7777",
        "http://127.0.0.1:7777",
        "http://[::1]:7777",
        "HTTP://LOCALHOST:7777",
        # Host-only by design, so a junk port does not change the answer: the
        # host really is localhost, and a browser cannot forge Origin anyway.
        "http://localhost:notaport",
    ])
    def test_a_loopback_origin_is_allowed(self, origin):
        assert _is_allowed_origin(origin)


class TestTheHolesThatWereFound:
    def test_null_is_refused(self):
        """A sandboxed iframe, a data: URL and a file:// page all send this.

        While "null" was allowed, any page on the internet could pass the check
        by framing itself with <iframe sandbox="allow-scripts">.
        """
        assert not _is_allowed_origin("null")

    @pytest.mark.parametrize("origin", [
        "http://localhost.attacker.com",
        "http://localhost.attacker.com:7777",
        "http://127.0.0.1.attacker.com",
        "https://localhost.evil.test",
    ])
    def test_a_prefix_lookalike_is_refused(self, origin):
        """`origin.startswith("http://localhost")` accepted every one of these."""
        assert not _is_allowed_origin(origin)


class TestEverythingElseIsRefused:
    @pytest.mark.parametrize("origin", [
        "http://evil.example",
        "https://evil.example",
        "http://192.168.1.50:7777",      # a LAN peer's browser is still a browser
        "http://macbook.local:7777",     # an mDNS name an LAN squatter can claim
        "file://",
        "data:text/html,<script>",
        "ws://localhost:7777",           # not an http(s) scheme
        "wss://localhost:7777",
        "javascript:alert(1)",
        "//localhost:7777",
        "localhost:7777",                # no scheme
        "http://localhost@evil.example",  # userinfo, host is evil.example
        "http://evil.example#http://localhost",
        "http://evil.example/?x=http://localhost",
    ])
    def test_a_foreign_origin_is_refused(self, origin):
        assert not _is_allowed_origin(origin)

    @pytest.mark.parametrize("origin", [
        "http://",
        "http://[",
        "http://[gg::1]",
        ":",
        "http://:7777",
    ])
    def test_a_malformed_origin_is_refused_without_raising(self, origin):
        """`urlsplit` raises ValueError on some of these; it must not escape.

        The caller is the WebSocket accept path, which catches only
        WebSocketDisconnect -- an exception here would drop the connection
        rather than refuse it.
        """
        assert _is_allowed_origin(origin) is False


def test_the_accept_path_actually_consults_it():
    """A check that is never called reads exactly like one that is."""
    from bridge import ws_handler

    assert _calls(ws_handler.ios_websocket, "_is_allowed_origin"), (
        "the WebSocket accept path no longer calls _is_allowed_origin -- "
        "commented out while debugging?"
    )
    # And refuses rather than logging and continuing.
    import inspect

    assert "disallowed origin" in inspect.getsource(ws_handler.ios_websocket)


def _calls(func, name: str) -> bool:
    """True when *func*'s body really contains a call to *name*.

    Parsed, not grepped. A substring search over ``inspect.getsource`` passes on
    a commented-out call -- which is the single commit shape most likely to
    cause the regression ("comment out the gate while debugging the emulator,
    forget to put it back"), and the one these assertions exist to catch.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == name)
             or (isinstance(node.func, ast.Attribute) and node.func.attr == name))
        for node in ast.walk(tree)
    )
