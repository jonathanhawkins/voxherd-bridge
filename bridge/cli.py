"""CLI entry point for the VoxHerd bridge server.

Supports subcommands:
    start   - Start the bridge in a managed tmux session (default)
    stop    - Stop the bridge
    restart - Restart the bridge
    status  - Show bridge and tmux session status
    cleanup - Clean up stale tmux sessions
    attach  - Attach to the bridge tmux session
    run     - Run the bridge directly (not in tmux; used internally)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections import deque
from datetime import datetime

import uvicorn
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from bridge import bridge_server
from bridge.bridge_server import app
from bridge.tailscale import detect_tailscale

console = Console()


# ---------------------------------------------------------------------------
# Terminal logging
# ---------------------------------------------------------------------------

_LEVEL_STYLES: dict[str, str] = {
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "info": "cyan",
}


def _log_event(level: str, project: str, message: str) -> None:
    """Print a timestamped, color-coded line to the terminal."""
    color = _LEVEL_STYLES.get(level, "white")
    timestamp = datetime.now().strftime("%H:%M:%S")
    console.print(
        f"[dim]{timestamp}[/dim] [bold]{project}[/bold] [{color}]{message}[/{color}]"
    )


# ---------------------------------------------------------------------------
# Persistent CLI display with live stats header
# ---------------------------------------------------------------------------


class CLIDisplay:
    """Persistent header + scrolling event log using ``rich.live.Live``.

    The header shows session counts, iOS connections, feature flags, and
    uptime.  Events scroll below it in the same terminal.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        tts: bool = False,
        listen: bool = False,
        wake_word: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.tts = tts
        self.listen = listen
        self.wake_word = wake_word
        self._start_time = datetime.now()
        self._events: deque[str] = deque(maxlen=500)
        self._live: Live | None = None
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=1,
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def log_event(self, level: str, project: str, message: str) -> None:
        """Append an event line and refresh the display."""
        color = _LEVEL_STYLES.get(level, "white")
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[dim]{timestamp}[/dim] [bold]{project}[/bold] [{color}]{message}[/{color}]"
        with self._lock:
            self._events.append(line)
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass

    # -- rendering ----------------------------------------------------------

    def _render(self) -> Group:
        return Group(self._stats_bar(), self._event_log())

    def _stats_bar(self) -> Panel:
        # Session counts
        all_sessions = bridge_server.sessions.get_all_sessions()
        active = sum(1 for s in all_sessions.values() if s.status == "active")
        idle = sum(1 for s in all_sessions.values() if s.status == "idle")
        waiting = sum(1 for s in all_sessions.values() if s.status == "waiting")
        ios_count = len(bridge_server.ios_connections)

        # Feature flags
        flags: list[str] = []
        if self.tts:
            flags.append("[green]TTS[/green]")
        if self.listen:
            flags.append("[green]STT[/green]")
        if self.wake_word:
            flags.append("[green]Wake[/green]")
        flags_str = " ".join(flags) if flags else "[dim]no voice[/dim]"

        # Uptime
        elapsed = datetime.now() - self._start_time
        total_secs = int(elapsed.total_seconds())
        hours, remainder = divmod(total_secs, 3600)
        mins, secs = divmod(remainder, 60)
        if hours:
            uptime = f"{hours}h{mins:02d}m"
        else:
            uptime = f"{mins}m{secs:02d}s"

        # Session detail
        session_parts: list[str] = []
        if active:
            session_parts.append(f"[green]{active} active[/green]")
        if idle:
            session_parts.append(f"[dim]{idle} idle[/dim]")
        if waiting:
            session_parts.append(f"[cyan]{waiting} waiting[/cyan]")
        if not session_parts:
            session_parts.append("[dim]0[/dim]")
        sessions_str = " ".join(session_parts)

        ios_str = f"[green]{ios_count}[/green]" if ios_count else f"[dim]{ios_count}[/dim]"

        bar = (
            f"[bold]VoxHerd[/bold] {self.host}:{self.port}"
            f"  |  Sessions: {sessions_str}"
            f"  |  iOS: {ios_str}"
            f"  |  {flags_str}"
            f"  |  {uptime}"
        )
        return Panel(Text.from_markup(bar), expand=True, style="dim")

    def _event_log(self) -> Text:
        # Fit events to available terminal height minus header (~4 lines)
        max_lines = max(console.height - 6, 5)
        with self._lock:
            recent = list(self._events)[-max_lines:]
        if not recent:
            return Text.from_markup("[dim]Waiting for events...[/dim]")
        return Text.from_markup("\n".join(recent))


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def print_banner(
    host: str,
    port: int,
    *,
    tts: bool = False,
    voice: str | None = None,
    listen: bool = False,
    listen_timeout: int = 8,
    wake_word: bool = False,
    use_tls: bool = False,
) -> None:
    """Display a startup banner with listen address."""
    if tts:
        from bridge.server_state import openai_tts_enabled
        if openai_tts_enabled:
            tts_line = "TTS [green]ON[/green] (OpenAI gpt-4o-mini-tts, Nova)"
        else:
            tts_line = f"TTS [green]ON[/green] (voice: {voice})"
    else:
        tts_line = "TTS [dim]off[/dim] (use --tts to enable)"
    if listen:
        listen_line = f"STT [green]ON[/green] (timeout: {listen_timeout}s)"
    else:
        listen_line = "STT [dim]off[/dim] (use --listen to enable)"
    if wake_word:
        wake_line = "Wake word [green]ON[/green] -- say [bold]'Hey Claude'[/bold] to activate"
    else:
        wake_line = "Wake word [dim]off[/dim] (use --wake-word to enable)"

    ts_info = detect_tailscale(port)
    if ts_info:
        ts_line = (
            f"Tailscale [green]{ts_info['ip']}[/green] ({ts_info['hostname']})\n"
            f"iOS config: [cyan]{ts_info['url']}[/cyan]"
        )
    else:
        ts_line = "Tailscale [dim]not detected[/dim] -- install for remote access"

    tls_line = "TLS [green]ON[/green] (wss:// + https://)" if use_tls else "TLS [dim]off[/dim] (use --tls to enable)"
    proto = "https" if use_tls else "http"

    content = (
        "[bold]VoxHerd Bridge Server[/bold]\n"
        f"Listening on [cyan]{proto}://{host}:{port}[/cyan]\n"
        f"{ts_line}\n"
        f"{tls_line}\n"
        f"{tts_line}\n"
        f"{listen_line}\n"
        f"{wake_line}\n"
        "[dim]Ctrl-C to stop[/dim]"
    )
    console.print(Panel(content, expand=False))


