"""The 1 MiB request-body cap, and the drain that makes its refusal readable.

Two things are under test and they fail differently:

* The **cap**. An unauthenticated POST to a pairing endpoint could hold whatever
  it sent in memory, because the per-route check it replaced ran after FastAPI
  had already buffered and parsed the body. Chunked encoding sent no
  ``Content-Length`` at all, and a client could simply lie in the header.

* The **drain**. The cap worked and its answer was unreadable: refusing without
  consuming the body made uvicorn close while the client was still writing, so a
  client that genuinely tripped the limit read a broken pipe instead of the 413
  -- the "looks like it is hanging" symptom the cap exists to prevent. The
  declared-oversize branch was worst, since it reads none of the body.

The drain's own bounds are tested too: a client is not obliged to send what it
declared, and a byte-bounded-only drain deadlocked against one that didn't.
"""

from __future__ import annotations

import asyncio
import http.client
import threading

from bridge.bridge_server import MAX_REQUEST_BODY_BYTES, BodySizeLimitMiddleware


class RecordingApp:
    """Inner ASGI app: records the body it was handed, answers 200."""

    def __init__(self) -> None:
        self.called = False
        self.body = b""

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        while True:
            message = await receive()
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})


async def run(middleware, messages, declared: object = None) -> tuple[int, bytes]:
    """Drive *middleware* over an HTTP scope, returning (status, body)."""
    pending = list(messages)
    received = 0

    async def receive():
        nonlocal received
        received += 1
        if pending:
            return pending.pop(0)
        # A client that has stopped sending but not disconnected -- the shape
        # that deadlocked the byte-bounded drain. Never returns.
        await asyncio.Event().wait()

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", str(declared).encode()))
    scope = {"type": "http", "method": "POST", "path": "/api/events", "headers": headers}
    await middleware(scope, receive, send)

    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def chunk(data: bytes, more: bool = False) -> dict:
    return {"type": "http.request", "body": data, "more_body": more}


