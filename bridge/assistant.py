"""Assistant/provider helpers for bridge dispatch and session metadata."""

from __future__ import annotations

import os
import re
from typing import Any

# Canonical assistant ids exposed to clients / spawn UI.
SUPPORTED_ASSISTANTS: tuple[str, ...] = (
    "claude",
    "codex",
    "gemini",
    "grok",
    "composer",
)
HOOK_CAPABLE_ASSISTANTS: tuple[str, ...] = (
    "claude",
    "codex",
    "gemini",
    "grok",
    "composer",
)

# Grok Build CLI model ids (verified via `grok models`).
GROK_MODEL_45 = "grok-4.5"
GROK_MODEL_COMPOSER = "grok-composer-2.5-fast"

# Aliases → canonical assistant id.
_ASSISTANT_ALIASES: dict[str, str] = {
    "grok-4.5": "grok",
    "grok-4_5": "grok",
    "grok45": "grok",
    "grok-build": "grok",
    "grok_build": "grok",
    "xai": "grok",
    "composer-2.5": "composer",
    "composer-2.5-fast": "composer",
    "composer_2_5": "composer",
    "composer2.5": "composer",
    "grok-composer": "composer",
    "grok-composer-2.5": "composer",
    "grok-composer-2.5-fast": "composer",
}

# Grok Build and Composer share one CLI binary (`grok` / `agent`).
_GROK_FAMILY: frozenset[str] = frozenset({"grok", "composer"})


def normalize_assistant(value: str | None, *, default: str = "claude") -> str:
    """Normalize an assistant id, falling back to a supported default."""
    raw = (value or "").strip().lower()
    if raw in _ASSISTANT_ALIASES:
        raw = _ASSISTANT_ALIASES[raw]
    if raw in SUPPORTED_ASSISTANTS:
        return raw
    fallback = (default or "claude").strip().lower()
    if fallback in _ASSISTANT_ALIASES:
        fallback = _ASSISTANT_ALIASES[fallback]
    if fallback in SUPPORTED_ASSISTANTS:
        return fallback
    return "claude"


def is_supported_assistant(value: str | None) -> bool:
    """Return True when *value* is a known assistant id (or alias)."""
    raw = (value or "").strip().lower()
    if not raw:
        return False
    if raw in _ASSISTANT_ALIASES:
        raw = _ASSISTANT_ALIASES[raw]
    return raw in SUPPORTED_ASSISTANTS


def default_assistant() -> str:
    """Return the configured default assistant for spawned sessions."""
    return normalize_assistant(os.environ.get("VOXHERD_DEFAULT_ASSISTANT"), default="claude")


def supports_hooks(assistant: str | None) -> bool:
    """Return True if the assistant has a native hook lifecycle."""
    return normalize_assistant(assistant) in HOOK_CAPABLE_ASSISTANTS


# Default tmux pane geometry for spawned assistant sessions (cols x rows).
# Wider than the 80x24 default so console output wraps less aggressively.
TMUX_SPAWN_WIDTH = 200
TMUX_SPAWN_HEIGHT = 50

# Canonical Grok reasoning-effort levels (from `grok --help`).
_GROK_EFFORT_LEVELS: frozenset[str] = frozenset({
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "deep",
})

# Canonical Codex reasoning-effort levels (codex `-c model_reasoning_effort=…`).
# GPT-5.6 (sol/terra/luna) is the first family to expose "max"; older models top out at xhigh.
_CODEX_EFFORT_LEVELS: frozenset[str] = frozenset({
    "minimal", "low", "medium", "high", "xhigh", "max",
})

# Conservative Codex model-id shape (e.g. "gpt-5.6-luna", "gpt-5.5", "o3"). Passed as its own
# argv element after ``--`` so there is no shell, but validate anyway so a stray value can't
# smuggle extra flags into the codex invocation.
_CODEX_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def normalize_codex_effort(value: str | None) -> str | None:
    """Return a safe effort for codex ``-c model_reasoning_effort=…``, or None."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    aliases = {"x-high": "xhigh", "extra-high": "xhigh", "maximum": "max"}
    raw = aliases.get(raw, raw)
    return raw if raw in _CODEX_EFFORT_LEVELS else None


def resolve_codex_model(model: str | None) -> str | None:
    """Return a validated codex ``-m`` model id, or None to use codex's own default."""
    raw = (model or "").strip()
    return raw if _CODEX_MODEL_RE.match(raw) else None


