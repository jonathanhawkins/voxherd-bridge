"""Unit tests for bridge.assistant helpers (Grok 4.5 + Composer support)."""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid

import pytest

from bridge.assistant import (
    GROK_MODEL_45,
    GROK_MODEL_COMPOSER,
    SUPPORTED_ASSISTANTS,
    TMUX_SPAWN_HEIGHT,
    TMUX_SPAWN_WIDTH,
    apply_assistant_env,
    assistants_compatible,
    build_tmux_spawn_argv,
    extract_hook_fields,
    infer_assistant_from_process,
    is_supported_assistant,
    looks_like_assistant_process,
    model_for_assistant,
    normalize_assistant,
    pane_env_pairs_for_assistant,
    resume_command_for_assistant,
    spawn_command_for_assistant,
    supports_hooks,
)
class TestSupportedIds:
    def test_grok_and_composer_are_supported(self):
        assert "grok" in SUPPORTED_ASSISTANTS
        assert "composer" in SUPPORTED_ASSISTANTS
        assert is_supported_assistant("grok")
        assert is_supported_assistant("composer")
        assert is_supported_assistant("grok-4.5")
        assert is_supported_assistant("grok-composer-2.5-fast")

    def test_normalize_aliases(self):
        assert normalize_assistant("grok-4.5") == "grok"
        assert normalize_assistant("Grok") == "grok"
        assert normalize_assistant("composer-2.5-fast") == "composer"
        assert normalize_assistant("grok-composer-2.5-fast") == "composer"
        assert normalize_assistant("claude") == "claude"

    def test_model_ids(self):
        assert model_for_assistant("grok") == GROK_MODEL_45
        assert model_for_assistant("grok-4.5") == GROK_MODEL_45
        assert model_for_assistant("composer") == GROK_MODEL_COMPOSER
        assert model_for_assistant("claude") is None

    def test_hooks_capable(self):
        assert supports_hooks("grok")
        assert supports_hooks("composer")
        assert supports_hooks("claude")


class TestSpawnAndResume:
    def test_spawn_grok_uses_cli_and_model(self):
        cmd = spawn_command_for_assistant("grok")
        assert cmd[0] == "grok"
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == GROK_MODEL_45
        assert "--always-approve" in cmd
        # --minimal enables native scrollback (tmux history_size grows)
        assert "--minimal" in cmd

    def test_spawn_composer_uses_composer_model(self):
        cmd = spawn_command_for_assistant("composer")
        assert cmd[0] == "grok"
        assert cmd[cmd.index("-m") + 1] == GROK_MODEL_COMPOSER
        assert "--minimal" in cmd

    def test_spawn_alias_grok_45(self):
        cmd = spawn_command_for_assistant("grok-4.5")
        assert cmd[0] == "grok"
        assert cmd[cmd.index("-m") + 1] == GROK_MODEL_45

    def test_spawn_honors_explicit_model_override(self):
        cmd = spawn_command_for_assistant("grok", model=GROK_MODEL_COMPOSER)
        assert cmd[cmd.index("-m") + 1] == GROK_MODEL_COMPOSER

    def test_spawn_honors_reasoning_effort(self):
        cmd = spawn_command_for_assistant("grok", reasoning_effort="high")
        assert "--reasoning-effort" in cmd
        assert cmd[cmd.index("--reasoning-effort") + 1] == "high"

    def test_spawn_ignores_invalid_effort(self):
        cmd = spawn_command_for_assistant("grok", reasoning_effort="not-a-level")
        assert "--reasoning-effort" not in cmd

    def test_resume_grok_has_resume_prompt_and_streaming_json(self):
        cmd = resume_command_for_assistant("grok", "sess-abc", "run the tests")
        assert cmd is not None
        assert cmd[0] == "grok"
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "sess-abc"
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "run the tests"
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "streaming-json"
        assert cmd[cmd.index("-m") + 1] == GROK_MODEL_45
        # Headless resume must not use --minimal
        assert "--minimal" not in cmd

    def test_resume_composer_model(self):
        cmd = resume_command_for_assistant("composer", "sid-1", "continue")
        assert cmd is not None
        assert cmd[cmd.index("-m") + 1] == GROK_MODEL_COMPOSER
        assert "--resume" in cmd
        assert "-p" in cmd
        assert "streaming-json" in cmd

    def test_existing_claude_spawn_unchanged(self):
        cmd = spawn_command_for_assistant("claude")
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "--minimal" not in cmd