# ---------------------------------------------------------------------------
# Bridge server arguments (shared between 'start' and 'run')
# ---------------------------------------------------------------------------


def _add_bridge_args(parser: argparse.ArgumentParser) -> None:
    """Add the common bridge server arguments to a parser."""
    _is_mac = sys.platform == "darwin"
    _is_win = sys.platform == "win32"
    _default_voice = "auto" if (_is_mac or _is_win) else "en"
    _default_rate = 190 if _is_mac else (200 if _is_win else 175)

    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0; auth token required for security)")
    parser.add_argument("--port", type=int, default=7777, help="Bind port (default: 7777)")
    parser.add_argument("--tts", action="store_true", help="Enable TTS announcements (macOS 'say', Linux 'espeak-ng', or Windows SAPI5)")
    parser.add_argument("--voice", default=_default_voice, help=f"TTS voice (default: {_default_voice}; 'auto' picks best available on macOS)")
    parser.add_argument("--rate", type=int, default=_default_rate, help=f"TTS speech rate (default: {_default_rate})")
    parser.add_argument("--listen", action="store_true", help="Enable Mac STT (listen after TTS; macOS only)")
    parser.add_argument("--no-listen-after", action="store_true", help="Disable auto-listen after TTS (use with --listen to keep wake word only)")
    parser.add_argument("--listen-timeout", type=int, default=8, help="STT silence timeout in seconds (default: 8)")
    parser.add_argument("--wake-word", action="store_true", help="Enable 'Hey Claude' wake word detection (macOS only; always-on mic)")
    parser.add_argument("--headless", action="store_true", help="Disable Rich display; emit JSON lines to stdout instead")
    parser.add_argument("--openai-tts", action="store_true", help="Use OpenAI TTS (gpt-4o-mini-tts, Nova voice) instead of platform TTS; requires OPENAI_API_KEY")
    parser.add_argument("--tls", action="store_true", help="Enable TLS (self-signed cert; serves wss:// and https://)")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _run_bridge_directly(args: argparse.Namespace) -> None:
    """Run the bridge server directly in the current process.

    This is the internal command used when the bridge is started inside
    a tmux session. Users should prefer 'start' which manages tmux.
    """
    bridge_server.server_port = args.port
    _is_mac = sys.platform == "darwin"

    # Configure TTS (macOS 'say', Linux 'espeak-ng', or OpenAI).
    openai_tts_requested = getattr(args, "openai_tts", False)
    if openai_tts_requested:
        from bridge.openai_tts import OpenAITTS
        oai_tts = OpenAITTS()
        if oai_tts.available:
            bridge_server.mac_tts = oai_tts  # type: ignore[assignment]
            bridge_server._state.mac_tts = oai_tts  # type: ignore[assignment]
            bridge_server._state.openai_tts_enabled = True
            console.print("[green]Using OpenAI TTS (gpt-4o-mini-tts, Nova voice)[/green]")
        else:
            console.print("[yellow]Warning: --openai-tts requested but OPENAI_API_KEY not set or no audio player found. Falling back to platform TTS.[/yellow]")

    bridge_server.mac_tts.enabled = args.tts
    if not openai_tts_requested or not bridge_server._state.openai_tts_enabled:
        if args.voice != "auto":
            # Explicit voice override — use as-is.
            bridge_server.mac_tts.voice = args.voice
        # else: keep the auto-detected voice from MacTTS.__init__
        if hasattr(bridge_server.mac_tts, "rate"):
            bridge_server.mac_tts.rate = args.rate

    # Configure STT (listen-after-speak).
    # Wake word implies --listen and --tts.
    if args.wake_word:
        if not _is_mac:
            console.print("[yellow]Warning: --wake-word requires macOS. Ignoring on this platform.[/yellow]")
            args.wake_word = False
        else:
            args.listen = True
            args.tts = True
            bridge_server.mac_tts.enabled = True
    if args.listen and not _is_mac:
        console.print("[yellow]Warning: --listen requires macOS (Swift STT binary). Ignoring on this platform.[/yellow]")
        args.listen = False
    bridge_server.mac_stt.enabled = args.listen
    bridge_server.mac_stt.timeout = args.listen_timeout
    bridge_server.wake_word_enabled = args.wake_word
    bridge_server.listen_after_enabled = args.listen and not args.no_listen_after

    headless = getattr(args, "headless", False)
    use_tls = getattr(args, "tls", False)

    # Prepare TLS kwargs for uvicorn if enabled
    tls_kwargs: dict[str, str] = {}
    if use_tls:
        from bridge.tls import ensure_cert
        cert_path, key_path = ensure_cert()
        tls_kwargs["ssl_certfile"] = cert_path
        tls_kwargs["ssl_keyfile"] = key_path

    if headless:
        # JSON-lines logger for headless / machine-readable mode.
        def _json_log_event(level: str, project: str, message: str) -> None:
            line = json.dumps({
                "ts": datetime.now().astimezone().isoformat(),
                "level": level,
                "project": project,
                "message": message,
            })
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

        bridge_server._state.set_log_handler(_json_log_event)

        def _handle_sigint(sig: int, frame: object) -> None:
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_sigint)

        # Tell the lifespan to emit the ready signal on stdout once startup is done.
        # (FastAPI ignores @app.on_event("startup") when lifespan is set.)
        bridge_server._state.headless_port = args.port

        uvicorn.run(app, host=args.host, port=args.port, log_level="error", **tls_kwargs)
    else:
        # One-time startup banner (Tailscale URL, voice config, etc.)
        print_banner(
            args.host,
            args.port,
            tts=args.tts,
            voice=bridge_server.mac_tts.voice if args.tts else None,
            listen=args.listen,
            listen_timeout=args.listen_timeout,
            wake_word=args.wake_word,
            use_tls=use_tls,
        )

        # Persistent live display: stats header + scrolling event log.
        display = CLIDisplay(
            args.host,
            args.port,
            tts=args.tts,
            listen=args.listen,
            wake_word=args.wake_word,
        )
        bridge_server._state.set_log_handler(display.log_event)
        display.start()

        def _handle_sigint(sig: int, frame: object) -> None:
            display.stop()
            console.print("\n[yellow]Shutting down...[/yellow]")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_sigint)

        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning", **tls_kwargs)
        finally:
            display.stop()