def model_for_assistant(assistant: str | None) -> str | None:
    """Return the CLI model id for Grok-family assistants, else None."""
    provider = normalize_assistant(assistant)
    if provider == "grok":
        return GROK_MODEL_45
    if provider == "composer":
        return GROK_MODEL_COMPOSER
    return None


def resolve_grok_model(
    assistant: str | None,
    model: str | None = None,
) -> str:
    """Pick the Grok CLI ``-m`` model id.

    Prefer an explicit cockpit/client *model* when it looks like a Grok family
    model id; otherwise fall back to the assistant's default model.
    """
    raw = (model or "").strip()
    if raw:
        # Accept full model ids and light aliases from the cockpit.
        lowered = raw.lower()
        if lowered in _ASSISTANT_ALIASES:
            return model_for_assistant(_ASSISTANT_ALIASES[lowered]) or GROK_MODEL_45
        if lowered in (GROK_MODEL_45, GROK_MODEL_COMPOSER):
            return lowered
        # Pass through other grok-* model ids verbatim (custom / future models).
        if lowered.startswith("grok-") or lowered.startswith("composer"):
            return raw
    return model_for_assistant(assistant) or GROK_MODEL_45


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Return a safe effort string for ``--reasoning-effort``, or None."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    # Accept common cockpit aliases.
    aliases = {"x-high": "xhigh", "extra-high": "xhigh", "maximum": "max"}
    raw = aliases.get(raw, raw)
    if raw in _GROK_EFFORT_LEVELS:
        return raw
    return None


def assistants_compatible(a: str | None, b: str | None) -> bool:
    """True when two assistant ids refer to the same CLI family.

    Grok 4.5 and Composer both run as the ``grok`` binary, so process
    inference must not thrash ``session.assistant`` between them.
    """
    left = normalize_assistant(a)
    right = normalize_assistant(b)
    if left == right:
        return True
    return left in _GROK_FAMILY and right in _GROK_FAMILY


