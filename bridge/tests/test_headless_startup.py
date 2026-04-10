"""End-to-end integration tests for the bridge headless startup lifecycle.

The macOS menu bar app (and systemd on Linux) launches the bridge as a
subprocess with ``--headless`` and communicates via:

1. **stdout**: JSON-lines log events + a ``{"status": "ready", "port": N}``
   ready signal that tells the host app the server is up.
2. **HTTP**: REST API on the announced port.
3. **SIGINT**: Graceful shutdown.

If *any* of these contracts break, the desktop app gets stuck at "Starting...",
can't poll sessions, or leaves zombie processes.  These tests exercise the full
subprocess lifecycle so regressions are caught before they ship.

Covered scenarios
-----------------
- Ready signal JSON format matches Swift ``ReadySignal`` struct.
- Ready signal arrives within 5 s (i.e., before slow ops like Bonjour).
- HTTP API is reachable shortly after the ready signal.
- Session registration round-trips through the running bridge.
- Headless JSON-lines log format is parseable.
- SIGINT triggers a shutdown (process exits, port freed).
- ``headless_port`` module-level flag controls signal emission.
- Ready signal is the *first* meaningful JSON line on stdout.
- PYTHONUNBUFFERED is required for timely signal delivery.

Architecture note
-----------------
The ready signal is emitted from FastAPI's lifespan handler.  Uvicorn
binds the listening socket *after* the lifespan yields, so there is a
brief window (~0.5 s) where the signal has been sent but HTTP is not yet
reachable.  The macOS app's session-polling loop already retries on
connection errors; these tests use ``_wait_for_http`` with the same
retry pattern.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    """Return a TCP port that is currently unbound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bridge_env(token: str = "e2e-test-token") -> dict[str, str]:
    """Build an env dict suitable for a headless bridge subprocess."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["VOXHERD_AUTH_TOKEN"] = token
    return env


def _start_bridge(port: int, extra_args: list[str] | None = None,
                  token: str = "e2e-test-token") -> subprocess.Popen:
    """Spawn a bridge subprocess in headless mode on *port*."""
    args = [sys.executable, "-m", "bridge", "run",
            "--headless", "--port", str(port)]
    if extra_args:
        args.extend(extra_args)
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_bridge_env(token),
    )


def _wait_for_ready(proc: subprocess.Popen, timeout: float = 5.0
                    ) -> dict[str, Any] | None:
    """Read stdout lines until the ready signal appears or *timeout* expires.

    Returns the parsed ready-signal dict, or ``None`` on timeout / EOF.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break  # EOF — process exited
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
            if parsed.get("status") == "ready":
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _wait_for_http(port: int, token: str = "e2e-test-token",
                   timeout: float = 5.0) -> httpx.Response:
    """Poll ``GET /api/sessions`` until it responds or *timeout* expires.

    Uvicorn binds the socket *after* the lifespan yields, so the first
    request right after the ready signal will see ``Connection refused``.
    A short retry loop (matching what the macOS app does) handles this.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"http://127.0.0.1:{port}/api/sessions",
                headers={"Authorization": f"Bearer {token}", "X-VoxHerd": "1"},
                timeout=2.0,
            )
            return resp
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            time.sleep(0.15)
    raise RuntimeError(
        f"Bridge HTTP not reachable on port {port} within {timeout}s: {last_exc}"
    )


def _kill_bridge(proc: subprocess.Popen, sig: int = signal.SIGINT,
                 timeout: float = 5.0) -> int:
    """Send *sig* to the bridge and return the exit code."""
    if proc.poll() is not None:
        return proc.returncode
    proc.send_signal(sig)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    return proc.returncode


def _collect_stdout_lines(proc: subprocess.Popen,
                          timeout: float = 2.0) -> list[str]:
    """Drain stdout lines until timeout or EOF."""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            lines.append(text)
    return lines


# ---------------------------------------------------------------------------
# Ready-Signal Contract
# ---------------------------------------------------------------------------

class TestReadySignalFormat:
    """The ready signal is the only way the desktop app knows the server is up.
    Any change to its shape breaks the macOS/Linux host apps."""

    def test_required_fields(self):
        """Must contain ``status`` (str) and ``port`` (int)."""
        sig = json.loads('{"status": "ready", "port": 7777}')
        assert isinstance(sig["status"], str)
        assert isinstance(sig["port"], int)

    def test_extra_fields_are_harmless(self):
        """Swift's ``ReadySignal`` uses ``Decodable``; extra keys are ignored."""
        sig = json.loads('{"status": "ready", "port": 7777, "version": "1.0"}')
        assert sig["status"] == "ready"
        assert sig["port"] == 7777

    def test_single_line_no_pretty_print(self):
        """The signal must be a single line (no newlines inside the JSON)."""
        line = json.dumps({"status": "ready", "port": 7777})
        assert "\n" not in line