def _cmd_start(args: argparse.Namespace) -> None:
    """Handle the 'start' subcommand: launch bridge in tmux (or directly on Windows)."""
    # Windows has no tmux — run directly
    if sys.platform == "win32":
        _run_bridge_directly(args)
        return

    from bridge.tmux_manager import start_bridge, is_port_listening, BRIDGE_PORT

    # Check if we're already inside the vh-bridge tmux session
    current_tmux = os.environ.get("TMUX", "")
    if current_tmux and "vh-bridge" in current_tmux:
        # We're already inside the managed session -- run directly
        _run_bridge_directly(args)
        return

    # Collect bridge args to pass through to the 'run' subcommand inside tmux
    bridge_args: list[str] = []
    if args.host != "0.0.0.0":
        bridge_args.extend(["--host", args.host])
    if args.port != 7777:
        bridge_args.extend(["--port", str(args.port)])
    if args.tts:
        bridge_args.append("--tts")
    if args.voice != "auto":
        bridge_args.extend(["--voice", args.voice])
    if args.rate != 190:
        bridge_args.extend(["--rate", str(args.rate)])
    if args.listen:
        bridge_args.append("--listen")
    if args.no_listen_after:
        bridge_args.append("--no-listen-after")
    if args.listen_timeout != 8:
        bridge_args.extend(["--listen-timeout", str(args.listen_timeout)])
    if args.wake_word:
        bridge_args.append("--wake-word")
    if args.headless:
        bridge_args.append("--headless")
    if getattr(args, "openai_tts", False):
        bridge_args.append("--openai-tts")
    if args.tls:
        bridge_args.append("--tls")

    start_bridge(bridge_args, attach=not args.no_attach, force=args.force)


