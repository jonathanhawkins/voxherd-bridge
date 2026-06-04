"""Tests for bridge/activity.py — activity detection and idle prompt logic.

Covers the fix for the bug where stale tool names in terminal scrollback
falsely re-activated idle sessions, causing voice commands to queue with
"will send when it's free" even though nothing was running.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from bridge.activity import _detect_activity_type, _has_idle_prompt, _is_status_bar_line
from bridge.assistant import infer_assistant_from_process
from bridge.session_manager import Session, SessionManager


# ---------------------------------------------------------------------------
# Terminal output fixtures
# ---------------------------------------------------------------------------

# Claude Code sitting at its idle prompt — nothing happening
IDLE_TERMINAL = """\
  Created file at /tmp/foo.py

  Wrote 42 lines to foo.py

❯
─────────────────────────────────────────
🤖 Opus 4.6  $0.34  7.2k tokens  12s
⏵⏵ bypass permissions
"""

# Claude Code actively running a tool — spinner visible
ACTIVE_WITH_SPINNER = """\
  ⠹ Running: pytest tests/ -v
"""

# Claude Code actively editing — tool pattern visible, no idle prompt
ACTIVE_EDITING = """\
  Edit bridge/server.py
  Applied 3 changes
  ⠼ Verifying...
"""

# Stale scrollback with tool names ABOVE the idle prompt
# This was the root cause of the false re-activation bug
STALE_SCROLLBACK_WITH_IDLE_PROMPT = """\
  Read bridge/activity.py (1-50)
  Edit bridge/ws_handler.py
  Applied 2 changes
  Bash: pytest tests/ -v
  All 70 tests passed.

❯
─────────────────────────────────────────
🤖 Opus 4.6  $1.23  45k tokens  2m
⏵⏵ bypass permissions
"""

# Active tool output with no idle prompt — genuinely busy
ACTIVE_NO_IDLE_PROMPT = """\
  Read bridge/activity.py (1-50)
  Edit bridge/ws_handler.py
  Applied 2 changes
"""

# Empty terminal
EMPTY_TERMINAL = ""

# Only whitespace
WHITESPACE_TERMINAL = "   \n\n  \n"

# Idle prompt with > instead of ❯
IDLE_PROMPT_ANGLE_BRACKET = """\
  Done.

>
─────────────────────
🤖 Haiku  $0.01
"""

# Spinner but also idle prompt (edge case — spinner wins)
SPINNER_WITH_IDLE_PROMPT = """\
  ⠹ Running something