class TestProcessInference:
    def test_grok_binary(self):
        assert infer_assistant_from_process("grok") == "grok"
        assert infer_assistant_from_process("agent") == "grok"

    def test_looks_like_for_both_family_ids(self):
        assert looks_like_assistant_process("grok", "grok")
        assert looks_like_assistant_process("composer", "grok")
        assert looks_like_assistant_process("composer", "agent")
        assert looks_like_assistant_process("grok", "agent")
        assert not looks_like_assistant_process("grok", "claude")

    def test_family_compatible(self):
        assert assistants_compatible("grok", "composer")
        assert assistants_compatible("composer", "grok-4.5")
        assert not assistants_compatible("grok", "claude")
        assert assistants_compatible("claude", "claude")


class TestHookFieldNormalization:
    def test_claude_snake_case(self):
        fields = extract_hook_fields(
            {
                "session_id": "s1",
                "cwd": "/tmp/p",
                "transcript_path": "/tmp/t.jsonl",
                "stop_reason": "end_turn",
            }
        )
        assert fields["session_id"] == "s1"
        assert fields["cwd"] == "/tmp/p"
        assert fields["transcript_path"] == "/tmp/t.jsonl"
        assert fields["stop_reason"] == "end_turn"

    def test_grok_camel_case(self):
        fields = extract_hook_fields(
            {
                "sessionId": "abc-123",
                "workspaceRoot": "/Users/me/proj",
                "cwd": "",  # empty should fall through
                "transcriptPath": "/Users/me/.grok/sessions/x.jsonl",
                "stopReason": "completed",
                "agentId": "sub-1",
                "agentType": "explore",
            }
        )
        assert fields["session_id"] == "abc-123"
        assert fields["cwd"] == "/Users/me/proj"
        assert fields["transcript_path"] == "/Users/me/.grok/sessions/x.jsonl"
        assert fields["stop_reason"] == "completed"
        assert fields["agent_id"] == "sub-1"
        assert fields["agent_type"] == "explore"

    def test_empty_payload_defaults(self):
        fields = extract_hook_fields({})
        assert fields["session_id"] == ""
        assert fields["stop_reason"] == "completed"
        assert fields["cwd"] == ""

    def test_none_payload(self):
        fields = extract_hook_fields(None)
        assert fields["session_id"] == ""


