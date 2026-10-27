"""Tests for Antigravity CLI (`agy`) support and Gemini's resume semantics.

Antigravity is Google's terminal coding agent. Google retired Jules' free
tiers on 2026-06-18 and redirected those users here, so it is the Google
agent that matters -- Gemini CLI alone is not enough.

Everything asserted here was verified against the binaries actually installed
on the dev machine (agy 1.2.10, gemini 0.61.0), not inferred from docs.
"""

from __future__ import annotations

import pytest

from bridge.assistant import (
    HOOK_CAPABLE_ASSISTANTS,
    SUPPORTED_ASSISTANTS,
    infer_assistant_from_process,
    is_supported_assistant,
    looks_like_assistant_process,
    normalize_antigravity_effort,
    normalize_assistant,
    resume_command_for_assistant,
    spawn_command_for_assistant,
    supports_hooks,
)

# --- registration ----------------------------------------------------------


def test_antigravity_is_supported_and_hook_capable():
    assert "antigravity" in SUPPORTED_ASSISTANTS
    assert "antigravity" in HOOK_CAPABLE_ASSISTANTS
    assert is_supported_assistant("antigravity") is True
    assert supports_hooks("antigravity") is True


@pytest.mark.parametrize(
    "alias", ["agy", "antigravity", "antigravity-cli", "antigravity_cli", "anti-gravity", "AGY"]
)
def test_aliases_normalize(alias):
    assert normalize_assistant(alias) == "antigravity"


def test_unknown_still_falls_back_to_claude():
    assert normalize_assistant("nonsense") == "claude"


# --- spawn -----------------------------------------------------------------


def test_spawn_is_auto_approving():
    """VoxHerd is a voice remote -- nobody is at a keyboard to approve a tool call."""
    cmd = spawn_command_for_assistant("antigravity")
    assert cmd[0] == "agy"
    assert "--dangerously-skip-permissions" in cmd


def test_spawn_carries_model_and_effort():
    cmd = spawn_command_for_assistant("antigravity", model="gemini-3.8-flash", reasoning_effort="high")
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_spawn_omits_absent_options():
    cmd = spawn_command_for_assistant("antigravity")
    assert "--model" not in cmd
    assert "--effort" not in cmd


# --- effort normalisation --------------------------------------------------


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_effort_accepts_agy_levels(level):
    assert normalize_antigravity_effort(level) == level


def test_effort_clamps_tiers_agy_does_not_have():
    """`agy` has no xhigh/max tier; passing one would make it reject the launch."""
    for alias in ("xhigh", "x-high", "max", "maximum", "extra-high"):
        assert normalize_antigravity_effort(alias) == "high"


def test_effort_rejects_garbage():
    assert normalize_antigravity_effort("banana") is None
    assert normalize_antigravity_effort("") is None
    assert normalize_antigravity_effort(None) is None


# --- resume ----------------------------------------------------------------


def test_resume_targets_the_conversation_by_id():
    cmd = resume_command_for_assistant("antigravity", "conv-abc-123", "run the tests")
    assert cmd is not None
    assert cmd[0] == "agy"
    assert cmd[cmd.index("--conversation") + 1] == "conv-abc-123"
    assert cmd[cmd.index("-p") + 1] == "run the tests"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_gemini_has_no_headless_resume():
    """Verified against gemini 0.61.0: `-r/--resume` takes an INDEX or "latest".

    There is no way to target the specific session a hook registered, so the
    dispatcher must fall back to tmux send-keys on the live pane. If this ever
    starts returning a command, check that Gemini grew UUID resume first --
    silently resuming the wrong session would be worse than not resuming.
    """
    assert resume_command_for_assistant("gemini", "some-uuid", "hello") is None


# --- process identification ------------------------------------------------


def test_identifies_its_own_process():
    assert looks_like_assistant_process("antigravity", "agy") is True
    assert looks_like_assistant_process("antigravity", "agy-darwin-arm64") is True
    assert looks_like_assistant_process("antigravity", "claude") is False


def test_infers_antigravity_from_pane_command():
    assert infer_assistant_from_process("agy") == "antigravity"
    assert infer_assistant_from_process("antigravity") == "antigravity"


def test_agent_symlink_still_means_grok_not_antigravity():
    """Grok's install links `agent` -> grok. Ordering must not steal it."""
    assert infer_assistant_from_process("agent") == "grok"
    assert infer_assistant_from_process("grok") == "grok"


# --- gemini spawn ----------------------------------------------------------


def test_gemini_spawn_takes_short_model_flag():
    """Gemini uses -m, not --model. Getting this wrong fails the launch."""
    cmd = spawn_command_for_assistant("gemini", model="gemini-3.8-flash")
    assert cmd == ["gemini", "-m", "gemini-3.8-flash"]
    assert spawn_command_for_assistant("gemini") == ["gemini"]
