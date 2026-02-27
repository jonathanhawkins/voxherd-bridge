"""Session dashboard — ListBox with custom rows matching the macOS layout."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Pango

from voxherd_panel.bridge_client import BridgeClient
from voxherd_panel.models import SessionInfo

# CSS color classes for activity states
_CSS_COLORS = {
    "blue": "color: #89b4fa;",     # Catppuccin Blue
    "orange": "color: #fab387;",   # Catppuccin Peach
    "green": "color: #a6e3a1;",    # Catppuccin Green
    "gray": "color: #6c7086;",     # Catppuccin Overlay0
    "red": "color: #f38ba8;",      # Catppuccin Red
    "yellow": "color: #f9e2af;",   # Catppuccin Yellow
    "cyan": "color: #94e2d5;",     # Catppuccin Teal
}


class DashboardPage(Gtk.Box):
    """Session list with empty-state placeholders."""

    def __init__(self, client: BridgeClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._client = client

        # Status bar
        self._status_bar = Gtk.Box(spacing=8)
        self._status_bar.set_margin_start(12)
        self._status_bar.set_margin_end(12)
        self._status_bar.set_margin_top(8)
        self._status_bar.set_margin_bottom(4)

        self._header_label = Gtk.Label(label="Sessions")
        self._header_label.add_css_class("heading")
        self._header_label.set_halign(Gtk.Align.START)
        self._status_bar.append(self._header_label)

        self._badge_box = Gtk.Box(spacing=6)
        self._badge_box.set_halign(Gtk.Align.END)
        self._badge_box.set_hexpand(True)
        self._status_bar.append(self._badge_box)

        self.append(self._status_bar)

        # Scrolled list
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)
        self._list_box.set_margin_top(8)
        self._list_box.set_margin_bottom(12)
        scrolled.set_child(self._list_box)

        # Empty state
        self._empty_status = Adw.StatusPage(
            icon_name="utilities-terminal-symbolic",
            title="No Active Sessions",
            description="Start Claude Code in a tmux pane to see sessions here.",
        )
        self._empty_status.set_visible(True)

        # Stack to switch between list and empty state
        self._stack = Gtk.Stack()
        self._stack.add_named(scrolled, "list")
        self._stack.add_named(self._empty_status, "empty")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

        # Connect to client
        client.on_sessions(self._on_sessions)
        client.on_status(self._on_bridge_status)

    def _on_bridge_status(self, running: bool) -> None:
        if not running:
            self._empty_status.set_icon_name("network-offline-symbolic")
            self._empty_status.set_title("Bridge Not Running")
            self._empty_status.set_description(
                "Start with: systemctl --user start voxherd-bridge"
            )
            self._stack.set_visible_child_name("empty")

    def _on_sessions(self, sessions: list[SessionInfo]) -> None:
        # Clear existing rows
        while True:
            row = self._list_box.get_row_at_index(0)
            if row is None:
                break
            self._list_box.remove(row)

        if not sessions:
            if self._client.bridge_running:
                self._empty_status.set_icon_name("utilities-terminal-symbolic")
                self._empty_status.set_title("No Active Sessions")
                self._empty_status.set_description(
                    "Start Claude Code in a tmux pane to see sessions here."
                )
            self._stack.set_visible_child_name("empty")
        else:
            for session in sessions:
                self._list_box.append(_build_session_row(session))
            self._stack.set_visible_child_name("list")

        # Update badges
        self._update_badges(sessions)

    def _update_badges(self, sessions: list[SessionInfo]) -> None:
        while child := self._badge_box.get_first_child():
            self._badge_box.remove(child)

        active = sum(1 for s in sessions if s.is_active)
        attention = sum(1 for s in sessions if s.needs_attention)
        idle = len(sessions) - active - attention

        if active > 0:
            self._badge_box.append(_badge(f"{active}", "blue"))
        if attention > 0:
            self._badge_box.append(_badge(f"{attention}", "yellow"))
        if idle > 0:
            self._badge_box.append(_badge(f"{idle}", "gray"))


def _badge(text: str, color: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("caption")
    css = _CSS_COLORS.get(color, "")
    if css:
        provider = Gtk.CssProvider()
        provider.load_from_string(f"label {{ {css} font-weight: bold; }}")
        label.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    return label


def _build_session_row(session: SessionInfo) -> Gtk.ListBoxRow:
    """Build a single session row matching the macOS SessionRow layout."""
    row = Gtk.ListBoxRow()

    hbox = Gtk.Box(spacing=8)
    hbox.set_margin_start(8)
    hbox.set_margin_end(8)
    hbox.set_margin_top(6)
    hbox.set_margin_bottom(6)

    # Activity icon
    icon = Gtk.Image.new_from_icon_name(session.activity_icon)
    icon.set_pixel_size(16)
    icon_color = _CSS_COLORS.get(session.status_color, "")
    if icon_color:
        provider = Gtk.CssProvider()
        provider.load_from_string(f"image {{ {icon_color} }}")
        icon.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    hbox.append(icon)

    # Project name + summary/label column
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    vbox.set_hexpand(True)

    name_box = Gtk.Box(spacing=4)
    name_label = Gtk.Label(label=session.project)
    name_label.set_halign(Gtk.Align.START)
    name_label.add_css_class("heading")
    name_label.set_ellipsize(Pango.EllipsizeMode.END)
    name_box.append(name_label)

    if session.agent_number > 1:
        num_label = Gtk.Label(label=f"#{session.agent_number}")
        num_label.add_css_class("caption")
        num_label.add_css_class("dim-label")
        name_box.append(num_label)

    vbox.append(name_box)

    # Summary (idle) or activity label (active)
    if session.last_summary and not session.is_active:
        detail_label = Gtk.Label(label=session.last_summary)
        detail_label.add_css_class("caption")
        detail_label.add_css_class("dim-label")
    else:
        detail_label = Gtk.Label(label=session.activity_label)
        detail_label.add_css_class("caption")
        detail_color = _CSS_COLORS.get(session.status_color, "")
        if detail_color:
            provider = Gtk.CssProvider()
            provider.load_from_string(f"label {{ {detail_color} }}")
            detail_label.get_style_context().add_provider(
                provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    detail_label.set_halign(Gtk.Align.START)
    detail_label.set_ellipsize(Pango.EllipsizeMode.END)
    vbox.append(detail_label)
    hbox.append(vbox)

    # Sub-agent count badge
    if session.sub_agent_count > 0:
        sub_label = Gtk.Label(label=str(session.sub_agent_count))
        sub_label.add_css_class("caption")
        sub_label.set_tooltip_text("Sub-agents")
        hbox.append(sub_label)

    # Attention indicator (yellow dot)
    if session.needs_attention:
        dot = Gtk.DrawingArea()
        dot.set_content_width(8)
        dot.set_content_height(8)
        dot.set_valign(Gtk.Align.CENTER)
        provider = Gtk.CssProvider()
        provider.load_from_string(
            "drawingarea { background: #f9e2af; border-radius: 50%; min-width: 8px; min-height: 8px; }"
        )
        dot.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        hbox.append(dot)

    row.set_child(hbox)
    return row
