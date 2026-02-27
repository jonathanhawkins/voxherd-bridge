"""Assistant/provider helpers for bridge dispatch and session metadata."""

from __future__ import annotations

import os
import re

SUPPORTED_ASSISTANTS: tuple[str, ...] = ("claude", "codex", "gemini")
HOOK_CAPABLE_ASSISTANTS: tuple[str, ...] = ("claude", "gemini")


def normalize_assistant(value: str | None, *, default: str = "claude") -> str:
    """Normalize an assistant id, falling back to a supported default."""
    raw = (value or "").strip().lower()
    if raw in SUPPORTED_ASSISTANTS:
        return raw
    fallback = (default or "claude").strip().lower()
    if fallback in SUPPORTED_ASSISTANTS:
        return fallback
    return "claude"


def is_supported_assistant(value: str | None) -> bool:
    """Return True when *value* is a known assistant id."""
    return (value or "").strip().lower() in SUPPORTED_ASSISTANTS


def default_assistant() -> str:
    """Return the configured default assistant for spawned sessions."""
    return normalize_assistant(os.environ.get("VOXHERD_DEFAULT_ASSISTANT"), default="claude")


def supports_hooks(assistant: str | None) -> bool:
    """Return True if the assistant has a native hook lifecycle."""
    return normalize_assistant(assistant) in HOOK_CAPABLE_ASSISTANTS


def spawn_command_for_assistant(assistant: str | None) -> list[str]:
    """Return the interactive command used to start an assistant session."""
    provider = normalize_assistant(assistant)
    if provider == "claude":
        return ["claude", "--dangerously-skip-permissions", "--chrome"]
    if provider == "codex":
        return ["codex"]
    if provider == "gemini":
        return ["gemini"]
    return ["claude", "--dangerously-skip-permissions", "--chrome"]


def resume_command_for_assistant(
    assistant: str | None, session_id: str, message: str
) -> list[str] | None:
    """Return a non-interactive resume command, or None if unsupported."""
    provider = normalize_assistant(assistant)
    if provider == "claude":
        return [
            "claude",
            "--resume",
            session_id,
            "-p",
            message,
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--chrome",
        ]
    if provider == "codex":
        return ["codex", "exec", "resume", session_id, message]
    # Gemini dispatch should use tmux send-keys for active sessions.
    return None


def looks_like_assistant_process(assistant: str | None, fg_cmd: str) -> bool:
    """Return True when *fg_cmd* appears to be the expected assistant process."""
    cmd = (fg_cmd or "").strip().lower()
    provider = normalize_assistant(assistant)
    if provider == "claude":
        # Claude can report either "claude" or its semver (e.g., "2.1.42")
        return cmd == "claude" or bool(re.match(r"^\d+\.\d+\.\d+$", cmd))
    if provider == "codex":
        # Codex binary may report as "codex" or platform-specific name
        # like "codex-aarch64-apple-darwin" (truncated to "codex-aarch64-a" by tmux)
        return cmd == "codex" or cmd.startswith("codex-")
    if provider == "gemini":
        return cmd == "gemini" or cmd.startswith("gemini-")
    return False


def infer_assistant_from_process(fg_cmd: str) -> str | None:
    """Best-effort assistant inference from tmux foreground command."""
    cmd = (fg_cmd or "").strip().lower()
    if not cmd:
        return None
    # Codex binary may report as "codex" or platform-specific name
    # like "codex-aarch64-apple-darwin" (truncated to "codex-aarch64-a" by tmux)
    if cmd == "codex" or cmd.startswith("codex-"):
        return "codex"
    if cmd == "gemini" or cmd.startswith("gemini-"):
        return "gemini"
    if cmd == "claude" or re.match(r"^\d+\.\d+\.\d+$", cmd):
        return "claude"
    return None