❯
"""


# ---------------------------------------------------------------------------
# _has_idle_prompt tests
# ---------------------------------------------------------------------------

class TestHasIdlePrompt:
    """Tests for _has_idle_prompt() — idle prompt detection near bottom of terminal."""

    def test_idle_prompt_present(self):
        assert _has_idle_prompt(IDLE_TERMINAL) is True

    def test_idle_prompt_angle_bracket(self):
        assert _has_idle_prompt(IDLE_PROMPT_ANGLE_BRACKET) is True

    def test_idle_prompt_in_stale_scrollback(self):
        """Idle prompt at bottom with stale tool output above — still idle."""
        assert _has_idle_prompt(STALE_SCROLLBACK_WITH_IDLE_PROMPT) is True

    def test_no_idle_prompt_active(self):
        assert _has_idle_prompt(ACTIVE_WITH_SPINNER) is False

    def test_no_idle_prompt_editing(self):
        assert _has_idle_prompt(ACTIVE_EDITING) is False

    def test_no_idle_prompt_no_prompt(self):
        assert _has_idle_prompt(ACTIVE_NO_IDLE_PROMPT) is False

    def test_empty_terminal(self):
        assert _has_idle_prompt(EMPTY_TERMINAL) is False

    def test_whitespace_terminal(self):
        assert _has_idle_prompt(WHITESPACE_TERMINAL) is False

    def test_bare_prompt_line(self):
        assert _has_idle_prompt("❯\n") is True

    def test_prompt_with_trailing_space(self):
        assert _has_idle_prompt("❯ \n") is True


# ---------------------------------------------------------------------------
# _detect_activity_type tests
# ---------------------------------------------------------------------------

class TestDetectActivityType:
    """Tests for _detect_activity_type() — what is Claude Code doing?"""

    def test_idle_prompt_returns_none(self):
        """When idle prompt is visible and no spinners, return None (idle)."""
        result = _detect_activity_type(IDLE_TERMINAL)
        assert result is None

    def test_spinner_detected_as_thinking(self):
        """Bare spinner with no tool pattern → thinking."""
        result = _detect_activity_type("  ⠹ Working on it...\n")
        assert result == "thinking"

    def test_edit_tool_detected(self):
        """Edit tool pattern without idle prompt → writing."""
        result = _detect_activity_type(ACTIVE_EDITING)
        assert result is not None  # should detect some activity

    def test_stale_scrollback_with_idle_prompt_returns_none(self):
        """Tool names in scrollback ABOVE idle prompt must NOT trigger activity.

        This is the core regression test for the false re-activation bug.
        Before the fix, stale Edit/Read/Bash output above the ❯ prompt
        would re-activate an idle session, blocking voice commands.
        """
        result = _detect_activity_type(STALE_SCROLLBACK_WITH_IDLE_PROMPT)
        assert result is None, (
            "Stale tool names above idle prompt should not be detected as activity. "
            "This was the root cause of the 'queued for VoxHerd' bug."
        )

    def test_active_no_idle_prompt_detects_activity(self):
        """Tool output without idle prompt → detected as activity."""
        result = _detect_activity_type(ACTIVE_NO_IDLE_PROMPT)
        # Should detect Read or Edit
        assert result is not None

    def test_empty_returns_none(self):
        result = _detect_activity_type(EMPTY_TERMINAL)
        assert result is None

    def test_spinner_overrides_idle_prompt(self):
        """If both spinner and idle prompt present, spinner means activity."""
        result = _detect_activity_type(SPINNER_WITH_IDLE_PROMPT)
        # Spinner detected → should not return None
        assert result is not None

    def test_completion_line_while_typing_is_not_active(self):
        """Turn finished (✻ Churned for 1m 9s) and the user is TYPING (prompt
        has text, not a bare ❯), with already-run tool names + the user's own
        words in scrollback. Must NOT detect as active — that scrollback was
        producing the phantom 'Testing 0s' on the footer."""
        terminal = (
            "⏺ Done — committed and pushed.\n"
            "  Bash: pytest tests/ -v\n"
            "  All 70 tests passed.\n"
            "✻ Churned for 1m 9s\n"
            "──────────────\n"
            "❯ i see ran 1 shell command and now testing\n"
            "──────────────\n"
            "🌿 main 🤖 Opus 4.8\n"
        )
        assert _detect_activity_type(terminal) is None

    def test_star_spinner_working_line_with_bare_prompt_is_active(self):
        """Claude's star/asterisk spinner frames (✢ ✶ +) aren't braille, and it
        renders a bare ❯ even while thinking. A present-tense working line must
        still detect as active — else the session gets falsely auto-idled (the
        footer-said-Recombobulating-while-marked-idle bug)."""
        terminal = (
            "⏺ Let me check that.\n"
            "✢ Recombobulating… (12m 57s · ↓ 61.6k tokens · thought for 1s)\n"
            "\n"
            "────────────────\n"
            "❯\n"
            "────────────────\n"
            "🌿 main 🤖 Opus 4.8\n"
        )
        assert _detect_activity_type(terminal) is not None


# ---------------------------------------------------------------------------
# Re-activation guard regression tests
# ---------------------------------------------------------------------------

class TestReactivationGuard:
    """Regression tests for the idle-prompt guard on session re-activation.

    The activity poll loop should NOT re-activate a session when:
    - Tool names appear in terminal scrollback (stale history)
    - BUT the idle prompt (❯) is visible at the bottom

    These tests verify the building blocks that the poll loop relies on.
    """

    def test_stale_edit_above_prompt_not_reactivated(self):
        """Edit in scrollback + idle prompt = should not re-activate."""
        terminal = "  Edit foo.py\n  Applied 1 change\n\n❯\n───\n🤖 Opus\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        # The re-activation condition is: detected is not None AND NOT has_idle_prompt
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is False, "Should not re-activate with idle prompt visible"

    def test_stale_read_above_prompt_not_reactivated(self):
        """Read in scrollback + idle prompt = should not re-activate."""
        terminal = "  Read bridge/server.py (1-100)\n\n❯\n───\n🤖 Opus\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is False

    def test_stale_bash_above_prompt_not_reactivated(self):
        """Bash in scrollback + idle prompt = should not re-activate."""
        terminal = "  Bash: npm test\n  All tests pass\n\n❯\n───\n🤖 Opus\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is False

    def test_active_spinner_no_prompt_should_reactivate(self):
        """Spinner visible + no idle prompt = genuinely busy, should re-activate."""
        terminal = "  ⠹ Running: pytest tests/\n"
        detected = _detect_activity_type(terminal)
        has_prompt = _has_idle_prompt(terminal)
        should_reactivate = detected is not None and not has_prompt
        assert should_reactivate is True, "Active spinner without idle prompt should re-activate"

    def test_no_activity_with_idle_prompt_should_auto_idle(self):
        """No detected activity + idle prompt visible = should auto-idle immediately."""
        detected = _detect_activity_type(IDLE_TERMINAL)
        has_prompt = _has_idle_prompt(IDLE_TERMINAL)
        # The auto-idle condition for active sessions:
        # detected is None AND has_idle_prompt → idle immediately
        should_auto_idle = detected is None and has_prompt
        assert should_auto_idle is True, "Should auto-idle when at prompt with no activity"


class TestActivityTypeGate:
    """Focused unit tests for the status-gated type-write rule.

    Calls the production helper ``_should_overwrite_activity_type``
    directly so any change to the gate logic in ``activity.py`` is
    reflected here without re-implementing the expression.
    """

    def test_idle_session_does_not_overwrite_sleeping(self):
        """Idle session, local fallback "thinking" must not overwrite
        the correct "sleeping" label. This is the recurring leg of the
        macOS=Thinking / iOS=idle desync bug — without the gate, the
        poll loop's per-tick fallback would re-stamp "thinking" every
        ~1.5s after the session went idle."""
        from bridge.activity import _should_overwrite_activity_type
        assert _should_overwrite_activity_type(
            session_status="idle",
            local="thinking",
            current="sleeping",
        ) is False

    def test_waiting_session_does_not_overwrite_approval(self):
        """Same rule for waiting sessions: activity_type is owned by
        update_status (set to "approval" / "input" when the session
        transitioned to .waiting), the poll loop's fallback must not
        clobber it."""
        from bridge.activity import _should_overwrite_activity_type
        assert _should_overwrite_activity_type(
            session_status="waiting",
            local="thinking",
            current="approval",
        ) is False

    def test_active_session_writes_specific_type(self):
        """Normal poll-loop update: active session, detected="writing"
        from _detect_activity_type, must propagate to session.activity_type."""
        from bridge.activity import _should_overwrite_activity_type
        assert _should_overwrite_activity_type(
            session_status="active",
            local="writing",
            current="thinking",
        ) is True

    def test_active_session_falls_back_from_stale_specific_to_thinking(self):
        """Sticky-expiry case: an active session that was "writing"
        but has been quiet >5s. _resolve_activity_type falls back to
        "thinking" (generic active label). The gate MUST allow this
        write — otherwise session.activity_type freezes on the stale
        specific label forever.

        This is exactly the case that a stricter gate of "detected is
        not None" would break (see C1 rejection in the 2026-05-20
        code review)."""
        from bridge.activity import _should_overwrite_activity_type
        assert _should_overwrite_activity_type(
            session_status="active",
            local="thinking",
            current="writing",
        ) is True

    def test_active_session_no_op_when_types_match(self):
        """Idempotency: when the local and current activity_type agree,
        no write should be attempted."""
        from bridge.activity import _should_overwrite_activity_type
        assert _should_overwrite_activity_type(
            session_status="active",
            local="writing",
            current="writing",
        ) is False


@pytest.mark.asyncio
class TestAutoIdleActivityTypeSync:
    """Regression for the 2026-05-20 sync bug: macOS bridge UI showed
    sessions as 'Thinking' while iOS showed the same sessions as 'idle'.

    Root cause: the activity poll loop set ``status='idle'`` via
    ``update_status('idle')`` (which also wrote ``activity_type='sleeping'``),
    then the immediately-following type-change comparison used a stale
    local ``activity_type='thinking'`` (from sticky/default) and
    overwrote ``session.activity_type`` back to 'thinking'. macOS reads
    the raw field via REST polling and showed 'Thinking'; iOS masked the
    bug via ``displayActivityType`` overriding to '.completed' when
    ``status==.idle``.
    """

    async def test_auto_idle_does_not_restamp_sticky_activity(
        self, tmp_path, monkeypatch
    ):
        """After auto-idle, session.activity_type must remain 'sleeping'
        even when a sticky 'thinking' entry was in flight, and the
        sticky entry must be cleared so the next poll tick doesn't
        re-stamp it."""
        from bridge import activity as activity_mod
        from bridge.server_state import _STICKY_ACTIVITY

        monkeypatch.setattr(
            "bridge.session_manager._PERSIST_PATH",
            str(tmp_path / "sessions.json"),
        )

        mgr = SessionManager()
        mgr._sessions.clear()
        session, _ = mgr.register_session(
            "sid", "proj", "/tmp", tmux_target="vh-proj:0.0",
        )
        mgr.update_status("sid", "active", activity_type="thinking")
        _STICKY_ACTIVITY["sid"] = ("thinking", 0.0)

        # Patch the module-level sessions/_STICKY_ACTIVITY so the poll
        # loop operates against our fixtures.
        monkeypatch.setattr(activity_mod, "sessions", mgr)
        monkeypatch.setattr(
            "bridge.activity._LAST_REAL_ACTIVITY", {"sid": 0.0}
        )

        async def _fake_pane_exists(_target):
            return True

        async def _fake_fg(_target):
            return "2.1.144"  # Claude version string — assistant still alive

        async def _fake_subprocess(*args, **kwargs):
            class _Proc:
                stdout = None
                stderr = None
                returncode = 0
                async def communicate(self):
                    return (IDLE_TERMINAL.encode(), b"")
            return _Proc()

        monkeypatch.setattr(mgr, "_tmux_pane_exists", _fake_pane_exists)
        monkeypatch.setattr(activity_mod, "_pane_fg_command", _fake_fg)
        monkeypatch.setattr(
            "asyncio.create_subprocess_exec", _fake_subprocess
        )
        monkeypatch.setattr(
            activity_mod, "broadcast_to_ios", AsyncMock()
        )
        monkeypatch.setattr(
            activity_mod, "_state_sync_msg", lambda: {"type": "state_sync"}
        )

        # Drive one poll iteration via a short-lived task.
        import asyncio
        task = asyncio.create_task(activity_mod._activity_poll_loop())
        await asyncio.sleep(1.8)  # wait one poll cycle
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert session.status == "idle", (
            f"Session should auto-idle (idle prompt visible), got {session.status!r}"
        )
        assert session.activity_type == "sleeping", (
            f"After auto-idle, activity_type must be 'sleeping', got "
            f"{session.activity_type!r} — this is the macOS=Thinking / "
            f"iOS=idle desync bug"
        )
        assert "sid" not in _STICKY_ACTIVITY, (
            "Sticky activity must be cleared on auto-idle so the next "
            "poll tick can't re-stamp 'thinking'"
        )

    async def test_already_idle_session_stays_sleeping_across_ticks(
        self, tmp_path, monkeypatch
    ):
        """Second leg of the macOS=Thinking bug: sessions that are
        ALREADY idle (status='idle', activity_type='sleeping') must not
        get re-stamped to 'thinking' on subsequent poll ticks.

        Reproduces by registering a session, marking it idle, then
        driving multiple poll iterations. The bug was that
        ``_resolve_activity_type`` falls back to 'thinking' when
        ``detected is None`` (no spinners, idle prompt visible), and
        the type-comparison block unconditionally wrote that fallback
        to ``session.activity_type`` regardless of status. The fix
        gates the write on ``session.status == 'active'``.
        """
        from bridge import activity as activity_mod
        from bridge.server_state import _STICKY_ACTIVITY

        monkeypatch.setattr(
            "bridge.session_manager._PERSIST_PATH",
            str(tmp_path / "sessions.json"),
        )

        mgr = SessionManager()
        mgr._sessions.clear()
        session, _ = mgr.register_session(
            "sid", "proj", "/tmp", tmux_target="vh-proj:0.0",
        )
        # Simulate the post-auto-idle steady state.
        mgr.update_status("sid", "idle")
        assert session.status == "idle"
        assert session.activity_type == "sleeping"
        _STICKY_ACTIVITY.pop("sid", None)

        monkeypatch.setattr(activity_mod, "sessions", mgr)
        monkeypatch.setattr(
            "bridge.activity._LAST_REAL_ACTIVITY", {"sid": 0.0}
        )

        async def _fake_pane_exists(_target):
            return True

        async def _fake_fg(_target):
            return "2.1.144"

        async def _fake_subprocess(*args, **kwargs):
            class _Proc:
                stdout = None
                stderr = None
                returncode = 0
                async def communicate(self):
                    return (IDLE_TERMINAL.encode(), b"")
            return _Proc()

        monkeypatch.setattr(mgr, "_tmux_pane_exists", _fake_pane_exists)
        monkeypatch.setattr(activity_mod, "_pane_fg_command", _fake_fg)
        monkeypatch.setattr(
            "asyncio.create_subprocess_exec", _fake_subprocess
        )
        monkeypatch.setattr(
            activity_mod, "broadcast_to_ios", AsyncMock()
        )
        monkeypatch.setattr(
            activity_mod, "_state_sync_msg", lambda: {"type": "state_sync"}
        )

        # Drive multiple poll iterations — the bug only became visible
        # on the SECOND tick (first tick was the auto-idle transition,
        # already covered by the test above). Run ~5s = 3 ticks.
        import asyncio
        task = asyncio.create_task(activity_mod._activity_poll_loop())
        await asyncio.sleep(5.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert session.status == "idle", (
            f"Idle session must stay idle, got {session.status!r}"
        )
        assert session.activity_type == "sleeping", (
            f"After multiple poll ticks on an idle session, activity_type "
            f"must remain 'sleeping' — got {session.activity_type!r}. "
            f"This is the recurring leg of the macOS=Thinking bug: the "
            f"fallback 'thinking' from _resolve_activity_type kept "
            f"overwriting the correct 'sleeping' label every ~1.5s."
        )


# ---------------------------------------------------------------------------
# Assistant inference and auto-correction tests
# ---------------------------------------------------------------------------


class TestInferAssistantFromProcess:
    """Tests for infer_assistant_from_process() — detect assistant from tmux fg command."""

    def test_claude_binary(self):
        assert infer_assistant_from_process("claude") == "claude"

    def test_claude_semver(self):
        assert infer_assistant_from_process("2.1.42") == "claude"

    def test_codex_binary(self):
        assert infer_assistant_from_process("codex") == "codex"

    def test_codex_platform_specific(self):
        """Codex binary may report as platform-specific name (truncated by tmux)."""
        assert infer_assistant_from_process("codex-aarch64-a") == "codex"

    def test_codex_full_platform(self):
        assert infer_assistant_from_process("codex-aarch64-apple-darwin") == "codex"

    def test_gemini_binary(self):
        assert infer_assistant_from_process("gemini") == "gemini"

    def test_shell_returns_none(self):
        assert infer_assistant_from_process("bash") is None

    def test_empty_returns_none(self):
        assert infer_assistant_from_process("") is None

    def test_node_returns_none(self):
        assert infer_assistant_from_process("node") is None


class TestAssistantAutoCorrection:
    """Tests for the activity poll auto-correcting session.assistant.

    When a session is registered as 'claude' but the tmux pane is actually
    running Codex (or vice versa), the poll loop should fix the mismatch.
    """

    def _make_session(self, assistant: str = "claude") -> Session:
        return Session(
            session_id="test-123",
            project="myproject",
            project_dir="/tmp/test",
            assistant=assistant,
            status="active",
        )

    def test_codex_process_corrects_claude_session(self):
        """Session registered as claude but running codex should be corrected."""
        session = self._make_session(assistant="claude")
        fg_cmd = "codex-aarch64-a"
        detected = infer_assistant_from_process(fg_cmd)
        if detected and detected != session.assistant:
            session.assistant = detected
        assert session.assistant == "codex"

    def test_claude_process_corrects_codex_session(self):
        """Session registered as codex but running claude should be corrected."""
        session = self._make_session(assistant="codex")
        fg_cmd = "2.1.42"  # Claude reports semver as process name
        detected = infer_assistant_from_process(fg_cmd)
        if detected and detected != session.assistant:
            session.assistant = detected
        assert session.assistant == "claude"

    def test_matching_assistant_unchanged(self):
        """No correction when process matches registered assistant."""
        session = self._make_session(assistant="claude")
        fg_cmd = "claude"
        detected = infer_assistant_from_process(fg_cmd)
        if detected and detected != session.assistant:
            session.assistant = detected
        assert session.assistant == "claude"

    def test_shell_process_does_not_overwrite(self):
        """Shell process (bash) should not clear the assistant field."""
        session = self._make_session(assistant="codex")
        fg_cmd = "bash"
        detected = infer_assistant_from_process(fg_cmd)
        if detected and detected != session.assistant:
            session.assistant = detected
        assert session.assistant == "codex"


# ---------------------------------------------------------------------------
# Regression: _discover_tmux_sessions
#
# 2026-05-19 bug: bridge restarted with 3 sessions persisted on disk; tmux
# had 5 Claude Code panes total. Discovery should have picked up the
# other 2 at startup AND every 30s in the periodic poll — neither
# happened. The user reported "only 3 sessions in the dashboard, but my
# tmux has 5". After rebuilding the bridge with diagnostic logging, the
# next restart correctly registered all 5.
#
# These tests pin the policy: every tmux session running a supported
# assistant that is NOT already registered (by its `session_name:0.0`
# tmux_target prefix) MUST get a discovered-* registration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDiscoverTmuxSessions:
    """Pins the discovery filter so the May 19 missing-sessions bug
    can't silently regress.

    Strategy: stub out the four subprocess-touching helpers used by
    `_discover_tmux_sessions` (`async_list_sessions`, `_pane_fg_command`,
    `_tmux_pane_path`, and the module-level `sessions` SessionManager)
    so the function runs entirely against in-memory state. The test
    then asserts on what landed in the SessionManager — same observable
    surface as the live bridge.
    """

    @staticmethod
    def _make_tmux_session(name: str) -> dict:
        """Shape that `async_list_sessions` actually returns."""
        return {"name": name, "windows": 1, "created": 0, "attached": True, "activity": 0}

    @staticmethod
    def _fresh_session_manager() -> SessionManager:
        """Empty SessionManager with disk I/O stubbed out."""
        mgr = SessionManager()
        mgr._sessions.clear()
        return mgr

    @staticmethod
    def _pre_register(mgr: SessionManager, *targets: tuple[str, str]) -> None:
        """Seed the manager with sessions whose tmux_targets are the
        given (session_name, project) pairs. Mimics what `_load()`
        would restore from disk on a real bridge restart."""
        # Need a real-looking project_dir for register_session validation;
        # use $HOME/.voxherd/test-projects so it's inside HOME.
        base = os.path.join(os.path.expanduser("~"), ".voxherd", "test-projects", "discovery")
        os.makedirs(base, exist_ok=True)
        with patch.object(mgr, "_save", return_value=None):
            for tmux_name, project in targets:
                pdir = os.path.join(base, project)
                os.makedirs(pdir, exist_ok=True)
                mgr.register_session(
                    session_id=f"sid-{project}",
                    project=project,
                    project_dir=pdir,
                    tmux_target=f"{tmux_name}:0.0",
                )

    async def _run_discovery(
        self,
        tmux_sessions: list[str],
        registered: list[tuple[str, str]],
        *,
        fg_cmd: str = "2.1.144",   # Claude version string — what tmux reports
        pane_cwd: str = "/home/user/dev/test-project",
    ) -> tuple[int, SessionManager]:
        """Drive _discover_tmux_sessions with the supplied tmux state and
        pre-registered sessions; return (discovered_count, mgr)."""
        from bridge import activity as activity_mod

        mgr = self._fresh_session_manager()
        if registered:
            self._pre_register(mgr, *registered)

        # `_load_projects` reads ~/.voxherd/projects.json. Force it
        # empty so all discoveries fall into the "use tmux name as
        # project name" branch — the path that was failing in prod.
        with (
            patch.object(activity_mod, "sessions", mgr),
            patch.object(activity_mod.tmux_manager, "async_list_sessions",
                         AsyncMock(return_value=[self._make_tmux_session(n) for n in tmux_sessions])),
            patch.object(activity_mod, "_pane_fg_command",
                         AsyncMock(return_value=fg_cmd)),
            patch.object(activity_mod, "_tmux_pane_path",
                         AsyncMock(return_value=pane_cwd)),
            patch.object(activity_mod, "_load_projects", return_value=[]),
            patch.object(activity_mod, "log_event", lambda *a, **kw: None),
            # SessionManager._save() schedules an asyncio.to_thread; in tests
            # we don't want the disk side-effect, so stub it out.
            patch.object(mgr, "_save", return_value=None),
        ):
            count = await activity_mod._discover_tmux_sessions()
        return count, mgr

    async def test_registers_sessions_not_in_registered_targets(self):
        """The May 19 bug: bridge had 3 sessions persisted, tmux had 5
        Claude panes, but the 2 missing ones never got registered.
        Pin this exact scenario — given 3 already-registered sessions and
        5 total tmux sessions, the 2 unregistered ones must show up."""
        count, mgr = await self._run_discovery(
            tmux_sessions=["aligned-tools-0", "hack-day-0", "hack-day-1", "patina-0", "voxherd-0"],
            registered=[("patina-0", "engine-rs"), ("voxherd-0", "voxherd"), ("hack-day-1", "hack-day")],
        )

        assert count == 2, "Discovery must find the 2 sessions not in registered_targets"
        targets = {s.tmux_target for s in mgr._sessions.values()}
        assert "aligned-tools-0:0.0" in targets, "aligned-tools-0 must be registered"
        assert "hack-day-0:0.0" in targets, "hack-day-0 must be registered (despite hack-day-1 already being registered — the filter is by tmux session NAME, not project name)"
        assert len(mgr._sessions) == 5, "Final count = 3 pre-existing + 2 discovered"

    async def test_skips_sessions_already_registered(self):
        """Locks in the dedup direction: if a tmux session NAME is in
        the registered_targets set, discovery skips it (no double-
        register, no churn)."""
        count, mgr = await self._run_discovery(
            tmux_sessions=["alpha", "beta"],
            registered=[("alpha", "alpha-proj"), ("beta", "beta-proj")],
        )
        assert count == 0, "Both tmux sessions already registered — nothing new to discover"
        assert len(mgr._sessions) == 2

    async def test_skips_bridge_own_sessions(self):
        """Discovery must skip the bridge's own tmux session by name —
        a regression here would auto-register the bridge as a Claude
        session and the activity poller would read its OWN log output
        as terminal content for every other session."""
        from bridge import tmux_manager
        bridge_name = tmux_manager.BRIDGE_SESSION  # typically "vh-bridge"
        count, mgr = await self._run_discovery(
            tmux_sessions=[bridge_name, "bridge", "real-project"],
            registered=[],
        )
        assert count == 1, "Only 'real-project' should be registered; both bridge names must be skipped"
        targets = {s.tmux_target for s in mgr._sessions.values()}
        assert "real-project:0.0" in targets
        assert f"{bridge_name}:0.0" not in targets
        assert "bridge:0.0" not in targets

    async def test_skips_sessions_without_assistant_process(self):
        """A tmux session whose foreground command isn't an assistant
        (shell prompt, vim, node, etc.) must NOT be registered. Otherwise
        the dashboard fills up with garbage rows the user never
        invoked."""
        count, mgr = await self._run_discovery(
            tmux_sessions=["random-shell"],
            registered=[],
            fg_cmd="bash",  # not a known assistant
        )
        assert count == 0
        assert len(mgr._sessions) == 0

    async def test_uses_tmux_session_name_as_project_when_unconfigured(self):
        """No projects.json → discovery falls back to using the tmux
        session name as the project name and the pane CWD as project
        dir. This is the path the May 19 bug exercised."""
        count, mgr = await self._run_discovery(
            tmux_sessions=["my-cool-project-0"],
            registered=[],
            pane_cwd="/home/user/dev/my-cool-project",
        )
        assert count == 1
        session = next(iter(mgr._sessions.values()))
        assert session.project == "my-cool-project-0"
        assert session.project_dir == "/home/user/dev/my-cool-project"
        assert session.session_id.startswith("discovered-"), "Discovered sessions get a temp ID prefix so hook events can dedup by tmux_target later"

    async def test_register_failure_does_not_block_other_sessions(self):
        """One bad apple (e.g. a tmux session whose name fails
        validation, or whose project_dir resolution throws) must not
        stop discovery from processing the remaining sessions. This is
        the kind of regression the silent `except Exception: pass`
        used to hide."""
        from bridge import activity as activity_mod

        mgr = self._fresh_session_manager()

        # `_tmux_pane_path` will return empty for the bad session, which
        # short-circuits its registration. The good session gets a valid path.
        async def fake_pane_path(name: str) -> str:
            if name == "bad-session":
                return ""  # triggers the "could not resolve project_dir" skip
            return "/home/user/dev/good-project"

        with (
            patch.object(activity_mod, "sessions", mgr),
            patch.object(activity_mod.tmux_manager, "async_list_sessions",
                         AsyncMock(return_value=[
                             self._make_tmux_session("bad-session"),
                             self._make_tmux_session("good-session"),
                         ])),
            patch.object(activity_mod, "_pane_fg_command",
                         AsyncMock(return_value="2.1.144")),
            patch.object(activity_mod, "_tmux_pane_path",
                         side_effect=fake_pane_path),
            patch.object(activity_mod, "_load_projects", return_value=[]),
            patch.object(activity_mod, "log_event", lambda *a, **kw: None),
            patch.object(mgr, "_save", return_value=None),
        ):
            count = await activity_mod._discover_tmux_sessions()

        assert count == 1, "Bad session should be skipped, good session should still register"
        targets = {s.tmux_target for s in mgr._sessions.values()}
        assert "good-session:0.0" in targets
        assert "bad-session:0.0" not in targets

    async def test_tmux_list_failure_returns_zero_does_not_raise(self):
        """If the tmux subprocess itself fails (frozen .app context, no
        tmux on PATH, socket permissions), discovery must return 0 and
        log the error — not raise out of the periodic poll loop."""
        from bridge import activity as activity_mod

        mgr = self._fresh_session_manager()
        with (
            patch.object(activity_mod, "sessions", mgr),
            patch.object(activity_mod.tmux_manager, "async_list_sessions",
                         AsyncMock(side_effect=RuntimeError("tmux not reachable"))),
            patch.object(activity_mod, "log_event", lambda *a, **kw: None),
            patch.object(mgr, "_save", return_value=None),
        ):
            # Must not raise; must return 0
            count = await activity_mod._discover_tmux_sessions()
        assert count == 0
        assert len(mgr._sessions) == 0


# ---------------------------------------------------------------------------
# Session lens-footer fields (added 2026-05-20 for status-footer feature)
# ---------------------------------------------------------------------------


class TestSessionLensFooterFields:
    """Tests that the new lens-footer state fields on Session behave as
    expected: defaults, idle→active edge resets the activity clock, and
    the stream-json buffer is opt-in (None until populated)."""

    def test_new_fields_default_none(self):
        s = Session(session_id="x", project="p", project_dir="/")
        assert s._stream_json_buffer is None
        assert s._last_broadcast_status is None
        assert s._activity_started_at is None

    def test_idle_to_active_sets_activity_started_at(self, tmp_path, monkeypatch):
        """Going active resets the elapsed-clock anchor so the lens
        footer counter starts from 0 on each new turn. Going idle
        clears the anchor so the activity poll doesn't keep
        re-broadcasting a fresh elapsed_s every tick on an idle
        session (over-broadcast bug — see derive_status())."""
        monkeypatch.setattr(
            "bridge.session_manager._PERSIST_PATH",
            str(tmp_path / "sessions.json"),
        )
        mgr = SessionManager()
        session, _ = mgr.register_session(
            "sid", "proj", "/tmp", tmux_target=None,
        )
        # register_session sets status to "active" — that's the first
        # active edge, so _activity_started_at should be set.
        assert session._activity_started_at is not None
        first_start = session._activity_started_at

        # Going idle clears the anchor.
        mgr.update_status("sid", "idle")
        assert session._activity_started_at is None

        # Idle → active edge sets a fresh anchor.
        mgr.update_status("sid", "active")
        assert session._activity_started_at is not None
        assert session._activity_started_at >= first_start

    def test_active_to_active_does_not_reset_clock(self, tmp_path, monkeypatch):
        """Repeated active updates (e.g., from the poll loop reconfirming
        status) must NOT reset the elapsed clock — only a real idle→active
        edge should."""
        monkeypatch.setattr(
            "bridge.session_manager._PERSIST_PATH",
            str(tmp_path / "sessions.json"),
        )
        mgr = SessionManager()
        session, _ = mgr.register_session("sid", "proj", "/tmp")
        first_start = session._activity_started_at
        assert first_start is not None

        # Repeated active update — should be a no-op for the clock.
        mgr.update_status("sid", "active", activity_type="working")
        assert session._activity_started_at == first_start


class TestStatusBarLineRecognition:
    """Sanity check that recently-added chrome patterns trip
    _is_status_bar_line. The choice_detector and snippet extractors both
    rely on this to skip Claude Code TUI hint lines."""

    def test_recognizes_plan_mode_editor_hint(self):
        # Plan-mode trailer below the option block. Without this match,
        # the choice_detector's bottom-up TUI footer scan stops here
        # instead of reaching the plan-mode footer above.
        assert _is_status_bar_line(
            "ctrl-g to edit in  Vim   · ~/.claude/plans/foo.md"
        )

    def test_does_not_match_plan_mode_footer_itself(self):
        # The plan-mode footer ("shift+tab to approve...") is the anchor
        # the choice_detector NEEDS to see. It must NOT be filtered as
        # chrome here — that asymmetry is intentional.
        assert not _is_status_bar_line(
            "     shift+tab to approve with this feedback"
        )