def _cmd_stop(args: argparse.Namespace) -> None:
    """Handle the 'stop' subcommand."""
    from bridge.tmux_manager import stop_bridge
    stop_bridge()


def _cmd_restart(args: argparse.Namespace) -> None:
    """Handle the 'restart' subcommand."""
    from bridge.tmux_manager import restart_bridge

    bridge_args: list[str] = []
    if args.host != "0.0.0.0":
        bridge_args.extend(["--host", args.host])
    if args.port != 7777:
        bridge_args.extend(["--port", str(args.port)])
    if args.tts:
        bridge_args.append("--tts")
    if args.voice != "auto":
        bridge_args.extend(["--voice", args.voice])
    if args.rate != 190:
        bridge_args.extend(["--rate", str(args.rate)])
    if args.listen:
        bridge_args.append("--listen")
    if args.no_listen_after:
        bridge_args.append("--no-listen-after")
    if args.listen_timeout != 8:
        bridge_args.extend(["--listen-timeout", str(args.listen_timeout)])
    if args.wake_word:
        bridge_args.append("--wake-word")
    if args.headless:
        bridge_args.append("--headless")
    if getattr(args, "openai_tts", False):
        bridge_args.append("--openai-tts")
    if args.tls:
        bridge_args.append("--tls")

    restart_bridge(bridge_args, attach=not args.no_attach)


def _cmd_status(args: argparse.Namespace) -> None:
    """Handle the 'status' subcommand."""
    from bridge.tmux_manager import print_status
    print_status()


def _cmd_cleanup(args: argparse.Namespace) -> None:
    """Handle the 'cleanup' subcommand."""
    from bridge.tmux_manager import cleanup_stale_sessions

    console.print("[cyan]Cleaning up stale VoxHerd tmux sessions...[/cyan]")
    killed = cleanup_stale_sessions()
    if killed:
        console.print(f"[green]Cleaned up {len(killed)} session(s): {', '.join(killed)}[/green]")
    else:
        console.print("[dim]No stale sessions found.[/dim]")


def _cmd_attach(args: argparse.Namespace) -> None:
    """Handle the 'attach' subcommand."""
    from bridge.tmux_manager import session_exists, BRIDGE_SESSION

    if not session_exists(BRIDGE_SESSION):
        console.print(f"[red]No bridge session found ({BRIDGE_SESSION}).[/red]")
        console.print("[dim]Start one with: python -m bridge start[/dim]")
        sys.exit(1)

    os.execvp("tmux", ["tmux", "attach-session", "-t", BRIDGE_SESSION])


