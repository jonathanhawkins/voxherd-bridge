"""Install-time model profile — which fast/cheap model the bridge itself uses.

VoxHerd calls a small model for exactly two things: the 1-2 sentence stop-hook
summary that gets spoken aloud, and the structured-JSON intent parse behind
``/api/intent/parse``. Which model that is, is an **install-time** decision, not
a per-request one — a summary's model must never depend on which client happens
to be connected when the turn ends.

Three named profiles: ``anthropic`` (Claude Haiku, the default), ``google``
(Gemini 3.8 Flash through ``agy``) and ``openai`` (GPT-6 Luna through
``codex exec``). ``hooks/install.sh`` writes ``~/.voxherd/models.env`` (``%APPDATA%\\VoxHerd`` on
Windows)::

    VOXHERD_SUMMARY_CLI=agy
    VOXHERD_SUMMARY_MODEL=gemini-3.8-flash-low
    VOXHERD_NLU_CLI=agy
    VOXHERD_NLU_MODEL=gemini-3.8-flash-low

Resolution order, highest first:

1. the process environment (a user can override for one session),
2. ``models.env``,
3. the hardcoded Anthropic defaults below.

Every layer is optional and every value is validated. A missing, unreadable or
corrupt ``models.env`` degrades to the Anthropic defaults — **hooks must never
block the assistant**, so nothing in this module raises.

This is the canonical Python resolver. ``bridge/routes.py`` imports it directly;
``hooks/on-stop.py`` imports the copy that ``install.sh`` drops next to it in
``~/.voxherd/hooks/``. The bash and PowerShell hooks carry line-for-line
equivalents (``hooks/model-profile.sh``, ``hooks/ModelProfile.ps1``) — keep all
three in step.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Defaults and validation
# --------------------------------------------------------------------------

#: CLIs we are willing to spawn. ``models.env`` is attacker-adjacent (it is
#: read by a shell hook), so the CLI name is matched against this allowlist
#: rather than interpolated into a command line. Anything else is ignored and
#: the default is used instead.
ALLOWED_CLIS: tuple[str, ...] = ("claude", "gemini", "codex", "agy")

#: Stock VoxHerd: Claude Haiku via the ``claude`` CLI.
DEFAULT_CLI = "claude"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

#: Named install profiles. ``install.sh`` materialises one of these into
#: ``models.env``; kept here so the installer and the resolver cannot drift.
PROFILES: dict[str, dict[str, str]] = {
    "anthropic": {
        "summary_cli": "claude",
        "summary_model": "claude-haiku-4-5-20251001",
        "nlu_cli": "claude",
        "nlu_model": "claude-haiku-4-5-20251001",
    },
    # The `google` profile. Antigravity's
    # `agy`, not Gemini CLI: Google retired Gemini CLI's sign-in for personal
    # accounts on 2026-06-18, so `gemini -p` now fails without an API key,
    # while `agy` runs on the Google sign-in. agy offers no Flash-Lite; its
    # cheapest model is Gemini 3.8 Flash at low effort. Measured 2026-09-27:
    # 3.8-5 s a call, ~1.2 s of it the model and the rest agy starting up.
    "google": {
        "summary_cli": "agy",
        "summary_model": "gemini-3.8-flash-low",
        "nlu_cli": "agy",
        "nlu_model": "gemini-3.8-flash-low",
    },
    # The `openai` profile, for Codex users. GPT-6 Luna is the model OpenAI's
    # Codex docs recommend for summarisation and extraction, and its cheapest
    # ($0.10 in / $0.50 out per million tokens, against Haiku 4.5's $1 / $5).
    # `codex exec` runs it on the user's ChatGPT sign-in or API key. Effort is
    # pinned low in the argv (_CODEX_EXEC_ARGS), not in the model id. Measured
    # 2026-10-27 on codex-cli 0.161.0: 4.4-5.8 s a call, ~12k tokens, most of
    # them Codex's own agent instructions rather than the transcript.
    "openai": {
        "summary_cli": "codex",
        "summary_model": "gpt-6-luna",
        "nlu_cli": "codex",
        "nlu_model": "gpt-6-luna",
    },
}

DEFAULT_PROFILE = "anthropic"

#: Model ids are vendor strings: letters, digits and ``. _ - :`` only. This is
#: deliberately narrow — it keeps shell metacharacters, whitespace and newlines
#: out of an argv that a hook builds.
_MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_MODEL_MAX_LEN = 128

#: The four keys ``models.env`` may set, mapped to ModelProfile field names.
ENV_KEYS: dict[str, str] = {
    "VOXHERD_SUMMARY_CLI": "summary_cli",
    "VOXHERD_SUMMARY_MODEL": "summary_model",
    "VOXHERD_NLU_CLI": "nlu_cli",
    "VOXHERD_NLU_MODEL": "nlu_model",
}

#: Per-CLI flag spelling. ``claude`` and ``agy`` want ``--model``; ``gemini`` and
#: ``codex`` want ``-m``. Getting this wrong is a silent fallback to the CLI's
#: default model, which is slower and pricier than the one we asked for.
_MODEL_FLAG: dict[str, str] = {
    "claude": "--model",
    "gemini": "-m",
    "codex": "-m",
    "agy": "--model",
}

#: Per-CLI flag for "give me a JSON envelope instead of prose". ``codex exec``
#: only offers a JSONL event stream, so it has no entry; callers fall back to
#: scraping the object out of raw stdout.
_JSON_FLAG: dict[str, tuple[str, ...]] = {
    "claude": ("--output-format", "json"),
    "gemini": ("-o", "json"),
    "agy": ("--output-format", "json"),
}

#: ``codex exec`` is a full agent run, so a one-shot call has to turn most of
#: that off. Every flag here is load-bearing:
#:
#: * ``--ephemeral`` -- no session file, or every summary and every intent
#:   parse would show up in the user's ``codex resume`` list.
#: * ``--ignore-user-config`` -- skip ``~/.codex/config.toml``: its MCP servers
#:   (slow to start) and the ``notify`` hook VoxHerd itself installs there,
#:   which would report the summary call as a session that just finished.
#:   Sign-in still comes from ``CODEX_HOME``. The cost: a custom provider set
#:   in that file is not used for summaries.
#: * ``--skip-git-repo-check`` -- hooks and the bridge run from directories
#:   that are not repositories, where ``codex exec`` otherwise refuses.
#: * ``-s read-only`` -- the model can read but never write or run a change.
#: * ``--color never`` -- plain stdout for the hook to speak.
#: * ``model_reasoning_effort="low"`` -- a 1-2 sentence summary needs no more,
#:   and low used a third fewer tokens than the default medium.
#:
#: They go ahead of ``-m <model>``, and a ``--`` goes ahead of the prompt:
#: without it clap reads a prompt that begins with ``-`` as an option and
#: exits with "unexpected argument" (measured, codex-cli 0.161.0).
_CODEX_EXEC_ARGS: tuple[str, ...] = (
    "codex", "exec",
    "--ephemeral",
    "--ignore-user-config",
    "--skip-git-repo-check",
    "-s", "read-only",
    "--color", "never",
    "-c", 'model_reasoning_effort="low"',
)

#: ``--ignore-user-config`` also drops where the sign-in is kept: a user with
#: ``cli_auth_credentials_store = "keyring"`` looks signed out without it. So
#: that one top-level key is read back out of ``$CODEX_HOME/config.toml`` and
#: forwarded, and nothing else in the file is. The value must be one of these.
_CODEX_AUTH_STORES: tuple[str, ...] = ("file", "keyring", "auto")
_CODEX_AUTH_KEY = "cli_auth_credentials_store"
_CODEX_AUTH_RE = re.compile(
    r"""\A\s*cli_auth_credentials_store\s*=\s*(?:"([a-z]+)"|'([a-z]+)')\s*(?:\#.*)?\Z"""
)

