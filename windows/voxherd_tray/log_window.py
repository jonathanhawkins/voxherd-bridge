"""Log viewer window for VoxHerd bridge events.

Displays recent bridge events in a scrollable, color-coded text widget
with filtering and auto-scroll. Mirrors the macOS LogView.swift.

Uses Toplevel (not Tk) so it can share the single Tk root managed by TrayApp.
"""

import tkinter as tk
from tkinter import scrolledtext
from typing import Callable

from voxherd_tray.bridge_manager import LogEntry


# Level -> (foreground color, tag name)
_LEVEL_COLORS: dict[str, str] = {
    "success": "#4ec9b0",   # green
    "warning": "#dcdcaa",   # yellow
    "error": "#f14c4c",     # red
    "info": "#4fc1ff",      # cyan
}

_DEFAULT_COLOR = "#d4d4d4"  # light gray


class LogWindow:
    """Tkinter Toplevel window displaying a scrollable, color-coded event log."""

    def __init__(
        self,
        root: tk.Tk,
        initial_entries: list[LogEntry] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._on_close = on_close
        self._text: scrolledtext.ScrolledText | None = None
        self._entry_count: int = 0
        self._max_entries: int = 500
        self._window: tk.Toplevel | None = None

        self._create(root, initial_entries or [])

    def _create(self, root: tk.Tk, initial_entries: list[LogEntry]) -> None:
        """Create and display the log viewer."""
        self._window = tk.Toplevel(root)
        self._window.title("VoxHerd - Bridge Logs")
        self._window.geometry("700x500")
        self._window.configure(bg="#1e1e1e")
        self._window.protocol("WM_DELETE_WINDOW", self._close)

        # Toolbar
        toolbar = tk.Frame(self._window, bg="#2d2d2d", pady=4, padx=8)
        toolbar.pack(fill=tk.X)

        count_var = tk.StringVar(value="0 events")
        self._count_var = count_var
        count_label = tk.Label(
            toolbar,
            textvariable=count_var,
            font=("Consolas", 9),
            fg="#888888",
            bg="#2d2d2d",
        )
        count_label.pack(side=tk.LEFT)

        clear_btn = tk.Button(
            toolbar,
            text="Clear",
            command=self._clear,
            font=("Segoe UI", 9),
            bg="#3c3c3c",
            fg="white",
            relief=tk.FLAT,
            padx=8,
            pady=2,
        )
        clear_btn.pack(side=tk.RIGHT)

        # Log text area
        self._text = scrolledtext.ScrolledText(
            self._window,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg=_DEFAULT_COLOR,
            insertbackground="white",
            state=tk.DISABLED,
            borderwidth=0,
            padx=8,
            pady=4,
        )
        self._text.pack(fill=tk.BOTH, expand=True)

        # Configure color tags
        for level, color in _LEVEL_COLORS.items():
            self._text.tag_configure(level, foreground=color)
        self._text.tag_configure("timestamp", foreground="#666666")
        self._text.tag_configure("project", foreground="#569cd6", font=("Consolas", 9, "bold"))

        # Load initial entries
        for entry in initial_entries:
            self._insert_entry(entry)

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

    def append_entry(self, entry: LogEntry) -> None:
        """Append a new log entry. Safe to call from the tk thread."""
        self._insert_entry(entry)

    def _insert_entry(self, entry: LogEntry) -> None:
        """Insert a log entry into the text widget."""
        if self._text is None:
            return

        self._text.configure(state=tk.NORMAL)

        # Trim old entries
        if self._entry_count >= self._max_entries:
            self._text.delete("1.0", "2.0")
            self._entry_count -= 1

        # Format timestamp (extract HH:MM:SS from ISO 8601)
        ts = entry.timestamp
        if "T" in ts:
            time_part = ts.split("T")[1]
            for sep in ("+", "-", "Z"):
                if sep in time_part and sep != time_part[0]:
                    time_part = time_part.split(sep)[0]
                    break
            ts = time_part

        level_tag = entry.level if entry.level in _LEVEL_COLORS else "info"

        self._text.insert(tk.END, ts + " ", "timestamp")
        self._text.insert(tk.END, entry.project + " ", "project")
        self._text.insert(tk.END, entry.message + "\n", level_tag)

        self._entry_count += 1
        self._count_var.set(f"{self._entry_count} event{'s' if self._entry_count != 1 else ''}")

        # Auto-scroll to bottom
        self._text.see(tk.END)
        self._text.configure(state=tk.DISABLED)

    def _clear(self) -> None:
        """Clear all log entries."""
        if self._text is None:
            return
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)
        self._entry_count = 0
        self._count_var.set("0 events")

    def _close(self) -> None:
        """Close the window and notify the parent."""
        if self._window:
            self._window.destroy()
            self._window = None
        if self._on_close:
            self._on_close()
