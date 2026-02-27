"""macOS speech-to-text via a compiled Swift CLI (``voxherd-listen``).

Mirrors ``mac_tts.py`` structure: best-effort, async, never raises.
The Swift CLI uses ``SFSpeechRecognizer`` with on-device recognition.

IMPORTANT — macOS TCC "responsible process" issue:
When Python spawns a child process via fork+exec (which asyncio and
subprocess both use), macOS TCC attributes the child's microphone access
to Python's "responsible process" — not Terminal.app.  This means the
child can't access the mic even though Terminal.app has permission.

Fix: use ``posix_spawn`` with ``responsibility_spawnattrs_setdisclaim``
so the child process disclaims its parent's responsibility and gets
evaluated independently by TCC.  This requires calling private macOS
APIs via ctypes.
"""

import asyncio
import ctypes
import ctypes.util
import os
import sys
import tempfile
import time

from bridge.env_utils import get_subprocess_env


# Path to the compiled Swift binary, adjacent to this module.
_BINARY = os.environ.get(
    "VOXHERD_STT_BINARY",
    os.path.join(os.path.dirname(__file__), "stt", "voxherd-listen"),
)

# ---------------------------------------------------------------------------
# macOS posix_spawn with responsibility disclaim
# ---------------------------------------------------------------------------

_libc = ctypes.CDLL(ctypes.util.find_library("c"))
_libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")

# posix_spawnattr_t is an opaque struct; we treat it as a byte buffer.
# On macOS it's typically ~300 bytes; 512 is safe.
_SPAWNATTR_SIZE = 512


def _spawn_disclaimed(cmd: str, args: list[str], stdout_path: str) -> int:
    """Spawn a process using posix_spawn with responsibility disclaimed.

    This lets the child process be independently evaluated by macOS TCC
    for microphone/camera permissions, instead of inheriting Python's
    (denied) context.

    Returns the child PID, or -1 on error.
    """
    # posix_spawnattr_init
    attr = ctypes.create_string_buffer(_SPAWNATTR_SIZE)
    if _libc.posix_spawnattr_init(attr) != 0:
        return -1

    # responsibility_spawnattrs_setdisclaim — private API in libSystem
    try:
        _libsystem.responsibility_spawnattrs_setdisclaim(attr, ctypes.c_int(1))
    except AttributeError:
        # Not available on this macOS version; fall through to normal spawn
        pass

    # Build argv as ctypes array
    argv_type = ctypes.c_char_p * (len(args) + 1)
    argv = argv_type(*[a.encode() for a in args], None)

    # Build file_actions to redirect stdout to our file
    file_actions = ctypes.create_string_buffer(512)
    _libc.posix_spawn_file_actions_init(file_actions)

    # Open stdout_path as fd 1 (stdout)
    stdout_path_b = stdout_path.encode()
    O_WRONLY_CREAT_TRUNC = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    _libc.posix_spawn_file_actions_addopen(
        file_actions, ctypes.c_int(1),
        stdout_path_b, ctypes.c_int(O_WRONLY_CREAT_TRUNC), ctypes.c_int(0o644)
    )
    # Redirect stderr to log file for debugging
    stt_log = os.path.expanduser("~/.voxherd/logs/stt-stderr.log").encode()
    _libc.posix_spawn_file_actions_addopen(
        file_actions, ctypes.c_int(2),
        stt_log, ctypes.c_int(os.O_WRONLY | os.O_CREAT | os.O_TRUNC), ctypes.c_int(0o644)
    )

    # Build environ as ctypes array (use get_subprocess_env for enriched PATH)
    env_list = [f"{k}={v}".encode() for k, v in get_subprocess_env().items()]
    envp_type = ctypes.c_char_p * (len(env_list) + 1)
    envp = envp_type(*env_list, None)

    # Spawn
    pid = ctypes.c_int(0)
    cmd_b = cmd.encode()
    ret = _libc.posix_spawn(
        ctypes.byref(pid),
        cmd_b,
        file_actions,
        attr,
        argv,
        envp,
    )

    # Cleanup attrs
    _libc.posix_spawnattr_destroy(attr)
    _libc.posix_spawn_file_actions_destroy(file_actions)

    if ret != 0:
        return -1
    return pid.value


class MacSTT:
    """Async wrapper around the ``voxherd-listen`` Swift CLI."""

    def __init__(self, timeout: int = 8, locale: str = "en-US") -> None:
        self.enabled = False
        self.timeout = timeout
        self.locale = locale
        self._warned_missing = False

    @property
    def available(self) -> bool:
        """True if the compiled binary exists on disk."""
        return os.path.isfile(_BINARY) and os.access(_BINARY, os.X_OK)

    async def listen(self, timeout: int | None = None, quiet: bool = False) -> str | None:
        """Listen on the Mac mic and return the transcription, or ``None``.

        Returns ``None`` on silence timeout, empty output, or any error.
        Never raises — callers can fire-and-forget.

        If *quiet* is True, suppresses the beep and log messages (used for
        wake word passive listening).
        """
        if not self.enabled:
            return None
        if not self.available:
            if not self._warned_missing:
                self._warned_missing = True
                print(f"[mac_stt] WARNING: STT binary not found at {_BINARY}. "
                      f"Run scripts/build-stt.sh to compile it.", file=sys.stderr)
            return None

        effective_timeout = timeout if timeout is not None else self.timeout

        t1 = time.monotonic()
        if not quiet:
            print("[mac_stt] Starting STT (beep plays when recognizer is ready)...", file=sys.stderr)

        # Write output to temp file; spawn with disclaimed responsibility
        fd, out_path = tempfile.mkstemp(prefix="stt-out-", suffix=".txt")
        os.close(fd)

        args = [
            _BINARY,
            "--timeout", str(effective_timeout),
            "--locale", self.locale,
        ]
        if quiet:
            args.append("--no-beep")

        try:
            pid = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _spawn_disclaimed(_BINARY, args, out_path),
            )

            if pid < 0:
                print("[mac_stt] Failed to spawn STT process", file=sys.stderr)
                return None

            # Wait for the child process
            def _wait_child(child_pid: int, deadline: float) -> int:
                """Wait for child, returning exit status or -1 on timeout."""
                while time.monotonic() < deadline:
                    result = os.waitpid(child_pid, os.WNOHANG)
                    if result[0] != 0:
                        # Child exited
                        if os.WIFEXITED(result[1]):
                            return os.WEXITSTATUS(result[1])
                        return -1
                    time.sleep(0.2)
                # Timeout — kill child
                try:
                    os.kill(child_pid, 9)
                    os.waitpid(child_pid, 0)
                except OSError:
                    pass
                return -1

            exit_code = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _wait_child(pid, time.monotonic() + effective_timeout + 5),
            )

            # Read result from temp file
            with open(out_path, "r") as f:
                text = f.read().strip()

            t2 = time.monotonic()
            preview = text[:60] if text else "(empty)"
            if not quiet:
                print(f"[mac_stt] STT done ({t2 - t1:.2f}s). Result: {preview}", file=sys.stderr)

            if exit_code != 0:
                return None
            return text if text else None

        except Exception as exc:
            print(f"[mac_stt] Error: {exc}", file=sys.stderr)
            return None
        finally:
            # Always clean up the temp file, regardless of success/failure
            try:
                os.unlink(out_path)
            except OSError:
                pass