#: ``agy`` is an agent first: a prompt that happens to start with "/" would run
#: as one of its slash commands or skills. Nothing we send should.
_EXTRA_ARGS: dict[str, tuple[str, ...]] = {
    "agy": ("--disable-slash-commands",),
}

#: How long an intent parse may take. Every CLI spends most of it starting up,
#: not answering. Measured on the dev machine: ``claude -p`` took 6.3-11.4 s end
#: to end around a ~1.5 s model call (its settings and plugins load first), and
#: ``agy`` 4-9 s (~3.7 s of start-up). The old 5 s budget failed nearly every
#: parse, so the iPhone's fallback for an unmatched command never worked. The
#: iOS app waits a little longer than this (``HaikuIntentParser``), so it hears
#: the bridge's answer -- or its timeout -- rather than giving up first.
NLU_TIMEOUT_SECONDS = 15.0


def _config_dir() -> Path:
    """Where VoxHerd keeps its per-user state."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "VoxHerd"
    return Path.home() / ".voxherd"


def models_env_path(env: dict[str, str] | None = None) -> Path:
    """Path to ``models.env``.

    ``VOXHERD_MODELS_ENV`` overrides it (used by tests and by anyone running
    several profiles side by side).
    """
    env = os.environ if env is None else env
    override = (env.get("VOXHERD_MODELS_ENV") or "").strip()
    if override:
        return Path(override)
    return _config_dir() / "models.env"


def valid_cli(value: object) -> bool:
    """True when ``value`` names a CLI we are willing to spawn."""
    return isinstance(value, str) and value.strip().lower() in ALLOWED_CLIS


def valid_model(value: object) -> bool:
    """True when ``value`` is a plausible model id (no shell metacharacters)."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    return bool(value) and len(value) <= _MODEL_MAX_LEN and bool(_MODEL_RE.match(value))


