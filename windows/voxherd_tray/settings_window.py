"""Settings window for the VoxHerd Windows tray app.

Provides controls for bridge port, TTS toggle, auto-start, launch at login,
hook installation, auth token display, and logs folder access. Mirrors the
macOS SettingsView.swift feature set.

Uses Toplevel (not Tk) so it can share the single Tk root managed by TrayApp.
"""

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Callable

from voxherd_tray.bridge_manager import BridgeManager, BridgeState
from voxherd_tray.config import APPDATA_DIR, LOGS_DIR, Preferences


class SettingsWindow:
    """Tkinter Toplevel window for editing VoxHerd settings."""

    def __init__(
        self,
        root: tk.Tk,
        prefs: Preferences,
        bridge: BridgeManager,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._prefs = prefs
        self._bridge = bridge
        self._on_close = on_close
        self._window: tk.Toplevel | None = None

        # Tkinter variables (initialized in _create)
        self._port_var: tk.StringVar | None = None
        self._tts_var: tk.BooleanVar | None = None
        self._autostart_var: tk.BooleanVar | None = None
        self._login_var: tk.BooleanVar | None = None
        self._show_token: bool = False
        self._token_label: tk.Label | None = None

        self._create(root)

    def _create(self, root: tk.Tk) -> None:
        """Create and display the settings window."""
        self._window = tk.Toplevel(root)
        self._window.title("VoxHerd - Settings")
        self._window.geometry("420x560")
        self._window.resizable(False, False)
        self._window.configure(bg="#1e1e1e")
        self._window.protocol("WM_DELETE_WINDOW", self._close)

        # Initialize Tkinter variables
        self._port_var = tk.StringVar(value=str(self._prefs.port))
        self._tts_var = tk.BooleanVar(value=self._prefs.enable_tts)
        self._autostart_var = tk.BooleanVar(value=self._prefs.auto_start)
        self._login_var = tk.BooleanVar(value=self._prefs.launch_at_login)

        container = tk.Frame(self._window, bg="#1e1e1e", padx=20, pady=15)
        container.pack(fill=tk.BOTH, expand=True)

        # -- Bridge Server Section --
        self._section_header(container, "Bridge Server")

        port_frame = tk.Frame(container, bg="#1e1e1e")
        port_frame.pack(fill=tk.X, pady=(0, 2))

        tk.Label(
            port_frame, text="Port:", font=("Segoe UI", 10),
            fg="white", bg="#1e1e1e", width=12, anchor="w",
        ).pack(side=tk.LEFT)

        port_entry = tk.Entry(
            port_frame, textvariable=self._port_var, width=8,
            font=("Consolas", 10), bg="#2d2d2d", fg="white",
            insertbackground="white", relief=tk.FLAT,
        )
        port_entry.pack(side=tk.LEFT)

        tk.Label(
            container, text="Valid range: 1024 - 65535",
            font=("Segoe UI", 8), fg="#888888", bg="#1e1e1e",
        ).pack(anchor="w", pady=(0, 8))

        # -- Auth Token Section --
        self._section_header(container, "Auth Token")

        token = self._bridge.auth_token
        if token:
            token_frame = tk.Frame(container, bg="#1e1e1e")
            token_frame.pack(fill=tk.X, pady=(0, 4))

            preview = token[:8] + "..." + token[-4:]
            self._token_label = tk.Label(
                token_frame, text=preview,
                font=("Consolas", 9), fg="#aaaaaa", bg="#1e1e1e", anchor="w",
            )
            self._token_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

            reveal_btn = tk.Button(
                token_frame, text="Reveal",
                command=self._toggle_token_reveal,
                font=("Segoe UI", 8), bg="#3c3c3c", fg="white",
                relief=tk.FLAT, padx=6,
            )
            reveal_btn.pack(side=tk.RIGHT, padx=(4, 0))
            self._reveal_btn = reveal_btn

            btn_frame = tk.Frame(container, bg="#1e1e1e")
            btn_frame.pack(fill=tk.X, pady=(0, 4))

            copy_btn = tk.Button(
                btn_frame, text="Copy Token",
                command=self._copy_token,
                font=("Segoe UI", 9), bg="#3c3c3c", fg="white",
                relief=tk.FLAT, padx=8, pady=2,
            )
            copy_btn.pack(side=tk.LEFT)

            self._copy_status = tk.Label(
                btn_frame, text="", font=("Segoe UI", 8),
                fg="#4ec9b0", bg="#1e1e1e",
            )
            self._copy_status.pack(side=tk.LEFT, padx=(8, 0))

            tk.Label(
                container, text="Paste this into the iOS app's Settings to connect.",
                font=("Segoe UI", 8), fg="#888888", bg="#1e1e1e",
            ).pack(anchor="w", pady=(0, 8))
        else:
            tk.Label(
                container, text="Token will appear after bridge starts.",
                font=("Segoe UI", 9), fg="#888888", bg="#1e1e1e",
            ).pack(anchor="w", pady=(0, 8))

        # -- Voice Features Section --
        self._section_header(container, "Voice Features")

        tts_check = tk.Checkbutton(
            container, text="Text-to-Speech (TTS)",
            variable=self._tts_var, font=("Segoe UI", 10),
            fg="white", bg="#1e1e1e", selectcolor="#2d2d2d",
            activebackground="#1e1e1e", activeforeground="white",
        )
        tts_check.pack(anchor="w", pady=(0, 8))

        # -- System Section --
        self._section_header(container, "System")

        autostart_check = tk.Checkbutton(
            container, text="Auto-start bridge on app launch",
            variable=self._autostart_var, font=("Segoe UI", 10),
            fg="white", bg="#1e1e1e", selectcolor="#2d2d2d",
            activebackground="#1e1e1e", activeforeground="white",
        )
        autostart_check.pack(anchor="w", pady=(0, 2))

        login_check = tk.Checkbutton(
            container, text="Launch at login",
            variable=self._login_var, font=("Segoe UI", 10),
            fg="white", bg="#1e1e1e", selectcolor="#2d2d2d",
            activebackground="#1e1e1e", activeforeground="white",
        )
        login_check.pack(anchor="w", pady=(0, 8))

        sys_btn_frame = tk.Frame(container, bg="#1e1e1e")
        sys_btn_frame.pack(fill=tk.X, pady=(0, 4))

        install_btn = tk.Button(
            sys_btn_frame, text="Install Hooks",
            command=self._install_hooks,
            font=("Segoe UI", 9), bg="#3c3c3c", fg="white",
            relief=tk.FLAT, padx=8, pady=2,
        )
        install_btn.pack(side=tk.LEFT)

        self._hook_status = tk.Label(
            sys_btn_frame, text="", font=("Segoe UI", 8),
            fg="#4ec9b0", bg="#1e1e1e",
        )
        self._hook_status.pack(side=tk.LEFT, padx=(8, 0))

        logs_btn = tk.Button(
            sys_btn_frame, text="Open Logs Folder",
            command=self._open_logs_folder,
            font=("Segoe UI", 9), bg="#3c3c3c", fg="white",
            relief=tk.FLAT, padx=8, pady=2,
        )
        logs_btn.pack(side=tk.RIGHT)

        # -- Spacer + Apply --
        spacer = tk.Frame(container, bg="#1e1e1e")
        spacer.pack(fill=tk.BOTH, expand=True)

        apply_frame = tk.Frame(container, bg="#1e1e1e")
        apply_frame.pack(fill=tk.X, pady=(8, 0))

        apply_btn = tk.Button(
            apply_frame, text="Apply",
            command=self._apply,
            font=("Segoe UI", 10, "bold"), bg="#0e639c", fg="white",
            relief=tk.FLAT, padx=16, pady=4,
        )
        apply_btn.pack(side=tk.RIGHT)

        cancel_btn = tk.Button(
            apply_frame, text="Cancel",
            command=self._close,
            font=("Segoe UI", 10), bg="#3c3c3c", fg="white",
            relief=tk.FLAT, padx=16, pady=4,
        )
        cancel_btn.pack(side=tk.RIGHT, padx=(0, 8))

        # Center on screen
        self._window.update_idletasks()
        w = self._window.winfo_width()
        h = self._window.winfo_height()
        x = (self._window.winfo_screenwidth() - w) // 2
        y = (self._window.winfo_screenheight() - h) // 2
        self._window.geometry(f"+{x}+{y}")

    def focus(self) -> None:
        """Bring the window to front."""
        if self._window and self._window.winfo_exists():
            self._window.lift()
            self._window.focus_force()
        else:
            raise RuntimeError("Window closed")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """Save settings and restart bridge if port changed."""
        if self._port_var is None:
            return

        # Validate port
        try:
            port = int(self._port_var.get())
        except ValueError:
            messagebox.showerror("Invalid Port", "Port must be a number.", parent=self._window)
            return

        if not (1024 <= port <= 65535):
            messagebox.showerror(
                "Invalid Port",
                "Port must be between 1024 and 65535.",
                parent=self._window,
            )
            return

        old_port = self._prefs.port
        self._prefs.port = port
        self._prefs.enable_tts = self._tts_var.get() if self._tts_var else True
        self._prefs.auto_start = self._autostart_var.get() if self._autostart_var else True
        self._prefs.launch_at_login = self._login_var.get() if self._login_var else False

        self._prefs.save()
        self._prefs.apply_launch_at_login()

        # Restart bridge if port changed and bridge is running
        if port != old_port and self._bridge.state == BridgeState.RUNNING:
            self._bridge.restart(port=port, enable_tts=self._prefs.enable_tts)

        self._close()

    def _toggle_token_reveal(self) -> None:
        """Toggle between masked and full token display."""
        token = self._bridge.auth_token
        if not token or not self._token_label:
            return

        self._show_token = not self._show_token
        if self._show_token:
            self._token_label.configure(text=token)
            self._reveal_btn.configure(text="Hide")
        else:
            preview = token[:8] + "..." + token[-4:]
            self._token_label.configure(text=preview)
            self._reveal_btn.configure(text="Reveal")

    def _copy_token(self) -> None:
        """Copy the auth token to the clipboard."""
        token = self._bridge.auth_token
        if not token or not self._window:
            return

        self._window.clipboard_clear()
        self._window.clipboard_append(token)

        if self._copy_status:
            self._copy_status.configure(text="Copied!")
            self._window.after(2000, lambda: self._copy_status.configure(text=""))

    def _install_hooks(self) -> None:
        """Run the hooks install script."""
        # Look for the install script -- prefer PowerShell on Windows
        this_dir = Path(__file__).resolve().parent
        candidates = [
            this_dir.parent.parent / "hooks" / "install-hooks.ps1",
            this_dir.parent.parent / "hooks" / "install.sh",
        ]

        install_script = None
        for candidate in candidates:
            if candidate.exists():
                install_script = candidate
                break

        if install_script is None:
            if self._hook_status:
                self._hook_status.configure(text="Install script not found", fg="#f14c4c")
            return

        try:
            if install_script.suffix == ".ps1":
                result = subprocess.run(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(install_script)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            else:
                # Bash script -- try Git Bash or WSL
                bash_paths = [
                    r"C:\Program Files\Git\bin\bash.exe",
                    "bash",
                ]
                result = None
                for bash in bash_paths:
                    try:
                        result = subprocess.run(
                            [bash, str(install_script)],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        break
                    except FileNotFoundError:
                        continue

                if result is None:
                    if self._hook_status:
                        self._hook_status.configure(text="No bash found", fg="#f14c4c")
                    return

            if result.returncode == 0:
                if self._hook_status:
                    self._hook_status.configure(text="Installed", fg="#4ec9b0")
            else:
                if self._hook_status:
                    self._hook_status.configure(
                        text=f"Failed (exit {result.returncode})", fg="#f14c4c",
                    )
        except (subprocess.TimeoutExpired, OSError) as exc:
            if self._hook_status:
                self._hook_status.configure(text=f"Error: {exc}", fg="#f14c4c")

    def _open_logs_folder(self) -> None:
        """Open the logs directory in Windows Explorer."""
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(LOGS_DIR))  # type: ignore[attr-defined]
        except AttributeError:
            # Fallback for non-Windows (shouldn't happen but be safe)
            subprocess.Popen(["explorer", str(LOGS_DIR)])

    def _close(self) -> None:
        """Close the settings window and notify the parent."""
        if self._window:
            self._window.destroy()
            self._window = None
        if self._on_close:
            self._on_close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _section_header(parent: tk.Frame, text: str) -> None:
        """Add a section header with separator line."""
        frame = tk.Frame(parent, bg="#1e1e1e")
        frame.pack(fill=tk.X, pady=(8, 4))

        tk.Label(
            frame, text=text,
            font=("Segoe UI", 10, "bold"), fg="#569cd6", bg="#1e1e1e",
        ).pack(side=tk.LEFT)

        sep = tk.Frame(frame, bg="#3c3c3c", height=1)
        sep.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0), pady=1)
