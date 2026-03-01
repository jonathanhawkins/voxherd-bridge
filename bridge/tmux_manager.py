"""Tmux session lifecycle management for VoxHerd.

Handles starting/stopping the bridge in tmux, cleaning up stale sessions,
and health-checking running instances. All functions are synchronous
(called before the async event loop starts or from CLI commands).
"""

import os
import signal
import socket
import subprocess
import sys
import time

from rich.console import Console
from rich.table import Table

from bridge.env_utils import get_subprocess_env

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRIDGE_SESSION = "vh-bridge"          # tmux session name for the bridge server
VC_PREFIX = "vh-"                     # prefix for all VoxHerd-spawned sessions
BRIDGE_PORT = 7777                    # default bridge port
BRIDGE_PIDFILE = os.path.expanduser("~/.voxherd/bridge.pid")


# ---------------------------------------------------------------------------
# Low-level tmux helpers
# ---------------------------------------------------------------------------


def _run_tmux(*args: str, timeout: int = 5) -> subprocess.CompletedProcess:
    """Run a tmux command with a timeout. Returns CompletedProcess."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=get_subprocess_env(),
    )


def tmux_server_running() -> bool:
    """Check if the tmux server is running at all."""
    try:
        result = _run_tmux("list-sessions")
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def list_sessions() -> list[dict]:
    """List all tmux sessions with metadata.

    Returns a list of dicts with keys: name, windows, created, attached, activity.
    """
    try:
        fmt = "#{session_name}\t#{session_windows}\t#{session_created}\t#{session_attached}\t#{session_activity}"
        result = _run_tmux("list-sessions", "-F", fmt)
        if result.returncode != 0:
            return []
        sessions = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 5:
                sessions.append({
                    "name": parts[0],
                    "windows": int(parts[1]),
                    "created": int(parts[2]),
                    "attached": parts[3] == "1",
                    "activity": int(parts[4]),
                })
        return sessions
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


async def async_list_sessions() -> list[dict]:
    """Async version of list_sessions() using asyncio subprocess.

    Preferred in async contexts (activity poll loop, route handlers)
    because synchronous subprocess.run() can fail when the bridge
    runs inside a macOS app bundle (different process context).
    """
    import asyncio
    try:
        fmt = "#{session_name}\t#{session_windows}\t#{session_created}\t#{session_attached}\t#{session_activity}"
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-sessions", "-F", fmt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        sessions = []
        for line in stdout.decode().strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 5:
                sessions.append({
                    "name": parts[0],
                    "windows": int(parts[1]),
                    "created": int(parts[2]),
                    "attached": parts[3] == "1",
                    "activity": int(parts[4]),
                })
        return sessions
    except Exception:
        return []


def list_voxherd_sessions() -> list[dict]:
    """List only VoxHerd tmux sessions (prefixed with 'vh-')."""
    return [s for s in list_sessions() if s["name"].startswith(VC_PREFIX)]


def session_exists(name: str) -> bool:
    """Check if a tmux session with the given name exists."""
    try:
        result = _run_tmux("has-session", "-t", name)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def session_has_live_process(name: str) -> bool:
    """Check if a tmux session's pane has a running process (not just a dead shell).

    Returns True if the pane's current command is not a bare shell (zsh/bash).
    Uses ``list-panes -F`` which correctly errors on nonexistent targets
    (unlike ``display-message`` which silently falls back to the current session).
    """
    try:
        result = _run_tmux(
            "list-panes", "-t", name, "-F", "#{pane_current_command}",
        )
        if result.returncode != 0:
            return False
        cmd = result.stdout.strip().splitlines()[0].lower() if result.stdout.strip() else ""
        # If the pane is running a shell, the process it was hosting has exited
        return cmd not in ("zsh", "bash", "sh", "fish", "")
    except (subprocess.TimeoutExpired, FileNotFoundError, IndexError):
        return False


async def async_session_has_live_process(name: str) -> bool:
    """Async version of session_has_live_process().

    Uses asyncio subprocess to avoid blocking the event loop.
    """
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", name, "-F", "#{pane_current_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return False
        cmd = stdout.decode().strip().splitlines()[0].lower() if stdout.decode().strip() else ""
        return cmd not in ("zsh", "bash", "sh", "fish", "")
    except Exception:
        return False


def kill_session(name: str) -> bool:
    """Kill a tmux session by name. Returns True if successful."""
    try:
        result = _run_tmux("kill-session", "-t", name)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def send_keys(target: str, keys: str) -> bool:
    """Send keys to a tmux target. Returns True if successful."""
    try:
        result = _run_tmux("send-keys", "-t", target, keys)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Port checking
# ---------------------------------------------------------------------------


def is_port_listening(port: int = BRIDGE_PORT) -> bool:
    """Check if something is listening on the given port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