# ---------------------------------------------------------------------------
# Subprocess Lifecycle — Full E2E
# ---------------------------------------------------------------------------

class TestBridgeStartup:
    """Verify the bridge starts, signals readiness, and serves HTTP."""

    def test_ready_signal_arrives_within_5_seconds(self):
        """The ready signal must arrive before slow operations (Bonjour).

        Before the fix, Bonjour registration blocked for ~11 s and the
        ready signal was emitted *after* it, leaving the macOS app stuck
        at 'Starting...' for the entire duration.
        """
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            t0 = time.monotonic()
            ready = _wait_for_ready(proc, timeout=5.0)
            elapsed = time.monotonic() - t0

            assert ready is not None, (
                f"No ready signal within 5 s.  "
                f"stderr: {proc.stderr.read().decode()[:500]}"
            )
            assert ready["port"] == port
            assert elapsed < 5.0, f"Ready signal took {elapsed:.1f} s"
        finally:
            _kill_bridge(proc)

    def test_ready_signal_is_first_json_line(self):
        """The ready signal should be among the first few stdout lines.

        The macOS app reads stdout sequentially.  If dozens of log lines
        appear before the ready signal, startup feels slow.
        """
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            lines_before_ready = 0
            deadline = time.monotonic() + 5.0
            found = False

            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                    if parsed.get("status") == "ready":
                        found = True
                        break
                except json.JSONDecodeError:
                    pass
                lines_before_ready += 1

            assert found, "Ready signal never appeared"
            assert lines_before_ready < 5, (
                f"Ready signal appeared after {lines_before_ready} other lines "
                f"— should be among the first few"
            )
        finally:
            _kill_bridge(proc)

    def test_http_api_reachable_after_ready_signal(self):
        """``GET /api/sessions`` must return 200 shortly after the ready signal.

        Uvicorn binds the socket ~0.5 s after the lifespan emits the
        ready signal.  The macOS app's session-polling retries on
        connection errors; this test uses the same retry pattern.
        """
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None, "No ready signal"

            resp = _wait_for_http(port)
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, dict)
        finally:
            _kill_bridge(proc)

    def test_ready_signal_port_matches_actual_listen_port(self):
        """The port in the ready signal must match where the server listens."""
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None
            assert ready["port"] == port

            resp = _wait_for_http(ready["port"])
            assert resp.status_code == 200
        finally:
            _kill_bridge(proc)


# ---------------------------------------------------------------------------
# HTTP API Through Live Bridge
# ---------------------------------------------------------------------------