class TestTheCap:
    async def test_a_small_body_reaches_the_app_intact(self):
        app = RecordingApp()
        status, _ = await run(BodySizeLimitMiddleware(app), [chunk(b'{"a":1}')], declared=7)
        assert status == 200
        # Replay, not just pass-through: the middleware consumed the body to
        # measure it, so the app can only see it if it was handed back.
        assert app.body == b'{"a":1}'

    async def test_a_body_split_across_chunks_is_reassembled(self):
        app = RecordingApp()
        status, _ = await run(
            BodySizeLimitMiddleware(app),
            [chunk(b"abc", more=True), chunk(b"def", more=True), chunk(b"ghi")],
        )
        assert status == 200
        assert app.body == b"abcdefghi"

    async def test_a_declared_oversize_body_is_refused_unread(self):
        app = RecordingApp()
        status, body = await run(
            BodySizeLimitMiddleware(app),
            [chunk(b"x" * 10)],
            declared=MAX_REQUEST_BODY_BYTES + 1,
        )
        assert status == 413
        assert b"Payload too large" in body
        assert not app.called

    async def test_a_lying_header_does_not_get_the_body_in(self):
        """No Content-Length, or a small one, and then megabytes."""
        app = RecordingApp()
        over = MAX_REQUEST_BODY_BYTES + 1
        messages = [chunk(b"x" * 65536, more=True) for _ in range(over // 65536 + 2)]
        middleware = BodySizeLimitMiddleware(app)
        # Shortened: the refusal drains, and `run`'s receive never resolves once
        # the queue empties, so the real 2 s budget was 2 s of pure sleeping.
        middleware.DRAIN_TIMEOUT_SECONDS = 0.05
        status, _ = await run(middleware, messages, declared=10)
        assert status == 413
        assert not app.called

    async def test_a_chunked_oversize_body_is_refused(self):
        """The case with no Content-Length at all, which read as "0" once."""
        app = RecordingApp()
        over = MAX_REQUEST_BODY_BYTES + 1
        messages = [chunk(b"x" * 65536, more=True) for _ in range(over // 65536 + 2)]
        middleware = BodySizeLimitMiddleware(app)
        middleware.DRAIN_TIMEOUT_SECONDS = 0.05
        status, _ = await run(middleware, messages)
        assert status == 413
        assert not app.called

    async def test_a_garbage_content_length_is_refused_as_a_bad_request(self):
        app = RecordingApp()
        status, body = await run(BodySizeLimitMiddleware(app), [chunk(b"{}")],
                                 declared="not-a-number")
        assert status == 400
        assert b"Invalid Content-Length" in body
        assert not app.called

    async def test_a_disconnect_mid_body_answers_nothing(self):
        app = RecordingApp()
        status, _ = await run(
            BodySizeLimitMiddleware(app),
            [chunk(b"abc", more=True), {"type": "http.disconnect"}],
        )
        assert status == 0
        assert not app.called

    async def test_a_non_http_scope_is_passed_straight_through(self):
        """WebSockets have no request body for this to measure."""
        seen = []

        async def ws_app(scope, receive, send):
            seen.append(scope["type"])

        await BodySizeLimitMiddleware(ws_app)({"type": "websocket"}, None, None)
        assert seen == ["websocket"]


class TestTheDrain:
    """Bounded by bytes and by time; either bound just stops reading."""

    async def test_it_consumes_the_rest_of_a_refused_body(self):
        chunks = [chunk(b"x" * 1024, more=True) for _ in range(4)] + [chunk(b"x" * 1024)]
        pending = list(chunks)

        async def receive():
            return pending.pop(0)

        await BodySizeLimitMiddleware(RecordingApp())._drain(receive)
        assert pending == [], "stopped reading before the client finished writing"

    async def test_it_stops_at_the_byte_bound(self):
        mw = BodySizeLimitMiddleware(RecordingApp())
        reads = 0

        async def receive():
            nonlocal reads
            reads += 1
            return chunk(b"x" * 65536, more=True)

        await mw._drain(receive)
        assert reads <= mw.DRAIN_LIMIT_BYTES // 65536 + 1

    async def test_it_stops_at_the_deadline_when_the_client_sends_no_more(self):
        """The lying-header deadlock: ``more_body`` forever, and no more body.

        uvicorn keeps reporting more to come because the header said so, while
        the client is already waiting for the response. Byte-bounded only, this
        never returned and both ends sat until the client timed out.
        """
        mw = BodySizeLimitMiddleware(RecordingApp())
        mw.DRAIN_TIMEOUT_SECONDS = 0.05
        done = asyncio.Event()

        async def receive():
            if not done.is_set():
                done.set()
                return chunk(b"x" * 10, more=True)
            await asyncio.Event().wait()  # never resolves

        await asyncio.wait_for(mw._drain(receive), timeout=2.0)

    async def test_it_stops_on_disconnect(self):
        mw = BodySizeLimitMiddleware(RecordingApp())
        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            return {"type": "http.disconnect"}

        await mw._drain(receive)
        assert calls == 1

    async def test_a_receive_that_raises_does_not_escape(self):
        async def receive():
            raise RuntimeError("socket gone")

        await BodySizeLimitMiddleware(RecordingApp())._drain(receive)

    async def test_the_refusal_drains_before_it_answers(self):
        """Order matters: the response is unreadable if the body is still coming."""
        mw = BodySizeLimitMiddleware(RecordingApp())
        order: list[str] = []

        async def receive():
            order.append("read")
            return chunk(b"x" * 10)

        async def send(message):
            if message["type"] == "http.response.start":
                order.append("answer")

        await mw._reject(send, receive=receive)
        assert order == ["read", "answer"]


def _serve(app):
    """Start uvicorn on an EPHEMERAL port and return (server, port).

    Port 0, not a pre-probed free port: the old helper bound a socket, read the
    number, closed it, and handed back the number -- so anything on the box
    could take it in the gap.

    And it FAILS rather than skips. `pytest.skip("uvicorn did not start")`
    meant the only test that can catch the real regression -- uvicorn closing a
    socket whose receive buffer still holds the client's body, so the client
    reads a broken pipe instead of the 413 -- could stop running and look like
    ordinary environmental noise. The suite already reports a steady 5 skips
    (PowerShell), so a 6th would not stand out.
    """
    import time

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start within 10s"
    sockets = [s for srv in server.servers for s in srv.sockets]
    assert sockets, "uvicorn started but is not listening"
    return server, sockets[0].getsockname()[1]


def test_a_client_that_trips_the_limit_reads_the_413_over_a_real_socket():
    """The regression, end to end.

    No ASGI-transport test can catch this: the failure is uvicorn closing a
    socket whose receive buffer still holds the client's body, which costs the
    client the response it had not read yet. Measured before the drain: twelve
    of twelve honest 1 MiB uploads raised ``BrokenPipeError`` instead of
    returning 413.
    """
    server, port = _serve(BodySizeLimitMiddleware(RecordingApp()))
    try:
        statuses = []
        for _ in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("POST", "/api/events", body=b"x" * (MAX_REQUEST_BODY_BYTES + 64),
                             headers={"Content-Type": "application/json"})
                statuses.append(conn.getresponse().status)
            except OSError as exc:  # BrokenPipeError / ConnectionReset
                statuses.append(type(exc).__name__)
            finally:
                conn.close()
        assert statuses == [413] * 5
    finally:
        server.should_exit = True