# --------------------------------------------------------------------------
# models.env parsing
# --------------------------------------------------------------------------

def parse_models_env(text: str) -> dict[str, str]:
    """Parse ``models.env`` text into a dict of recognised, *valid* keys.

    Deliberately not a shell: no expansion, no substitution, no execution. Junk
    lines are skipped rather than raising, and a key whose value fails
    validation is dropped so the caller falls through to the default.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key not in ENV_KEYS:
            continue
        value = value.strip()
        # Strip one layer of matching quotes, the way an .env file is written.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].strip()
        if not value:
            continue
        if key.endswith("_CLI"):
            if valid_cli(value):
                out[key] = value.lower()
        elif valid_model(value):
            out[key] = value
    return out


def read_models_env(path: Path | str | None = None) -> dict[str, str]:
    """Read and parse ``models.env``. Returns ``{}` for any failure at all.

    Missing file, unreadable file, a directory where a file should be, binary
    garbage, permissions — all of it degrades to "no overrides", which means
    the Anthropic defaults.
    """
    try:
        target = Path(path) if path is not None else models_env_path()
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    try:
        return parse_models_env(text)
    except Exception:  # pragma: no cover - parse_models_env does not raise
        return {}


# --------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelProfile:
    """The resolved answer to "which model, via which CLI"."""

    summary_cli: str = DEFAULT_CLI
    summary_model: str = DEFAULT_MODEL
    nlu_cli: str = DEFAULT_CLI
    nlu_model: str = DEFAULT_MODEL
    #: field name -> "default" | "file" | "env", for logging and for hooks that
    #: want to know whether anyone actually chose this.
    sources: dict[str, str] = field(
        default_factory=lambda: {name: "default" for name in ENV_KEYS.values()}
    )

    @property
    def summary_cli_configured(self) -> bool:
        """True when the summary CLI was chosen, not merely defaulted to.

        ``hooks/on-stop.py`` uses this: with the stock profile it keeps today's
        rule of only shelling out to the session's own assistant, but an
        explicitly installed profile overrides that.
        """
        return self.sources.get("summary_cli", "default") != "default"

    @property
    def nlu_cli_configured(self) -> bool:
        return self.sources.get("nlu_cli", "default") != "default"

    def summary_argv(self, prompt: str) -> list[str]:
        """argv for a one-shot summary request (plain text out)."""
        return cli_argv(self.summary_cli, self.summary_model, prompt, json_output=False)

    def nlu_argv(self, prompt: str) -> list[str]:
        """argv for a one-shot intent parse (JSON envelope out where supported)."""
        return cli_argv(self.nlu_cli, self.nlu_model, prompt, json_output=True)


def resolve(
    env: dict[str, str] | None = None,
    path: Path | str | None = None,
) -> ModelProfile:
    """Resolve the active profile: process env > ``models.env`` > defaults.

    Never raises. Any value that fails validation is ignored in favour of the
    next layer down, so the worst case is today's Claude Haiku behaviour.
    """
    env = os.environ if env is None else env
    try:
        file_values = read_models_env(
            path if path is not None else models_env_path(env)
        )
    except Exception:  # pragma: no cover - read_models_env swallows everything
        file_values = {}

    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for env_key, attr in ENV_KEYS.items():
        is_cli = env_key.endswith("_CLI")
        check = valid_cli if is_cli else valid_model

        raw_env = env.get(env_key)
        if raw_env is not None and check(raw_env):
            values[attr] = raw_env.strip().lower() if is_cli else raw_env.strip()
            sources[attr] = "env"
            continue

        raw_file = file_values.get(env_key)
        if raw_file is not None and check(raw_file):
            values[attr] = raw_file
            sources[attr] = "file"
            continue

        values[attr] = DEFAULT_CLI if is_cli else DEFAULT_MODEL
        sources[attr] = "default"

    return ModelProfile(sources=sources, **values)


_ACTIVE: ModelProfile | None = None


def active() -> ModelProfile:
    """The profile for this process, resolved once and cached.

    The profile is an *install-time* decision — a summary's model must not
    depend on which client happens to be connected — so a long-running bridge
    reads ``models.env`` once at first use, not per request.
    """
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = resolve()
    return _ACTIVE


def reset_active() -> None:
    """Drop the cached profile. For tests, and for anything that rewrites
    ``models.env`` inside a live process."""
    global _ACTIVE
    _ACTIVE = None


def profile_env(name: str) -> dict[str, str]:
    """The ``models.env`` body for a named install profile.

    Unknown names fall back to ``anthropic`` — an installer typo must not brick
    the hooks.
    """
    spec = PROFILES.get((name or "").strip().lower(), PROFILES[DEFAULT_PROFILE])
    return {env_key: spec[attr] for env_key, attr in ENV_KEYS.items()}


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------

def codex_auth_store(env: dict[str, str] | None = None) -> str | None:
    """The user's ``cli_auth_credentials_store``, if they set one, else None.

    Read line by line, top-level keys only (a line before the first ``[table]``
    header), and only a quoted value from :data:`_CODEX_AUTH_STORES`. This is
    not a TOML parser and does not need to be: anything it cannot be sure of is
    left out, which is exactly today's behaviour. Never raises.
    """
    env = os.environ if env is None else env
    try:
        codex_home = (env.get("CODEX_HOME") or "").strip()
        base = Path(codex_home) if codex_home else Path.home() / ".codex"
        text = (base / "config.toml").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            return None  # past the top level: a key under [profiles.x] is not the user's own
        match = _CODEX_AUTH_RE.match(line)
        if match:
            store = match.group(1) or match.group(2)
            return store if store in _CODEX_AUTH_STORES else None
    return None


def model_flag(cli: str) -> str:
    """The flag this CLI spells ``--model`` with."""
    return _MODEL_FLAG.get(cli, "--model")


def cli_argv(cli: str, model: str, prompt: str, json_output: bool = False) -> list[str]:
    """Build the argv for a one-shot, non-interactive prompt.

    Falls back to the Anthropic default for an unrecognised CLI or model rather
    than building a command out of untrusted text.
    """
    if not valid_cli(cli):
        cli = DEFAULT_CLI
    cli = cli.strip().lower()
    if not valid_model(model):
        model = DEFAULT_MODEL
    model = model.strip()

    if cli == "codex":
        # `codex exec` takes the prompt positionally, after the flags and a `--`.
        # It has no envelope mode, so json_output changes nothing here.
        argv = [*_CODEX_EXEC_ARGS]
        store = codex_auth_store()
        if store:
            argv.extend(("-c", f'{_CODEX_AUTH_KEY}="{store}"'))
        return [*argv, model_flag(cli), model, "--", prompt]

    argv = [cli, "-p", prompt, model_flag(cli), model, *_EXTRA_ARGS.get(cli, ())]
    if json_output:
        argv.extend(_JSON_FLAG.get(cli, ()))
    return argv


def unwrap_json_reply(cli: str, envelope: object) -> tuple[object | None, bool]:
    """Pull the model's reply out of a CLI's JSON envelope.

    ``claude --output-format json`` -> ``{"type":"result","is_error":bool,"result":"<text>"}``
    ``gemini -o json``              -> ``{"session_id":..,"response":"<text>","stats":{..}}``
                                       with ``{"error":{"message":..}}`` on failure
    ``agy --output-format json``    -> ``{"conversation_id":..,"status":"SUCCESS",``
                                       ``"response":"<text>","usage":{..}}`` (measured, 1.2.12)
    ``codex exec``                  -> no single envelope

    Returns ``(reply, is_error)``. ``reply`` is ``None`` when the shape is not
    recognised, which tells the caller to fall back to scraping raw stdout.
    """
    if not isinstance(envelope, dict):
        return None, False

    if cli == "gemini":
        if envelope.get("error"):
            return None, True
        if "response" in envelope:
            return envelope.get("response"), False
        return None, False

    if cli == "agy":
        # Anything but SUCCESS -- or no status at all -- is not an answer.
        if envelope.get("status") != "SUCCESS":
            return None, True
        return envelope.get("response"), False

    # claude, and anything else that happens to use the result envelope.
    if "result" in envelope and "action" not in envelope:
        if envelope.get("is_error"):
            return None, True
        return envelope.get("result"), False

    # A bare object (no envelope) — hand it straight back.
    return envelope, False


__all__ = [
    "ALLOWED_CLIS",
    "NLU_TIMEOUT_SECONDS",
    "DEFAULT_CLI",
    "DEFAULT_MODEL",
    "DEFAULT_PROFILE",
    "ENV_KEYS",
    "PROFILES",
    "ModelProfile",
    "active",
    "cli_argv",
    "codex_auth_store",
    "model_flag",
    "models_env_path",
    "parse_models_env",
    "profile_env",
    "read_models_env",
    "reset_active",
    "resolve",
    "unwrap_json_reply",
    "valid_cli",
    "valid_model",
]
