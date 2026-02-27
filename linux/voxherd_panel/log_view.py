"""Live log view — tails journalctl output for the voxherd-bridge service."""

import json
import os
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk, Pango

_MAX_LINES = 500

# Tag names -> Catppuccin colors
_LEVEL_COLORS = {
    "success": "#a6e3a1",  # Green
    "error": "#f38ba8",    # Red
    "warning": "#f9e2af",  # Yellow
    "info": "#94e2d5",     # Teal
}


class LogPage(Gtk.Box):
    """Live log viewer with filtering and color-coded output."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._process: subprocess.Popen | None = None
        self._io_watch_id: int | None = None
        self._line_count = 0

        # Filter bar
        filter_box = Gtk.Box(spacing=6)
        filter_box.set_margin_start(12)
        filter_box.set_margin_end(12)
        filter_box.set_margin_top(8)
        filter_box.set_margin_bottom(4)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Filter logs...")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_filter_changed)
        filter_box.append(self._search_entry)

        # Level toggle buttons
        self._level_toggles: dict[str, Gtk.ToggleButton] = {}
        for level in ("success", "error", "warning", "info"):
            btn = Gtk.ToggleButton(label=level.capitalize())
            btn.set_active(True)
            btn.add_css_class("flat")
            btn.add_css_class("caption")
            btn.connect("toggled", self._on_filter_changed)
            self._level_toggles[level] = btn
            filter_box.append(btn)

        self.append(filter_box)

        # Text view
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_monospace(True)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.set_margin_start(8)
        self._text_view.set_margin_end(8)
        self._text_view.set_margin_bottom(8)

        self._buffer = self._text_view.get_buffer()

        # Create text tags for log levels
        for level, color in _LEVEL_COLORS.items():
            self._buffer.create_tag(level, foreground=color)
        self._buffer.create_tag("dim", foreground="#6c7086")
        self._buffer.create_tag("bold", weight=Pango.Weight.BOLD)

        scrolled.set_child(self._text_view)
        self.append(scrolled)

        # Clear button
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.set_halign(Gtk.Align.END)
        clear_btn.set_margin_end(12)
        clear_btn.set_margin_bottom(4)
        clear_btn.connect("clicked", self._on_clear)
        self.append(clear_btn)

        # Start tailing
        self._start_tail()

    def _start_tail(self) -> None:
        """Start tailing journalctl for voxherd-bridge."""
        try:
            self._process = subprocess.Popen(
                [
                    "journalctl", "--user",
                    "-u", "voxherd-bridge",
                    "-f", "--output=cat",
                    "-n", "200",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
            )
        except FileNotFoundError:
            self._append_text("journalctl not found. Is systemd available?\n", "error")
            return

        if self._process.stdout:
            # Set non-blocking via os
            import fcntl
            fd = self._process.stdout.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            self._io_watch_id = GLib.io_add_watch(
                self._process.stdout,
                GLib.PRIORITY_DEFAULT,
                GLib.IOCondition.IN | GLib.IOCondition.HUP,
                self._on_stdout_ready,
            )

    def _on_stdout_ready(self, source, condition) -> bool:
        """Called by GLib when journalctl has output ready."""
        if condition & GLib.IOCondition.HUP:
            return False  # pipe closed

        try:
            data = source.read(65536)
            if not data:
                return True
            text = data.decode("utf-8", errors="replace")
        except (BlockingIOError, OSError):
            return True

        for line in text.splitlines():
            if not line.strip():
                continue
            self._process_line(line)

        # Auto-scroll to bottom
        end_iter = self._buffer.get_end_iter()
        self._text_view.scroll_to_iter(end_iter, 0.0, False, 0.0, 1.0)

        return True

    def _process_line(self, line: str) -> None:
        """Parse a log line and append it with appropriate formatting."""
        # Try JSON parse first
        try:
            data = json.loads(line)
            ts = data.get("ts", "")
            level = data.get("level", "info")
            project = data.get("project", "")
            message = data.get("message", "")

            # Apply filter
            if not self._passes_filter(level, project, message):
                return

            # Format: timestamp project message
            if ts:
                # Shorten ISO timestamp to HH:MM:SS
                if "T" in ts:
                    ts = ts.split("T")[1][:8]
                self._append_text(f"{ts} ", "dim")
            if project:
                self._append_text(f"{project} ", "bold")
            self._append_text(f"{message}\n", level)
        except (json.JSONDecodeError, ValueError):
            # Plain text line
            if not self._passes_filter("info", "", line):
                return
            self._append_text(f"{line}\n", "dim")

        self._line_count += 1
        self._trim_lines()

    def _passes_filter(self, level: str, project: str, message: str) -> bool:
        """Check if the line passes the current filter settings."""
        # Level toggle
        if level in self._level_toggles:
            if not self._level_toggles[level].get_active():
                return False

        # Text search
        search_text = self._search_entry.get_text().lower()
        if search_text:
            combined = f"{project} {message}".lower()
            if search_text not in combined:
                return False

        return True

    def _append_text(self, text: str, tag_name: str) -> None:
        """Append text to the buffer with a tag."""
        end_iter = self._buffer.get_end_iter()
        tag = self._buffer.get_tag_table().lookup(tag_name)
        if tag:
            self._buffer.insert_with_tags(end_iter, text, tag)
        else:
            self._buffer.insert(end_iter, text)

    def _trim_lines(self) -> None:
        """Remove oldest lines if we exceed MAX_LINES."""
        if self._line_count > _MAX_LINES:
            # Delete from start until we're back under limit
            excess = self._line_count - _MAX_LINES
            start = self._buffer.get_start_iter()
            # Find the Nth newline
            for _ in range(excess):
                if not start.forward_search("\n", Gtk.TextSearchFlags.TEXT_ONLY, None):
                    break
                result = start.forward_search("\n", Gtk.TextSearchFlags.TEXT_ONLY, None)
                if result:
                    _, end_of_line = result
                    start = end_of_line
                else:
                    break
            self._buffer.delete(self._buffer.get_start_iter(), start)
            self._line_count = _MAX_LINES

    def _on_filter_changed(self, _widget) -> None:
        """Filters are applied on new incoming lines, not retroactively."""
        pass

    def _on_clear(self, _btn: Gtk.Button) -> None:
        self._buffer.set_text("")
        self._line_count = 0

    def stop(self) -> None:
        """Stop the journalctl subprocess."""
        if self._io_watch_id is not None:
            GLib.source_remove(self._io_watch_id)
            self._io_watch_id = None
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
