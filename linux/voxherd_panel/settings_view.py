"""Settings page — bridge port, auth token, TTS toggle, system actions."""

import json
import os
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk

from voxherd_panel.bridge_client import BridgeClient

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "voxherd")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "panel.json")
_AUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".voxherd", "auth_token")


def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(config: dict) -> None:
    os.makedirs(_CONFIG_DIR, mode=0o700, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


class SettingsPage(Adw.PreferencesPage):
    """Settings panel with bridge configuration and system actions."""

    def __init__(self, client: BridgeClient) -> None:
        super().__init__()
        self._client = client
        self._config = _load_config()
        self._show_token = False

        # -- Bridge Server group --
        bridge_group = Adw.PreferencesGroup(title="Bridge Server")
        self.add(bridge_group)

        # Port
        self._port_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self._port_row.set_title("Port")
        self._port_row.set_value(self._config.get("port", 7777))
        self._port_row.connect("changed", self._on_port_changed)
        bridge_group.add(self._port_row)

        # -- Auth Token group --
        auth_group = Adw.PreferencesGroup(title="Auth Token")
        self.add(auth_group)

        # Token display row
        self._token_row = Adw.ActionRow(title="Token")
        self._update_token_display()

        reveal_btn = Gtk.Button(label="Reveal")
        reveal_btn.set_valign(Gtk.Align.CENTER)
        reveal_btn.add_css_class("flat")
        reveal_btn.connect("clicked", self._on_toggle_reveal)
        self._reveal_btn = reveal_btn
        self._token_row.add_suffix(reveal_btn)

        copy_btn = Gtk.Button(label="Copy")
        copy_btn.set_valign(Gtk.Align.CENTER)
        copy_btn.add_css_class("flat")
        copy_btn.connect("clicked", self._on_copy_token)
        self._copy_btn = copy_btn
        self._token_row.add_suffix(copy_btn)

        auth_group.add(self._token_row)

        # -- Voice group --
        voice_group = Adw.PreferencesGroup(title="Voice Features")
        self.add(voice_group)

        self._tts_row = Adw.SwitchRow(title="Text-to-Speech (TTS)")
        self._tts_row.set_subtitle("Announce events via espeak-ng")
        self._tts_row.set_active(self._config.get("tts", True))
        self._tts_row.connect("notify::active", self._on_tts_changed)
        voice_group.add(self._tts_row)

        # -- System group --
        system_group = Adw.PreferencesGroup(title="System")
        self.add(system_group)

        # Install hooks
        hooks_row = Adw.ActionRow(title="Install Hooks")
        hooks_row.set_subtitle("Deploy Claude Code hook scripts to ~/.voxherd/hooks/")
        hooks_btn = Gtk.Button(label="Install")
        hooks_btn.set_valign(Gtk.Align.CENTER)
        hooks_btn.connect("clicked", self._on_install_hooks)
        self._hooks_btn = hooks_btn
        hooks_row.add_suffix(hooks_btn)
        system_group.add(hooks_row)

        # Open logs folder
        logs_row = Adw.ActionRow(title="Logs Folder")
        logs_row.set_subtitle("~/.voxherd/logs/")
        logs_btn = Gtk.Button(label="Open")
        logs_btn.set_valign(Gtk.Align.CENTER)
        logs_btn.add_css_class("flat")
        logs_btn.connect("clicked", self._on_open_logs)
        logs_row.add_suffix(logs_btn)
        system_group.add(logs_row)

        # -- About group --
        about_group = Adw.PreferencesGroup(title="About")
        self.add(about_group)

        from voxherd_panel import __version__
        version_row = Adw.ActionRow(title="Version")
        version_label = Gtk.Label(label=__version__)
        version_label.add_css_class("dim-label")
        version_label.set_valign(Gtk.Align.CENTER)
        version_row.add_suffix(version_label)
        about_group.add(version_row)

    def _load_token(self) -> str:
        try:
            with open(_AUTH_TOKEN_FILE) as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def _update_token_display(self) -> None:
        token = self._load_token()
        if not token:
            self._token_row.set_subtitle("No token found")
            return
        if self._show_token:
            self._token_row.set_subtitle(token)
        else:
            masked = f"{token[:8]}...{token[-4:]}" if len(token) > 12 else "***"
            self._token_row.set_subtitle(masked)

    def _on_toggle_reveal(self, btn: Gtk.Button) -> None:
        self._show_token = not self._show_token
        btn.set_label("Hide" if self._show_token else "Reveal")
        self._update_token_display()

    def _on_copy_token(self, btn: Gtk.Button) -> None:
        token = self._load_token()
        if not token:
            return
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(token)
        btn.set_label("Copied!")
        GLib.timeout_add_seconds(2, lambda: (btn.set_label("Copy"), False)[-1])
        # Auto-clear clipboard after 30s
        GLib.timeout_add_seconds(30, lambda: (clipboard.set(""), False)[-1])

    def _on_port_changed(self, row: Adw.SpinRow) -> None:
        self._config["port"] = int(row.get_value())
        _save_config(self._config)

    def _on_tts_changed(self, row: Adw.SwitchRow, _pspec) -> None:
        self._config["tts"] = row.get_active()
        _save_config(self._config)

    def _on_install_hooks(self, btn: Gtk.Button) -> None:
        # Look for install.sh in known locations
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "share", "voxherd", "hooks", "install.sh"),
            "/opt/voxherd/hooks/install.sh",
        ]
        install_script = None
        for path in candidates:
            if os.path.isfile(path):
                install_script = path
                break

        if not install_script:
            btn.set_label("Not found")
            GLib.timeout_add_seconds(2, lambda: (btn.set_label("Install"), False)[-1])
            return

        try:
            result = subprocess.run(
                ["bash", install_script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                btn.set_label("Installed!")
            else:
                btn.set_label("Failed")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            btn.set_label("Error")

        GLib.timeout_add_seconds(2, lambda: (btn.set_label("Install"), False)[-1])

    def _on_open_logs(self, _btn: Gtk.Button) -> None:
        logs_dir = os.path.join(os.path.expanduser("~"), ".voxherd", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", logs_dir])
        except FileNotFoundError:
            pass