def spawn_command_for_assistant(
    assistant: str | None,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[str]:
    """Return the interactive command used to start an assistant session."""
    provider = normalize_assistant(assistant)
    if provider == "claude":
        return ["claude", "--dangerously-skip-permissions", "--chrome"]
    if provider == "codex":
        # Honor the cockpit's per-role model + effort (e.g. GPT-5.6 Luna @ max). Codex can't be
        # reconfigured in-session, so it is set at launch: `codex -m <id> -c model_reasoning_effort=<lvl>`.
        # Both are optional — a bare `codex` falls back to ~/.codex/config.toml, as before.
        cmd = ["codex"]
        resolved = resolve_codex_model(model)
        if resolved:
            cmd.extend(["-m", resolved])
        effort = normalize_codex_effort(reasoning_effort)
        if effort:
            cmd.extend(["-c", f"model_reasoning_effort={effort}"])
        return cmd
    if provider == "gemini":
        return ["gemini"]
    if provider in _GROK_FAMILY:
        # --minimal: finalized blocks go into native terminal scrollback so
        # tmux history_size grows (Claude-like console scrolling). Without it
        # Grok stays in alt-screen / in-place repaint with history_size=0.
        resolved = resolve_grok_model(provider, model)
        cmd = ["grok", "-m", resolved, "--always-approve", "--minimal"]
        effort = normalize_reasoning_effort(reasoning_effort)
        if effort:
            cmd.extend(["--reasoning-effort", effort])
        return cmd
    return ["claude", "--dangerously-skip-permissions", "--chrome"]


def pane_env_pairs_for_assistant(assistant: str | None) -> list[str]:
    """Return ``KEY=value`` pairs that must be visible *inside* a tmux pane.

    Tmux does **not** forward the ``tmux`` client process environment into the
    new pane. Callers must pass these via ``tmux new-session -e KEY=value``
    and/or prefix the pane command with ``env KEY=value …``.
    """
    provider = normalize_assistant(assistant)
    pairs: list[str] = []
    if provider == "claude":
        pairs.append("CLAUDE_CODE_TASK_LIST_ID=voxherd")
    if provider in _GROK_FAMILY:
        # Soft-default install hooks use VOXHERD_HOOK_ASSISTANT=grok; Composer
        # sessions must override so SessionStart/Stop register correctly.
        pairs.append(f"VOXHERD_HOOK_ASSISTANT={provider}")
    return pairs


def apply_assistant_env(env: dict[str, str], assistant: str | None) -> dict[str, str]:
    """Return a copy of *env* with assistant-specific variables applied.

    Used for headless resume / non-tmux subprocesses where the process *does*
    inherit the ``env`` dict passed to ``create_subprocess_exec``.
    """
    out = dict(env)
    for pair in pane_env_pairs_for_assistant(assistant):
        key, _, value = pair.partition("=")
        if key:
            out[key] = value
    return out


def build_tmux_spawn_argv(
    tmux_session: str,
    project_dir: str,
    assistant: str | None,
    *,
    command: list[str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    width: int = TMUX_SPAWN_WIDTH,
    height: int = TMUX_SPAWN_HEIGHT,
) -> list[str]:
    """Return the full ``tmux new-session`` argv used by session spawn.

    Pane identity env is injected twice for reliability:
    1. ``tmux … -e KEY=value`` (session/pane environment)
    2. ``env KEY=value <command>`` prefix (process environment even if -e is ignored)

    Geometry defaults to ``TMUX_SPAWN_WIDTH`` x ``TMUX_SPAWN_HEIGHT`` so spawned
    consoles are wider/taller than tmux's 80x24 default.
    """
    provider = normalize_assistant(assistant)
    spawn_cmd = (
        list(command)
        if command is not None
        else spawn_command_for_assistant(
            provider, model=model, reasoning_effort=reasoning_effort
        )
    )
    env_pairs = pane_env_pairs_for_assistant(provider)

    args: list[str] = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        tmux_session,
        "-c",
        project_dir,
        "-x",
        str(int(width)),
        "-y",
        str(int(height)),
    ]
    for pair in env_pairs:
        args.extend(["-e", pair])

    if env_pairs:
        # Prefix the pane command so the child process sees the vars even when
        # the tmux client env is not inherited (the common case).
        spawn_cmd = ["env", *env_pairs, *spawn_cmd]

    args.extend(["--", *spawn_cmd])
    return args


def resume_command_for_assistant(
    assistant: str | None,
    session_id: str,
    message: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
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
        # codex exec resume SESSION_ID PROMPT -- resumes a non-interactive session.
        # --json outputs newline-delimited JSON events (similar to Claude's stream-json).
        return ["codex", "exec", "resume", session_id, "--json", message]
    if provider in _GROK_FAMILY:
        # Headless resume: grok -p PROMPT --resume ID --output-format streaming-json -m MODEL
        # (no --minimal — headless doesn't use the TUI alt-screen path)
        resolved = resolve_grok_model(provider, model)
        cmd = [
            "grok",
            "-p",
            message,
            "--resume",
            session_id,
            "--output-format",
            "streaming-json",
            "-m",
            resolved,
            "--always-approve",
        ]
        effort = normalize_reasoning_effort(reasoning_effort)
        if effort:
            cmd.extend(["--reasoning-effort", effort])
        return cmd
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
    if provider in _GROK_FAMILY:
        # Binary is `grok`; install also links `agent` → same binary under ~/.grok/bin.
        return (
            cmd in ("grok", "agent")
            or cmd.startswith("grok-")
            or cmd.startswith("agent-")
            or cmd.startswith("grok_build")
        )
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
    if (
        cmd in ("grok", "agent")
        or cmd.startswith("grok-")
        or cmd.startswith("agent-")
        or cmd.startswith("grok_build")
    ):
        # Default family id — spawn/hook registration sets composer when selected.
        return "grok"
    if cmd == "claude" or re.match(r"^\d+\.\d+\.\d+$", cmd):
        return "claude"
    return None


def extract_hook_fields(payload: dict[str, Any] | None) -> dict[str, str]:
    """Normalize Claude snake_case and Grok camelCase hook stdin fields.

    Grok Build emits ``sessionId``, ``workspaceRoot``, ``transcriptPath``, etc.
    Claude Code emits ``session_id``, ``cwd``, ``transcript_path``. Accept both.
    Returns a dict with snake_case keys used by VoxHerd hooks/bridge.
    """
    data = payload if isinstance(payload, dict) else {}

    def _first(*keys: str) -> str:
        for key in keys:
            val = data.get(key)
            if val is None:
                continue
            text = str(val).strip()
            if text:
                return text
        return ""

    session_id = _first("session_id", "sessionId")
    cwd = _first("cwd", "workspaceRoot", "workspace_root")
    transcript_path = _first("transcript_path", "transcriptPath")
    stop_reason = _first("stop_reason", "stopReason") or "completed"
    agent_id = _first("agent_id", "agentId", "subagent_id", "subagentId")
    agent_type = _first(
        "agent_type",
        "agentType",
        "subagent_type",
        "subagentType",
    )

    return {
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "stop_reason": stop_reason,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }
