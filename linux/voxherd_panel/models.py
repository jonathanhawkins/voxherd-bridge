"""Data models mirroring macos/VoxHerdBridge/Sources/Models.swift."""

from dataclasses import dataclass, field

# Activity type -> (Adwaita icon name, CSS color class, human label)
ACTIVITY_MAP: dict[str, tuple[str, str, str]] = {
    "thinking":    ("brain-augemnted-symbolic",    "blue",      "Thinking"),
    "writing":     ("document-edit-symbolic",      "blue",      "Writing"),
    "searching":   ("system-search-symbolic",      "blue",      "Searching"),
    "running":     ("media-playback-start-symbolic", "orange",  "Running"),
    "building":    ("build-alt-symbolic",          "orange",    "Building"),
    "testing":     ("checkbox-checked-symbolic",   "orange",    "Testing"),
    "working":     ("emblem-system-symbolic",      "blue",      "Working"),
    "completed":   ("emblem-ok-symbolic",          "green",     "Completed"),
    "sleeping":    ("weather-clear-night-symbolic", "gray",     "Idle"),
    "errored":     ("dialog-warning-symbolic",     "red",       "Error"),
    "stopped":     ("media-playback-stop-symbolic", "gray",     "Stopped"),
    "approval":    ("hand-open-symbolic",          "yellow",    "Needs Approval"),
    "input":       ("dialog-question-symbolic",    "yellow",    "Needs Input"),
    "registered":  ("thunderbolt-symbolic",        "cyan",      "Starting"),
    "dead":        ("window-close-symbolic",       "red",       "Dead"),
}

_DEFAULT_ENTRY = ("circle-outline-symbolic", "gray", "Unknown")


@dataclass
class SessionInfo:
    """A Claude Code session tracked by the bridge server."""

    session_id: str = ""
    project: str = "unknown"
    status: str = "active"
    activity_type: str = "registered"
    last_summary: str = ""
    agent_number: int = 1
    sub_agent_count: int = 0
    last_activity: str = ""

    @property
    def activity_icon(self) -> str:
        return ACTIVITY_MAP.get(self.activity_type, _DEFAULT_ENTRY)[0]

    @property
    def status_color(self) -> str:
        return ACTIVITY_MAP.get(self.activity_type, _DEFAULT_ENTRY)[1]

    @property
    def activity_label(self) -> str:
        return ACTIVITY_MAP.get(self.activity_type, _DEFAULT_ENTRY)[2]

    @property
    def is_active(self) -> bool:
        return self.activity_type in (
            "thinking", "writing", "searching", "running",
            "building", "testing", "working", "registered",
        )

    @property
    def needs_attention(self) -> bool:
        return self.activity_type in ("approval", "input")

    @classmethod
    def from_dict(cls, data: dict) -> "SessionInfo":
        return cls(
            session_id=data.get("session_id", ""),
            project=data.get("project", "unknown"),
            status=data.get("status", "active"),
            activity_type=data.get("activity_type", "registered"),
            last_summary=data.get("last_summary", ""),
            agent_number=data.get("agent_number", 1),
            sub_agent_count=data.get("sub_agent_count", 0),
            last_activity=data.get("last_activity", ""),
        )


@dataclass
class LogEntry:
    """A structured log entry from the bridge server."""

    timestamp: str = ""
    level: str = "info"
    project: str = ""
    message: str = ""

    @property
    def level_color(self) -> str:
        return {
            "success": "green",
            "warning": "yellow",
            "error": "red",
            "info": "cyan",
        }.get(self.level, "gray")
