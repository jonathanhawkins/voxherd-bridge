"""Windows-specific configuration and path management for VoxHerd tray app."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _appdata_dir() -> Path:
    """Return %APPDATA%/VoxHerd/, creating it if necessary."""
    base = os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming"))
    path = Path(base) / "VoxHerd"
    path.mkdir(parents=True, exist_ok=True)
    return path


APPDATA_DIR: Path = _appdata_dir()
CONFIG_FILE: Path = APPDATA_DIR / "config.json"
AUTH_TOKEN_FILE: Path = APPDATA_DIR / "auth_token"
LOGS_DIR: Path = APPDATA_DIR / "logs"
HOOKS_DIR: Path = APPDATA_DIR / "hooks"

DEFAULT_PORT: int = 7777


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

@dataclass
class Preferences:
    """Persistent user preferences loaded from config.json."""

    port: int = DEFAULT_PORT
    enable_tts: bool = True
    auto_start: bool = True
    launch_at_login: bool = False

    @classmethod
    def load(cls) -> "Preferences":
        """Load preferences from the config file, falling back to defaults."""
        prefs = cls()
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                prefs.port = int(data.get("port", DEFAULT_PORT))
                prefs.port = max(1024, min(65535, prefs.port))
                prefs.enable_tts = bool(data.get("enable_tts", True))
                prefs.auto_start = bool(data.get("auto_start", True))
                prefs.launch_at_login = bool(data.get("launch_at_login", False))
            except (json.JSONDecodeError, ValueError, TypeError, OSError):
                pass  # Fall back to defaults
        return prefs

    def save(self) -> None:
        """Persist preferences to config.json."""
        APPDATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "port": self.port,
            "enable_tts": self.enable_tts,
            "auto_start": self.auto_start,
            "launch_at_login": self.launch_at_login,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass  # Best effort

    def apply_launch_at_login(self) -> None:
        """Add or remove a shortcut in the Windows Startup folder.

        Falls back to a .bat file in shell:Startup if winshell/pywin32
        are unavailable.
        """
        try:
            startup_dir = Path(
                os.environ.get(
                    "APPDATA",
                    os.path.expanduser("~/AppData/Roaming"),
                )
            ) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            startup_dir.mkdir(parents=True, exist_ok=True)
            bat_path = startup_dir / "VoxHerd.bat"

            if self.launch_at_login:
                # Create a .bat that launches the tray app with pythonw
                import sys
                python_exe = sys.executable
                # Prefer pythonw.exe for no-console launch
                pythonw = Path(python_exe).parent / "pythonw.exe"
                if not pythonw.exists():
                    pythonw = Path(python_exe)
                bat_path.write_text(
                    f'@echo off\r\nstart "" "{pythonw}" -m voxherd_tray\r\n',
                    encoding="utf-8",
                )
            else:
                if bat_path.exists():
                    bat_path.unlink()
        except OSError:
            pass  # Best effort


def load_auth_token() -> str | None:
    """Read the auth token from disk. Returns None if not found."""
    try:
        token = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        return token if token else None
    except (OSError, FileNotFoundError):
        return None