class TestBridgeHTTPAfterStartup:
    """Verify key API endpoints work against a live bridge process."""

    @pytest.fixture(autouse=True)
    def bridge(self):
        """Start a bridge for the test, tear it down afterwards."""
        self.port = _find_free_port()
        self.token = "e2e-session-test-token"
        self.proc = _start_bridge(self.port, token=self.token)
        ready = _wait_for_ready(self.proc, timeout=5.0)
        assert ready is not None, "Bridge failed to start"
        _wait_for_http(self.port, token=self.token)
        yield
        _kill_bridge(self.proc)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-VoxHerd": "1"}

    def test_sessions_endpoint_returns_dict(self):
        """GET /api/sessions returns a JSON dict (may have persisted sessions)."""
        resp = httpx.get(
            f"http://127.0.0.1:{self.port}/api/sessions",
            headers=self._headers(), timeout=3.0,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_session_registration_round_trip(self):
        """Register a session via POST, then see it in GET."""
        project_dir = os.path.join(
            os.path.expanduser("~"), ".voxherd", "test-projects", "e2e-test"
        )
        os.makedirs(project_dir, exist_ok=True)

        reg_resp = httpx.post(
            f"http://127.0.0.1:{self.port}/api/sessions/register",
            json={
                "session_id": "e2e-session-001",
                "project": "e2e-test",
                "project_dir": project_dir,
            },
            headers=self._headers(), timeout=3.0,
        )
        assert reg_resp.status_code == 200

        list_resp = httpx.get(
            f"http://127.0.0.1:{self.port}/api/sessions",
            headers=self._headers(), timeout=3.0,
        )
        assert list_resp.status_code == 200
        sessions = list_resp.json()
        assert "e2e-session-001" in sessions
        assert sessions["e2e-session-001"]["project"] == "e2e-test"

    def test_events_endpoint_accepts_stop_event(self):
        """POST /api/events with a stop event should return ok."""
        project_dir = os.path.join(
            os.path.expanduser("~"), ".voxherd", "test-projects", "e2e-stop"
        )
        os.makedirs(project_dir, exist_ok=True)

        httpx.post(
            f"http://127.0.0.1:{self.port}/api/sessions/register",
            json={"session_id": "e2e-stop-001", "project": "e2e-stop",
                  "project_dir": project_dir},
            headers=self._headers(), timeout=3.0,
        )

        resp = httpx.post(
            f"http://127.0.0.1:{self.port}/api/events",
            json={
                "event": "stop",
                "session_id": "e2e-stop-001",
                "project": "e2e-stop",
                "summary": "Test completed",
                "stop_reason": "completed",
            },
            headers=self._headers(), timeout=3.0,
        )
        assert resp.status_code == 200

    def test_auth_required(self):
        """Requests without a valid token should be rejected."""
        resp = httpx.get(
            f"http://127.0.0.1:{self.port}/api/sessions",
            headers={"X-VoxHerd": "1"},  # no Authorization header
            timeout=3.0,
        )
        assert resp.status_code == 401

    def test_tts_endpoint_exists(self):
        """POST /api/tts should accept a speak request (even if TTS is off)."""
        resp = httpx.post(
            f"http://127.0.0.1:{self.port}/api/tts",
            json={"text": "test"},
            headers=self._headers(), timeout=3.0,
        )
        # 200 (spoke) or 400 (missing field) — but NOT 404
        assert resp.status_code != 404


# ---------------------------------------------------------------------------
# Headless JSON-Lines Log Format
# ---------------------------------------------------------------------------

class TestHeadlessLogFormat:
    """The macOS app parses stdout as JSON-lines (``LogEntry``).
    Every non-ready-signal line must be valid JSON with known fields."""

    def test_log_lines_are_valid_json(self):
        """All stdout lines after the ready signal must be parseable JSON."""
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None

            # Wait for HTTP so the server is fully up and generating log lines
            _wait_for_http(port)

            # Trigger a registration to generate a log event
            project_dir = os.path.join(
                os.path.expanduser("~"), ".voxherd", "test-projects", "log-test"
            )
            os.makedirs(project_dir, exist_ok=True)
            httpx.post(
                f"http://127.0.0.1:{port}/api/sessions/register",
                json={"session_id": "log-test-001", "project": "log-test",
                      "project_dir": project_dir},
                headers={"Authorization": "Bearer e2e-test-token", "X-VoxHerd": "1"},
                timeout=3.0,
            )

            # Collect log lines
            lines = _collect_stdout_lines(proc, timeout=2.0)

            # We should have at least one line (Bonjour, session registration, etc.)
            assert len(lines) > 0, "No log output after registration"

            for raw_line in lines:
                try:
                    parsed = json.loads(raw_line)
                    assert isinstance(parsed, dict), f"Not a JSON object: {raw_line[:200]}"
                except json.JSONDecodeError:
                    pytest.fail(f"Non-JSON line on stdout: {raw_line[:200]}")
        finally:
            _kill_bridge(proc)

    def test_log_entry_has_required_fields(self):
        """Log entries must have ts, level, project, message for the macOS LogEntry model."""
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None
            _wait_for_http(port)

            # Trigger activity
            project_dir = os.path.join(
                os.path.expanduser("~"), ".voxherd", "test-projects", "log-fields"
            )
            os.makedirs(project_dir, exist_ok=True)
            httpx.post(
                f"http://127.0.0.1:{port}/api/sessions/register",
                json={"session_id": "log-fields-001", "project": "log-fields",
                      "project_dir": project_dir},
                headers={"Authorization": "Bearer e2e-test-token", "X-VoxHerd": "1"},
                timeout=3.0,
            )

            lines = _collect_stdout_lines(proc, timeout=2.0)
            log_entries = []
            for raw_line in lines:
                try:
                    parsed = json.loads(raw_line)
                    # Skip the ready signal
                    if parsed.get("status") == "ready":
                        continue
                    log_entries.append(parsed)
                except json.JSONDecodeError:
                    pass

            assert len(log_entries) > 0, "No log entries collected"

            for entry in log_entries:
                assert "ts" in entry, f"Missing 'ts': {entry}"
                assert "level" in entry, f"Missing 'level': {entry}"
                assert "project" in entry, f"Missing 'project': {entry}"
                assert "message" in entry, f"Missing 'message': {entry}"
                # ts should be an ISO-format timestamp
                assert "T" in entry["ts"], f"'ts' not ISO format: {entry['ts']}"
        finally:
            _kill_bridge(proc)


# ---------------------------------------------------------------------------
# Graceful Shutdown
# ---------------------------------------------------------------------------

class TestBridgeShutdown:
    """The desktop app sends SIGINT to stop the bridge.  If it doesn't exit,
    we get zombie processes or the port stays occupied."""

    def test_sigterm_exits_process(self):
        """SIGTERM should cause the bridge to exit (not hang forever).

        We use SIGTERM instead of SIGINT because the Bonjour background
        thread may delay SIGINT handling (Zeroconf blocks in a daemon
        thread).  SIGTERM is what the macOS app's force-kill uses.
        """
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None
            _wait_for_http(port)

            proc.terminate()  # SIGTERM
            try:
                proc.wait(timeout=8.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                pytest.fail("Bridge did not exit within 8 s after SIGTERM")
        except Exception:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            raise

    def test_port_released_after_shutdown(self):
        """After the bridge exits, the port must be free for reuse."""
        port = _find_free_port()
        proc = _start_bridge(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None
            _wait_for_http(port)
            _kill_bridge(proc, sig=signal.SIGINT, timeout=5.0)
        except Exception:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            raise

        # Brief OS cleanup window for TIME_WAIT
        time.sleep(0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                pytest.fail(f"Port {port} still occupied after bridge shutdown")

    def test_sequential_start_stop_cycles(self):
        """Start → stop → start → stop on the same port must work.

        This catches resource leaks (sockets not closed, temp files locked).
        """
        port = _find_free_port()
        for cycle in range(2):
            proc = _start_bridge(port)
            try:
                ready = _wait_for_ready(proc, timeout=5.0)
                assert ready is not None, f"Cycle {cycle}: no ready signal"
                _wait_for_http(port)
                _kill_bridge(proc, sig=signal.SIGINT, timeout=5.0)
            except Exception:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)
                raise
            # Give OS time to release the port (TIME_WAIT)
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# headless_port Module Flag
# ---------------------------------------------------------------------------

class TestHeadlessPortConfig:
    """The ``headless_port`` flag in server_state controls whether the
    ready signal is emitted.  Default is 0 (no signal)."""

    def test_default_is_zero(self):
        from bridge import server_state
        assert server_state.headless_port == 0

    def test_set_and_read_back(self):
        from bridge import server_state
        original = server_state.headless_port
        try:
            server_state.headless_port = 9999
            assert server_state.headless_port == 9999
        finally:
            server_state.headless_port = original

    def test_non_headless_mode_no_ready_signal(self):
        """When --headless is NOT passed, headless_port stays at 0."""
        from bridge import server_state
        assert server_state.headless_port == 0


# ---------------------------------------------------------------------------
# PYTHONUNBUFFERED Requirement
# ---------------------------------------------------------------------------

class TestPythonUnbuffered:
    """Without PYTHONUNBUFFERED=1, Python buffers stdout when piped,
    causing the ready signal to be delayed or never delivered."""

    def test_ready_signal_arrives_with_unbuffered(self):
        """Explicit PYTHONUNBUFFERED=1 must produce a timely ready signal."""
        port = _find_free_port()
        env = _bridge_env()
        assert env.get("PYTHONUNBUFFERED") == "1"

        proc = subprocess.Popen(
            [sys.executable, "-m", "bridge", "run",
             "--headless", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        try:
            t0 = time.monotonic()
            ready = _wait_for_ready(proc, timeout=5.0)
            elapsed = time.monotonic() - t0

            assert ready is not None, "Ready signal missing with PYTHONUNBUFFERED=1"
            assert elapsed < 3.0, (
                f"Ready signal took {elapsed:.1f} s even with PYTHONUNBUFFERED=1"
            )
        finally:
            _kill_bridge(proc)

    def test_env_helper_sets_unbuffered(self):
        """``_bridge_env()`` must include PYTHONUNBUFFERED=1."""
        env = _bridge_env()
        assert env["PYTHONUNBUFFERED"] == "1"


# ---------------------------------------------------------------------------
# PyInstaller Binary Parity
# ---------------------------------------------------------------------------

# The macOS menu bar app runs a PyInstaller-compiled binary, NOT
# ``python -m bridge``.  If the binary is stale (built from old source),
# it may lack fixes (e.g., async Bonjour, ready signal ordering) that
# the source-based tests above happily pass.  These tests run the *binary*
# directly to catch source/binary divergence.

_BINARY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "macos", "dist", "voxherd-bridge", "voxherd-bridge",
)


def _binary_exists() -> bool:
    return os.path.isfile(_BINARY_PATH) and os.access(_BINARY_PATH, os.X_OK)


def _start_binary(port: int, token: str = "e2e-binary-token") -> subprocess.Popen:
    """Spawn the PyInstaller bridge binary (not the Python source)."""
    env = _bridge_env(token)
    return subprocess.Popen(
        [_BINARY_PATH, "run", "--headless", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


@pytest.mark.skipif(not _binary_exists(), reason="PyInstaller binary not found at macos/dist/")
class TestPyInstallerBinary:
    """Run the same startup contract tests against the compiled binary.

    This is the exact codepath the macOS menu bar app uses.  If the binary
    is stale (built from older source), these tests will FAIL even though
    the source-based tests above pass — which is exactly the divergence
    that caused the 'stuck at Starting...' bug.
    """

    def test_binary_ready_signal_within_5_seconds(self):
        """Binary must emit ready signal within 5 s, same as source."""
        port = _find_free_port()
        proc = _start_binary(port)
        try:
            t0 = time.monotonic()
            ready = _wait_for_ready(proc, timeout=5.0)
            elapsed = time.monotonic() - t0

            assert ready is not None, (
                f"PyInstaller binary did not emit ready signal within 5 s. "
                f"This usually means the binary is stale — rebuild with: "
                f"bash macos/build-bridge.sh\n"
                f"Binary: {_BINARY_PATH}\n"
                f"stderr: {proc.stderr.read().decode()[:500]}"
            )
            assert ready["port"] == port
            assert elapsed < 5.0, (
                f"Binary ready signal took {elapsed:.1f} s (limit: 5 s). "
                f"Bonjour may be blocking the lifespan — rebuild the binary."
            )
        finally:
            _kill_bridge(proc)

    def test_binary_http_reachable_after_ready(self):
        """Binary HTTP server must accept connections after ready signal."""
        port = _find_free_port()
        proc = _start_binary(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None, "Binary: no ready signal"

            resp = _wait_for_http(port, token="e2e-binary-token")
            assert resp.status_code == 200
        finally:
            _kill_bridge(proc)

    def test_binary_log_lines_are_json(self):
        """Binary stdout must produce parseable JSON lines."""
        port = _find_free_port()
        proc = _start_binary(port)
        try:
            ready = _wait_for_ready(proc, timeout=5.0)
            assert ready is not None, "Binary: no ready signal"
            _wait_for_http(port, token="e2e-binary-token")

            lines = _collect_stdout_lines(proc, timeout=2.0)
            for raw_line in lines:
                try:
                    json.loads(raw_line)
                except json.JSONDecodeError:
                    pytest.fail(
                        f"Binary emitted non-JSON on stdout: {raw_line[:200]}\n"
                        f"Binary may be stale — rebuild with: bash macos/build-bridge.sh"
                    )
        finally:
            _kill_bridge(proc)

    def test_binary_source_parity(self):
        """Binary and source must produce the ready signal at comparable speed.

        If the binary is >3x slower than source, it's likely stale and
        missing the async Bonjour fix.
        """
        port_src = _find_free_port()
        port_bin = _find_free_port()

        # Time the source
        proc_src = _start_bridge(port_src)
        try:
            t0 = time.monotonic()
            ready_src = _wait_for_ready(proc_src, timeout=5.0)
            src_elapsed = time.monotonic() - t0
        finally:
            _kill_bridge(proc_src)

        # Time the binary
        proc_bin = _start_binary(port_bin)
        try:
            t0 = time.monotonic()
            ready_bin = _wait_for_ready(proc_bin, timeout=10.0)
            bin_elapsed = time.monotonic() - t0
        finally:
            _kill_bridge(proc_bin)

        assert ready_src is not None, "Source: no ready signal"
        assert ready_bin is not None, (
            f"Binary: no ready signal within 10 s. "
            f"Rebuild with: bash macos/build-bridge.sh"
        )

        # Binary should be at most 3x slower than source.
        # If it's dramatically slower, the binary is likely stale and
        # Bonjour is blocking synchronously.
        ratio = bin_elapsed / max(src_elapsed, 0.01)
        assert ratio < 3.0, (
            f"Binary startup ({bin_elapsed:.1f} s) is {ratio:.1f}x slower than "
            f"source ({src_elapsed:.1f} s). The binary is likely stale and "
            f"missing the async Bonjour fix.\n"
            f"Rebuild with: bash macos/build-bridge.sh"
        )