class TestTmuxSpawnEnvIdentity:
    """Composer identity must reach the *pane*, not just the tmux client env."""

    def test_pane_env_pairs_composer(self):
        pairs = pane_env_pairs_for_assistant("composer")
        assert "VOXHERD_HOOK_ASSISTANT=composer" in pairs

    def test_pane_env_pairs_grok(self):
        pairs = pane_env_pairs_for_assistant("grok")
        assert "VOXHERD_HOOK_ASSISTANT=grok" in pairs

    def test_apply_assistant_env_for_resume(self):
        base = {"PATH": "/usr/bin", "HOME": "/tmp"}
        env = apply_assistant_env(base, "composer")
        assert env["VOXHERD_HOOK_ASSISTANT"] == "composer"
        assert env["PATH"] == "/usr/bin"
        # base must not be mutated
        assert "VOXHERD_HOOK_ASSISTANT" not in base

    def test_build_tmux_spawn_argv_injects_composer_env(self):
        """Shipped spawn argv must carry VOXHERD_HOOK_ASSISTANT into the pane.

        Regression: setting env= on the *tmux client* process does not reach the
        pane — Composer SessionStart hooks then soft-default to assistant=grok.
        """
        argv = build_tmux_spawn_argv("vh-test-sess", "/tmp/proj", "composer")
        assert argv[0] == "tmux"
        assert "new-session" in argv
        # Geometry wider than 80x24 default
        assert "-x" in argv
        assert argv[argv.index("-x") + 1] == str(TMUX_SPAWN_WIDTH)
        assert "-y" in argv
        assert argv[argv.index("-y") + 1] == str(TMUX_SPAWN_HEIGHT)
        # tmux -e form
        assert "-e" in argv
        e_values = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        assert "VOXHERD_HOOK_ASSISTANT=composer" in e_values
        # env-prefix form after --
        dash = argv.index("--")
        pane_cmd = argv[dash + 1 :]
        assert pane_cmd[0] == "env"
        assert "VOXHERD_HOOK_ASSISTANT=composer" in pane_cmd
        assert "grok" in pane_cmd
        assert GROK_MODEL_COMPOSER in pane_cmd
        assert "--minimal" in pane_cmd

    def test_build_tmux_spawn_argv_grok_model(self):
        argv = build_tmux_spawn_argv("vh-g", "/tmp/p", "grok")
        assert "VOXHERD_HOOK_ASSISTANT=grok" in argv
        assert GROK_MODEL_45 in argv
        assert "--minimal" in argv

    def test_build_tmux_spawn_argv_passes_model_and_effort(self):
        argv = build_tmux_spawn_argv(
            "vh-e",
            "/tmp/p",
            "grok",
            model=GROK_MODEL_COMPOSER,
            reasoning_effort="high",
        )
        assert GROK_MODEL_COMPOSER in argv
        assert "--reasoning-effort" in argv
        assert "high" in argv

    def test_ws_handler_spawn_uses_build_tmux_spawn_argv(self):
        """Guard: _handle_spawn_session must call the shipped argv builder."""
        # Source-level structural check — reimporting the function object used
        # by the module (not a reimplementation).
        import bridge.ws_handler as wh
        import bridge.assistant as asst

        assert wh.build_tmux_spawn_argv is asst.build_tmux_spawn_argv
        assert "build_tmux_spawn_argv" in wh._handle_spawn_session.__code__.co_names

    @pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
    def test_real_tmux_pane_sees_composer_hook_env(self, tmp_path):
        """Drive the real shipped argv against tmux; assert pane env, not client env."""
        session = f"vh-env-{uuid.uuid4().hex[:8]}"
        out_file = tmp_path / "hook_assistant.txt"
        # Write env from *inside* the pane command (proves env prefix + -e).
        # Keep the session alive briefly so show-environment can also be queried.
        shell_cmd = (
            f'printenv VOXHERD_HOOK_ASSISTANT > "{out_file}"; sleep 2'
        )
        argv = build_tmux_spawn_argv(
            session,
            str(tmp_path),
            "composer",
            command=["sh", "-c", shell_cmd],
        )
        # Fail if the shipped builder forgot the env injection entirely.
        assert "VOXHERD_HOOK_ASSISTANT=composer" in argv
        assert "-e" in argv

        try:
            # Intentionally do NOT pass VOXHERD_HOOK_ASSISTANT in the client env —
            # the pane must get it from -e / env prefix alone.
            client_env = {
                k: v
                for k, v in __import__("os").environ.items()
                if k != "VOXHERD_HOOK_ASSISTANT"
            }
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=client_env,
                timeout=5,
            )
            assert proc.returncode == 0, proc.stderr

            # 1) Session environment set via tmux -e
            show = subprocess.run(
                ["tmux", "show-environment", "-t", session, "VOXHERD_HOOK_ASSISTANT"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            assert show.returncode == 0, show.stderr
            assert "VOXHERD_HOOK_ASSISTANT=composer" in (show.stdout or "")

            # 2) Process environment seen by the pane command (env prefix / -e)
            value = ""
            for _ in range(30):
                if out_file.is_file():
                    value = out_file.read_text().strip()
                    if value:
                        break
                time.sleep(0.1)
            assert value == "composer", (
                f"pane process did not see VOXHERD_HOOK_ASSISTANT=composer; "
                f"got {value!r}; show-env={show.stdout!r}"
            )
        finally:
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True,
                timeout=3,
            )