def _cmd_qr(args: argparse.Namespace) -> None:
    """Handle the 'qr' subcommand: print a QR code for iOS pairing."""
    import socket
    import urllib.parse

    from bridge.auth import ensure_auth_token

    token = ensure_auth_token()
    port = args.port

    # Determine host IP
    host = args.host
    if not host or host == "auto":
        # Try to get the primary LAN IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("1.1.1.1", 80))
            host = s.getsockname()[0]
            s.close()
        except Exception:
            host = "127.0.0.1"

    # Check for Tailscale
    ts_info = detect_tailscale(port)

    # Build voxherd:// URL (same format as macOS app)
    params = {"host": host, "port": str(port), "token": token}
    if ts_info:
        params["tailscale"] = ts_info["ip"]
    query = urllib.parse.urlencode(params)
    payload = f"voxherd://connect?{query}"

    try:
        import qrcode
    except ImportError:
        console.print("[red]qrcode package not installed. Run: pip install qrcode[/red]")
        sys.exit(1)

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)

    console.print()
    console.print("[bold]Scan this QR code with the VoxHerd iOS app:[/bold]")
    console.print()

    # Print inverted ASCII QR (dark background terminals need inverted colors)
    qr.print_ascii(invert=True)

    console.print()
    console.print(f"[dim]Bridge:[/dim] [cyan]{host}:{port}[/cyan]")
    if ts_info:
        console.print(f"[dim]Tailscale:[/dim] [cyan]{ts_info['ip']}[/cyan] ({ts_info['hostname']})")
    console.print(f"[dim]Token:[/dim] [cyan]{token[:8]}...{token[-4:]}[/cyan]")
    console.print()
    console.print("[dim]Or manually enter the token in iOS Settings:[/dim]")
    console.print(f"[dim]{token}[/dim]")
    console.print()


def _cmd_run(args: argparse.Namespace) -> None:
    """Handle the 'run' subcommand (internal: run bridge directly)."""
    _run_bridge_directly(args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI args and dispatch to the appropriate subcommand."""
    parser = argparse.ArgumentParser(
        prog="voxherd",
        description="VoxHerd Bridge Server -- voice remote control for Claude Code",
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- start --
    p_start = subparsers.add_parser(
        "start",
        help="Start the bridge in a managed tmux session (default)",
    )
    _add_bridge_args(p_start)
    p_start.add_argument("--no-attach", action="store_true", help="Don't attach to the tmux session after starting")
    p_start.add_argument("--force", action="store_true", help="Force restart if bridge is already running")

    # -- stop --
    subparsers.add_parser("stop", help="Stop the bridge server")

    # -- restart --
    p_restart = subparsers.add_parser("restart", help="Restart the bridge server")
    _add_bridge_args(p_restart)
    p_restart.add_argument("--no-attach", action="store_true", help="Don't attach after restarting")

    # -- status --
    subparsers.add_parser("status", help="Show bridge and tmux session status")

    # -- cleanup --
    subparsers.add_parser("cleanup", help="Clean up stale VoxHerd tmux sessions")

    # -- attach --
    subparsers.add_parser("attach", help="Attach to the bridge tmux session")

    # -- qr --
    p_qr = subparsers.add_parser("qr", help="Show QR code for iOS app pairing")
    p_qr.add_argument("--host", default="auto", help="Bridge host IP (default: auto-detect)")
    p_qr.add_argument("--port", type=int, default=7777, help="Bridge port (default: 7777)")

    # -- run (internal) --
    p_run = subparsers.add_parser(
        "run",
        help="Run the bridge directly (internal; use 'start' instead)",
    )
    _add_bridge_args(p_run)

    # Backward compatibility: `python -m bridge --tts --listen` (old-style)
    # must be detected BEFORE parse_args() which would fail on unknown flags.
    raw_args = sys.argv[1:]
    if raw_args and raw_args[0].startswith("--"):
        # Old-style invocation — run directly (same as old behavior)
        fallback_parser = argparse.ArgumentParser(description="VoxHerd Bridge Server")
        _add_bridge_args(fallback_parser)
        fallback_args = fallback_parser.parse_args()
        _run_bridge_directly(fallback_args)
        return

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "start": _cmd_start,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "status": _cmd_status,
        "cleanup": _cmd_cleanup,
        "attach": _cmd_attach,
        "qr": _cmd_qr,
        "run": _cmd_run,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