def wait_for_port(port: int = BRIDGE_PORT, timeout: int = 10) -> bool:
    """Wait until a port starts listening. Returns True if it came up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_listening(port):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Stale session cleanup
# ---------------------------------------------------------------------------


def cleanup_stale_sessions() -> list[str]:
    """Find and kill stale VoxHerd tmux sessions.

    A session is considered stale if:
    1. It starts with 'vh-' prefix (VoxHerd-managed)
    2. Its pane is running a bare shell (the process it hosted has exited)

    The vh-bridge session is only killed if its process has exited.
    Returns list of killed session names.
    """
    killed: list[str] = []

    for session in list_voxherd_sessions():
        name = session["name"]

        # Check if the session's process is still alive
        if not session_has_live_process(name):
            if kill_session(name):
                killed.append(name)
                console.print(f"  [yellow]Cleaned up stale session:[/yellow] {name}")

    return killed


# ---------------------------------------------------------------------------
# Bridge lifecycle
# ---------------------------------------------------------------------------


def get_bridge_status() -> dict:
    """Get the current status of the bridge.

    Returns a dict with:
        running: bool - whether the bridge tmux session exists
        process_alive: bool - whether the bridge process is still running
        port_listening: bool - whether port 7777 is responding
        session_name: str - the tmux session name (if exists)
    """
    exists = session_exists(BRIDGE_SESSION)
    process_alive = session_has_live_process(BRIDGE_SESSION) if exists else False
    port_up = is_port_listening(BRIDGE_PORT)

    return {
        "running": exists and process_alive,
        "process_alive": process_alive,
        "port_listening": port_up,
        "session_name": BRIDGE_SESSION if exists else None,
        "tmux_exists": exists,
    }


def stop_bridge(graceful_timeout: int = 5) -> bool:
    """Stop the bridge server gracefully.

    Sends Ctrl-C to the tmux session and waits for the process to exit.
    If it doesn't exit within the timeout, kills the session.

    Returns True if the bridge was stopped (or wasn't running).
    """
    status = get_bridge_status()
    if not status["tmux_exists"]:
        console.print("[dim]Bridge is not running.[/dim]")
        return True

    console.print(f"[yellow]Stopping bridge ({BRIDGE_SESSION})...[/yellow]")

    # Send Ctrl-C for graceful shutdown
    send_keys(BRIDGE_SESSION, "C-c")

    # Wait for the process to exit
    deadline = time.monotonic() + graceful_timeout
    while time.monotonic() < deadline:
        if not session_has_live_process(BRIDGE_SESSION):
            break
        time.sleep(0.5)

    # Kill the session (removes the tmux window entirely)
    if session_exists(BRIDGE_SESSION):
        kill_session(BRIDGE_SESSION)

    # Verify port is free
    time.sleep(0.5)
    if is_port_listening(BRIDGE_PORT):
        console.print("[red]Warning: Port 7777 still in use after stopping bridge.[/red]")
        return False

    console.print("[green]Bridge stopped.[/green]")
    return True


def start_bridge(
    args: list[str],
    *,
    attach: bool = True,
    force: bool = False,
) -> bool:
    """Start the bridge server in a tmux session.

    Args:
        args: Command-line arguments to pass to the bridge (e.g. ['--tts', '--listen']).
        attach: If True, attach to the tmux session after starting.
        force: If True, stop any existing bridge first.

    Returns True if the bridge was started successfully.
    """
    status = get_bridge_status()

    # Handle existing bridge
    if status["tmux_exists"]:
        if status["running"] and status["port_listening"]:
            if force:
                console.print("[yellow]Force-stopping existing bridge...[/yellow]")
                stop_bridge()
            else:
                console.print("[green]Bridge is already running.[/green]")
                if attach:
                    console.print(f"[dim]Attaching to {BRIDGE_SESSION}...[/dim]")
                    _attach_session(BRIDGE_SESSION)
                return True
        else:
            # Session exists but process is dead -- clean it up
            console.print("[yellow]Cleaning up dead bridge session...[/yellow]")
            kill_session(BRIDGE_SESSION)

    # Check if port is already in use by something else
    if is_port_listening(BRIDGE_PORT):
        console.print(
            f"[red]Port {BRIDGE_PORT} is already in use by another process.[/red]\n"
            f"[dim]Kill it first or use --port to specify a different port.[/dim]"
        )
        return False

    # Clean up any other stale vh-* sessions
    cleaned = cleanup_stale_sessions()
    if cleaned:
        console.print(f"[yellow]Cleaned up {len(cleaned)} stale session(s).[/yellow]")

    # Determine the bridge launch command
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_python = os.path.join(repo_root, "bridge", ".venv", "bin", "python")

    if not os.path.isfile(venv_python):
        # Fall back to system Python if no venv
        venv_python = sys.executable

    bridge_cmd = [venv_python, "-m", "bridge", "run", *args]
    cmd_str = " ".join(bridge_cmd)

    # Create the tmux session
    console.print(f"[cyan]Starting bridge in tmux session '{BRIDGE_SESSION}'...[/cyan]")

    # Set PATH so claude CLI and other tools are available inside the session
    path_env = f"/usr/local/bin:/opt/homebrew/bin:{os.environ.get('HOME', '')}/.local/bin:{os.environ.get('HOME', '')}/.claude/local:{os.environ.get('PATH', '')}"

    try:
        result = _run_tmux(
            "new-session", "-d",
            "-s", BRIDGE_SESSION,
            "-c", repo_root,
            "--",
            "env", f"PATH={path_env}", *bridge_cmd,
        )
        if result.returncode != 0:
            console.print(f"[red]Failed to create tmux session: {result.stderr.strip()}[/red]")
            return False
    except subprocess.TimeoutExpired:
        console.print("[red]Timed out creating tmux session.[/red]")
        return False

    # Wait for the bridge to start listening
    console.print("[dim]Waiting for bridge to start...[/dim]")
    if wait_for_port(BRIDGE_PORT, timeout=15):
        console.print(f"[green]Bridge is running on port {BRIDGE_PORT}.[/green]")
    else:
        console.print(
            f"[yellow]Bridge tmux session created but port {BRIDGE_PORT} not responding yet.[/yellow]\n"
            f"[dim]Check the session with: tmux attach -t {BRIDGE_SESSION}[/dim]"
        )

    if attach:
        console.print(f"[dim]Attaching to {BRIDGE_SESSION}... (detach with Ctrl-B d)[/dim]")
        _attach_session(BRIDGE_SESSION)

    return True


def restart_bridge(args: list[str], *, attach: bool = True) -> bool:
    """Restart the bridge server.

    Stops any existing bridge, then starts a new one.
    """
    stop_bridge()
    time.sleep(1)
    return start_bridge(args, attach=attach, force=False)


def _attach_session(name: str) -> None:
    """Attach to a tmux session, replacing the current process."""
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------


def print_status() -> None:
    """Print a comprehensive status overview."""
    bridge = get_bridge_status()

    # Bridge status
    console.print()
    if bridge["running"] and bridge["port_listening"]:
        console.print(f"[green]Bridge: running[/green] (session: {BRIDGE_SESSION}, port {BRIDGE_PORT})")
    elif bridge["tmux_exists"] and not bridge["process_alive"]:
        console.print(f"[red]Bridge: dead[/red] (tmux session exists but process exited)")
    elif bridge["port_listening"]:
        console.print(f"[yellow]Bridge: port {BRIDGE_PORT} in use[/yellow] (not in managed tmux session)")
    else:
        console.print("[dim]Bridge: not running[/dim]")

    # VoxHerd tmux sessions
    vc_sessions = list_voxherd_sessions()
    all_sessions = list_sessions()

    if vc_sessions:
        console.print()
        table = Table(title="VoxHerd tmux sessions")
        table.add_column("Name", style="cyan")
        table.add_column("Windows", justify="right")
        table.add_column("Process", style="green")
        table.add_column("Attached")

        for s in vc_sessions:
            alive = session_has_live_process(s["name"])
            table.add_row(
                s["name"],
                str(s["windows"]),
                "alive" if alive else "[red]dead[/red]",
                "yes" if s["attached"] else "no",
            )
        console.print(table)
    else:
        console.print("\n[dim]No VoxHerd tmux sessions found.[/dim]")

    # Other tmux sessions (for context)
    other = [s for s in all_sessions if not s["name"].startswith(VC_PREFIX)]
    if other:
        console.print()
        table = Table(title="Other tmux sessions")
        table.add_column("Name", style="blue")
        table.add_column("Windows", justify="right")
        table.add_column("Attached")

        for s in other:
            table.add_row(
                s["name"],
                str(s["windows"]),
                "yes" if s["attached"] else "no",
            )
        console.print(table)

    # Persisted sessions
    sessions_path = os.path.expanduser("~/.voxherd/sessions.json")
    if os.path.isfile(sessions_path):
        import json
        try:
            with open(sessions_path) as f:
                data = json.load(f)
            registered = data.get("sessions", {})
            if registered:
                console.print()
                table = Table(title="Registered bridge sessions")
                table.add_column("Project", style="cyan")
                table.add_column("Status")
                table.add_column("Tmux target")
                table.add_column("Last activity")

                for s in registered.values():
                    tmux = s.get("tmux_target", "")
                    status_style = {
                        "active": "green",
                        "idle": "dim",
                        "waiting": "yellow",
                    }.get(s.get("status", ""), "white")
                    table.add_row(
                        s.get("project", "?"),
                        f"[{status_style}]{s.get('status', '?')}[/{status_style}]",
                        tmux or "[dim]none[/dim]",
                        s.get("last_activity", "?")[:19],
                    )
                console.print(table)
        except Exception:
            pass

    console.print()
