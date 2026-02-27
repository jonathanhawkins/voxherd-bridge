#!/usr/bin/env python3
"""Waybar custom module for VoxHerd bridge status.

Standalone script — stdlib only, no venv required.
Outputs a single JSON line for Waybar consumption.

Usage in Waybar config:
    "custom/voxherd": {
        "exec": "python3 ~/.local/share/voxherd/waybar_module.py",
        "return-type": "json",
        "interval": 5,
        "on-click": "python3 -m voxherd_panel"
    }
"""

import json
import os
import sys
import urllib.error
import urllib.request

_AUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".voxherd", "auth_token")
_BRIDGE_URL = os.environ.get("VOXHERD_BRIDGE_URL", "http://127.0.0.1:7777")


def _load_token() -> str:
    try:
        with open(_AUTH_TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _fetch_json(url: str, token: str = "") -> dict | None:
    try:
        req = urllib.request.Request(url, method="GET")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def main() -> None:
    # Health check (unauthenticated)
    health = _fetch_json(f"{_BRIDGE_URL}/health")
    if not health or not health.get("ok"):
        output = {"text": " off", "tooltip": "VoxHerd bridge not running", "class": "offline"}
        print(json.dumps(output))
        return

    # Sessions (authenticated)
    token = _load_token()
    sessions_raw = _fetch_json(f"{_BRIDGE_URL}/api/sessions", token)

    if not sessions_raw or not isinstance(sessions_raw, dict):
        output = {"text": " 0", "tooltip": "VoxHerd: no sessions", "class": "idle"}
        print(json.dumps(output))
        return

    total = len(sessions_raw)
    active = 0
    attention = 0
    projects: list[str] = []

    for sid, info in sessions_raw.items():
        if not isinstance(info, dict):
            continue
        activity = info.get("activity_type", "")
        project = info.get("project", "unknown")

        if activity in ("thinking", "writing", "searching", "running",
                        "building", "testing", "working", "registered"):
            active += 1
        if activity in ("approval", "input"):
            attention += 1

        status_emoji = {
            "thinking": "  ", "writing": "  ", "searching": "  ",
            "running": "  ", "building": "  ", "testing": "  ",
            "working": "  ", "completed": "  ", "sleeping": "  ",
            "errored": "  ", "approval": "  ", "input": "  ",
            "registered": "  ", "dead": "  ",
        }.get(activity, "  ")
        summary = info.get("last_summary", "")
        label = {
            "thinking": "Thinking", "writing": "Writing", "searching": "Searching",
            "running": "Running", "building": "Building", "testing": "Testing",
            "working": "Working", "completed": "Completed", "sleeping": "Idle",
            "errored": "Error", "approval": "Needs Approval", "input": "Needs Input",
            "registered": "Starting", "dead": "Dead",
        }.get(activity, activity)
        detail = summary if summary and activity in ("completed", "sleeping", "stopped") else label
        projects.append(f"{status_emoji} {project}: {detail}")

    idle = total - active - attention

    # Build text: icon + counts
    parts = []
    if active > 0:
        parts.append(f"{active}")
    if attention > 0:
        parts.append(f"{attention}")
    if idle > 0:
        parts.append(f"{idle}")

    text = f" {' '.join(parts)}" if parts else " 0"

    # Tooltip: full session list
    tooltip = f"VoxHerd — {total} session{'s' if total != 1 else ''}\n"
    tooltip += "\n".join(projects) if projects else "No sessions"

    # CSS class
    if attention > 0:
        css_class = "attention"
    elif active > 0:
        css_class = "active"
    elif total > 0:
        css_class = "idle"
    else:
        css_class = "idle"

    output = {"text": text, "tooltip": tooltip, "class": css_class}
    print(json.dumps(output))


if __name__ == "__main__":
    main()
