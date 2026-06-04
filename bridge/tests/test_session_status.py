"""Tests for bridge/session_status.py — the lens-footer status extractor.

Two regex extractors and one merge function. Most of the test value here
is the regex coverage: Claude Code's working-line format is undocumented
and shifts subtly across versions, so we lock down known shapes with
explicit fixtures and add new ones every time we observe a real variant
in the wild.
"""

from __future__ import annotations

from dataclasses import dataclass

from bridge.session_status import (
    StatusLine,
    action_hint,
    derive_status,
    extract_context_percent,
    extract_status_from_stream_json,
    extract_status_from_tmux,
    sticky_context_percent,
    _extract_recent_action,
    _extract_completion,
)


# A minimal Session stand-in. The real bridge.session_manager.Session has
# many fields we don't need here; this lets us construct one inline
# without depending on persistence side effects.
@dataclass
class _FakeSession:
    session_id: str = "fake"
    status: str = "active"
    activity_type: str = "thinking"
    _activity_started_at: float | None = None
    _last_context_pct: int | None = None
    _last_broadcast_status: dict | None = None


# ---------------------------------------------------------------------------
# extract_status_from_tmux
# ---------------------------------------------------------------------------


class TestExtractStatusFromTmux:

    def test_classic_working_line_with_tokens(self):
        lines = [
            "Some prior conversation",
            "",
            "* Blanching… (54s · ↓ 1.3k tokens)",
            "",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Blanching"
        assert s.elapsed_s == 54
        assert s.tokens == "1.3k"
        assert s.spinner is True
        assert s.source == "tmux"

    def test_working_line_without_tokens(self):
        lines = ["* Thinking… (12s)"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Thinking"
        assert s.elapsed_s == 12
        assert s.tokens is None

    def test_ascii_ellipsis_form(self):
        # Some terminals or shells strip the unicode ellipsis to "..."
        lines = ["* Drafting... (3s · 0.2k tokens)"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Drafting"
        assert s.tokens == "0.2k"

    def test_braille_spinner_variant(self):
        lines = ["⠹ Writing (12.4k)"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Writing"
        assert s.tokens == "12.4k"
        assert s.elapsed_s is None  # inner has no Ns

    def test_braille_with_bare_tokens_count(self):
        # Some Claude Code versions emit "(999 tokens)" without the k
        # suffix. The token regex now accepts both forms.
        lines = ["⠹ Drafting (999 tokens)"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Drafting"
        assert s.tokens == "999"

    def test_working_line_without_ellipsis(self):
        # Defensive against a future Claude Code build that drops the
        # ellipsis: the working line still matches because the dots are
        # optional in _WORKING_RE.
        lines = ["* Cogitating (7s)"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Cogitating"
        assert s.elapsed_s == 7

    def test_plus_spinner_frame_combobulating(self):
        # Regression: the spinner animates through "+" (and "✶", "-", …) frames,
        # not just "*". The old fixed glyph class dropped these, so the live
        # verb failed to parse and the footer showed a stale enum label.
        lines = ["+ Combobulating… (7s · thinking with max effort)"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Combobulating"
        assert s.elapsed_s == 7
        assert s.spinner is True

    def test_star_outline_spinner_frame_grooving(self):
        lines = ["✶ Grooving… (42s · ↓ 2.6k tokens · thought for 2s)"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Grooving"
        assert s.elapsed_s == 42
        assert s.tokens == "2.6k"

    def test_can_interrupt_hint(self):
        lines = [
            "* Searching… (7s · 0.5k tokens)",
            "(esc to interrupt)",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.can_interrupt is True

    def test_live_working_line_beats_bare_prompt(self):
        # Claude Code renders an empty "❯" input box at the bottom EVEN WHILE
        # THINKING, so _has_idle_prompt is True during active work. A present-
        # tense gerund working line a few rows above it means ACTIVE work, and
        # must win over the bare-prompt idle heuristic. (Regression for the
        # footer showing a stale "Testing 0s" while Claude was "Finagling".)
        lines = [
            "✢ Finagling… (6m 40s · ↓ 30.2k tokens)",
            "",
            "● How is Claude doing this session? (optional)",
            "1: Bad    2: Fine   3: Good   0: Dismiss",
            "",
            "❯",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Finagling"
        assert s.elapsed_s == 400  # 6m 40s parsed to total seconds
        assert s.spinner is True

    def test_completion_line_shows_as_footer_summary(self):
        # A FINISHED turn shows a past-tense "Worked for 54s" completion line
        # (no gerund working line). The footer surfaces that whole-turn summary
        # — verb + duration, no spinner — not "Waiting for input".
        lines = [
            "⏺ Here's the result.",
            "✻ Worked for 54s",
            "❯",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Worked"
        assert s.elapsed_s == 54
        assert s.spinner is False

    def test_stale_working_line_deep_in_scrollback_is_idle(self):
        # A gerund line FAR above the prompt (beyond the ~8-row content window)
        # is stale scrollback, not the live state — the bare ❯ wins.
        lines = ["* Blanching… (54s)"] + [f"output line {i}" for i in range(10)] + ["❯"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Waiting for input"

    def test_no_match_returns_none(self):
        lines = ["just some output", "no working line here"]
        assert extract_status_from_tmux(lines) is None

    def test_empty_input_returns_none(self):
        assert extract_status_from_tmux([]) is None

    def test_chrome_lines_are_skipped(self):
        # The bypass-permissions/model footer should NOT short-circuit the
        # scan — we still find the working line above it.
        lines = [
            "* Cogitating… (8s)",
            "  🌿 main 🤖 Opus 4.7",
            "  ►► bypass permissions on (shift+tab to cycle)",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Cogitating"


# ---------------------------------------------------------------------------
# extract_status_from_stream_json
# ---------------------------------------------------------------------------


class TestExtractStatusFromStreamJson:

    def test_empty_buffer_returns_none(self):
        assert extract_status_from_stream_json([]) is None

    def test_tool_use_edit_maps_to_writing(self):
        events = [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/x"}}],
                "usage": {"input_tokens": 1000, "output_tokens": 500},
            }},
        ]
        s = extract_status_from_stream_json(events)
        assert s is not None
        assert s.label == "Writing"
        assert s.tokens == "1.5k"

    def test_tool_use_bash_with_test_command_refines_to_testing(self):
        events = [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                             "input": {"command": "pytest bridge/tests/", "description": "Run tests"}}],
                "usage": {"input_tokens": 800, "output_tokens": 200},
            }},
        ]
        s = extract_status_from_stream_json(events)
        assert s is not None
        assert s.label == "Testing"

    def test_tool_use_bash_with_build_command_refines_to_building(self):
        events = [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                             "input": {"command": "xcodebuild -scheme VoxHerd"}}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }},
        ]
        s = extract_status_from_stream_json(events)
        assert s is not None
        assert s.label == "Building"

    def test_tool_use_read_maps_to_searching(self):
        events = [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}],
                "usage": {"input_tokens": 50, "output_tokens": 25},
            }},
        ]
        s = extract_status_from_stream_json(events)
        assert s is not None
        assert s.label == "Searching"

    def test_assistant_without_tool_use_is_thinking(self):
        events = [
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "I'm pondering..."}],
                "usage": {"input_tokens": 50, "output_tokens": 10},
            }},
        ]
        s = extract_status_from_stream_json(events)
        assert s is not None
        assert s.label == "Thinking"
        assert s.tokens == "60"  # below 1000 → no k suffix

    def test_result_event_ends_extraction(self):
        events = [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Edit", "input": {}}],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }},
            {"type": "result", "subtype": "success", "result": "done"},
        ]
        assert extract_status_from_stream_json(events) is None

    def test_token_formatting_thousands(self):
        events = [
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Write", "input": {}}],
                "usage": {"input_tokens": 8000, "output_tokens": 4000},
            }},
        ]
        s = extract_status_from_stream_json(events)
        assert s is not None
        assert s.tokens == "12k"

    def test_malformed_event_skipped(self):
        events = [
            "not a dict",
            None,
            {"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Edit", "input": {}}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }},
        ]
        s = extract_status_from_stream_json(events)
        assert s is not None
        assert s.label == "Writing"


# ---------------------------------------------------------------------------
# derive_status — merge precedence
# ---------------------------------------------------------------------------


class TestDeriveStatus:

    def test_stream_json_beats_tmux(self):
        session = _FakeSession()
        tmux = StatusLine(label="Blanching", elapsed_s=10, tokens="0.5k", source="tmux")
        stream = StatusLine(label="Writing", elapsed_s=None, tokens="2.0k", source="stream_json")
        d = derive_status(session, tmux, stream)
        assert d["label"] == "Writing"
        assert d["tokens"] == "2.0k"
        assert d["source"] == "stream_json"

    def test_tmux_used_when_no_stream(self):
        session = _FakeSession()
        tmux = StatusLine(label="Drafting", elapsed_s=8, tokens=None, source="tmux")
        d = derive_status(session, tmux, None)
        assert d["label"] == "Drafting"
        assert d["elapsed_s"] == 8
        assert d["source"] == "tmux"

    def test_falls_back_to_activity_type(self):
        session = _FakeSession(activity_type="testing")
        d = derive_status(session, None, None)
        assert d["label"] == "Testing"
        assert d["spinner"] is True
        assert d["source"] == "fallback"

    def test_falls_back_idle_when_sleeping(self):
        session = _FakeSession(activity_type="sleeping", status="idle")
        d = derive_status(session, None, None)
        assert d["label"] == "Idle"
        assert d["spinner"] is False

    def test_finished_or_waiting_idle_session_does_not_spin(self):
        # Connect-replay re-derives a fallback status for idle sessions. A
        # just-finished/waiting one must NOT animate a spinner — otherwise
        # the lens shows e.g. a spinning "Completed", implying it's still
        # working.
        for activity, label in (
            ("completed", "Completed"),
            ("errored", "Error"),
            ("stopped", "Stopped"),
            ("registered", "Ready"),
            ("approval", "Waiting for approval"),
            ("input", "Waiting for input"),
            ("dead", "Disconnected"),
        ):
            d = derive_status(_FakeSession(status="idle", activity_type=activity), None, None)
            assert d["label"] == label
            assert d["spinner"] is False, f"{label} must not spin"

    def test_active_work_labels_spin(self):
        for activity in ("thinking", "writing", "searching", "running", "building", "testing", "working"):
            d = derive_status(_FakeSession(status="active", activity_type=activity), None, None)
            assert d["spinner"] is True, f"{activity} should spin"

    def test_idle_session_with_stale_work_label_does_not_spin(self):
        # Real case from the connect-replay verification: an idle session
        # whose activity_type was never reset (still "thinking") must NOT
        # spin — the status gate catches the stale work label.
        d = derive_status(_FakeSession(status="idle", activity_type="thinking"), None, None)
        assert d["label"] == "Thinking"
        assert d["spinner"] is False

    def test_elapsed_filled_from_session_started_at(self):
        import time
        session = _FakeSession(_activity_started_at=time.monotonic() - 30.0)
        tmux = StatusLine(label="Searching", elapsed_s=None, source="tmux")
        d = derive_status(session, tmux, None)
        # Allow ±2s wobble for test timing.
        assert d["elapsed_s"] is not None
        assert 28 <= d["elapsed_s"] <= 32

    def test_hint_included(self):
        session = _FakeSession(status="active", activity_type="writing")
        d = derive_status(session, None, None)
        assert d["hint"] == "Tap: interrupt"

    def test_context_pct_rides_on_payload(self):
        session = _FakeSession()
        d = derive_status(session, None, None, context_pct=42)
        assert d["context_pct"] == 42
        # Defaults to None when caller didn't extract one.
        d2 = derive_status(session, None, None)
        assert d2["context_pct"] is None


# ---------------------------------------------------------------------------
# extract_context_percent — Claude Code "X/Yk" chrome scanner
# ---------------------------------------------------------------------------


class TestExtractContextPercent:

    def test_zero_over_thousand_k_is_zero(self):
        lines = ["🤖 Opus 4.7 ⚡max ✏️ +7044 -578 $48.83 0/1000k ○○○○"]
        assert extract_context_percent(lines) == 0

    def test_150k_over_200k_is_75(self):
        lines = ["🤖 Sonnet 4.6 $114.45 150k/200k ○●●●"]
        assert extract_context_percent(lines) == 75

    def test_fractional_used_token_count(self):
        lines = ["🤖 Opus 4.7 $9.50 15.3k/200k ○○○●"]
        assert extract_context_percent(lines) == 8  # round(15.3/200 * 100)

    def test_three_ninety_over_thousand_k_is_thirty_nine(self):
        # The "39%" the user asked about — verifies the round-trip.
        lines = ["🤖 Opus 4.7 +0 -0 $0.10 390/1000k ●●○○"]
        assert extract_context_percent(lines) == 39

    def test_no_context_indicator_returns_none(self):
        lines = ["random terminal output", "no chrome here", "echo hello"]
        assert extract_context_percent(lines) is None

    def test_empty_lines_returns_none(self):
        assert extract_context_percent([]) is None

    def test_date_like_string_does_not_match(self):
        # "20/24/2026" looks slash-numeric but has no `k` suffix on the
        # second number — the regex bound rejects it.
        lines = ["meeting 20/24/2026 at noon"]
        assert extract_context_percent(lines) is None

    def test_uses_most_recent_line(self):
        lines = [
            "stale chrome 100k/200k ●○○○",
            "fresher chrome 150k/200k ●●○○",
        ]
        assert extract_context_percent(lines) == 75

    def test_literal_context_used_line_is_parsed(self):
        # Some builds / high-usage states print this instead of the gauge.
        lines = ["                              89% context used"]
        assert extract_context_percent(lines) == 89

    def test_gauge_wins_when_both_present(self):
        # hack-day pane in the wild: literal "% context used" sits above
        # the X/Yk status bar. The gauge is primary; the literal is only a
        # fallback, so the gauge value (87) wins over the literal (89).
        lines = [
            "89% context used",
            "🤖 Opus 4.7 ⚡max ✏️ +9941 -1327 $344.86 868k/1000k ●●●●",
        ]
        assert extract_context_percent(lines) == 87

    def test_truncated_gauge_returns_none(self):
        # Claude Code clips the status line to terminal width; when the
        # X/Yk gauge falls off the right edge there's nothing to parse.
        assert extract_context_percent(["...$535.49 248.7k/100…"]) is None
        assert extract_context_percent(["...$535.49 248.7k/…"]) is None

    def test_degenerate_gauge_falls_through_to_literal(self):
        # A zero-budget gauge ("5k/0k") must not bail early — the literal
        # "% context used" line on the same frame should still win.
        lines = ["weird 5k/0k gauge", "89% context used"]
        assert extract_context_percent(lines) == 89


# ---------------------------------------------------------------------------
# sticky_context_percent — hold last value across frames with no gauge
# ---------------------------------------------------------------------------


class TestStickyContextPercent:

    def test_hit_returns_parsed_value_ignoring_last(self):
        lines = ["🤖 Opus 4.7 $9.50 150k/200k ●●○○"]
        assert sticky_context_percent(lines, last=12) == 75

    def test_miss_holds_last_value(self):
        # Truncated/working frame with no parseable gauge keeps the chip.
        assert sticky_context_percent(["...248.7k/100…"], last=42) == 42

    def test_miss_with_no_prior_value_is_none(self):
        assert sticky_context_percent(["no chrome here"], last=None) is None


# ---------------------------------------------------------------------------
# _replay_status_payload — what a freshly-connected client gets per session
# ---------------------------------------------------------------------------


class TestReplayStatusPayload:

    def test_idle_session_carries_sticky_context_pct(self):
        # The bug: idle sessions were skipped on connect, so their
        # context_pct never reached the lens (only the active session
        # showed a %). Now idle sessions get a fresh fallback status that
        # carries the sticky context_pct.
        from bridge.ws_handler import _replay_status_payload
        s = _FakeSession(status="idle", activity_type="input", _last_context_pct=42)
        p = _replay_status_payload(s)
        assert p is not None
        assert p["context_pct"] == 42
        assert p["label"] == "Waiting for input"  # fresh enum label, not a stale verb

    def test_active_session_replays_cached_verbatim(self):
        from bridge.ws_handler import _replay_status_payload
        cached = {"label": "Blanching", "context_pct": 70, "source": "tmux"}
        s = _FakeSession(status="active", _last_broadcast_status=cached)
        assert _replay_status_payload(s) is cached

    def test_active_without_cache_returns_none(self):
        from bridge.ws_handler import _replay_status_payload
        s = _FakeSession(status="active", _last_broadcast_status=None)
        assert _replay_status_payload(s) is None

    def test_idle_without_prior_context_is_none(self):
        from bridge.ws_handler import _replay_status_payload
        s = _FakeSession(status="idle", activity_type="sleeping", _last_context_pct=None)
        p = _replay_status_payload(s)
        assert p["context_pct"] is None
        assert p["label"] == "Idle"

    def test_connect_replay_includes_idle_sessions(self):
        # The actual bug: idle sessions were skipped on connect, so only
        # the one active session ever showed a context %. This guards the
        # loop that builds the connect-replay burst — every session must
        # get a message, with idle sessions carrying their sticky %.
        from bridge.ws_handler import _connect_replay_status_messages
        all_sessions = {
            "act": _FakeSession(
                status="active",
                _last_broadcast_status={"label": "Writing", "context_pct": 50, "source": "tmux"},
            ),
            "idle1": _FakeSession(status="idle", activity_type="input", _last_context_pct=21),
            "idle2": _FakeSession(status="idle", activity_type="sleeping", _last_context_pct=80),
        }
        msgs = _connect_replay_status_messages(all_sessions)
        by_sid = {m["session_id"]: m for m in msgs}
        assert set(by_sid) == {"act", "idle1", "idle2"}, "every session must be replayed, not just active"
        assert all(m["type"] == "session_status" for m in msgs)
        assert by_sid["idle1"]["context_pct"] == 21
        assert by_sid["idle2"]["context_pct"] == 80
        assert by_sid["act"]["context_pct"] == 50

    def test_connect_replay_skips_active_session_without_cache(self):
        # An active session with no cached status yet has nothing useful to
        # replay — it's omitted (the next poll edge delivers one).
        from bridge.ws_handler import _connect_replay_status_messages
        all_sessions = {
            "act": _FakeSession(status="active", _last_broadcast_status=None),
            "idle": _FakeSession(status="idle", activity_type="input", _last_context_pct=33),
        }
        by_sid = {m["session_id"]: m for m in _connect_replay_status_messages(all_sessions)}
        assert set(by_sid) == {"idle"}
        assert by_sid["idle"]["context_pct"] == 33

    def test_idle_session_replays_settled_action_label(self):
        # The fix: a client connecting AFTER a session went idle must still see
        # "what Claude just did". The settled (spinner=False) cache from the
        # live poll carries that action label, so replay it verbatim.
        from bridge.ws_handler import _replay_status_payload
        cached = {"label": "Ran 1 shell command", "spinner": False,
                  "context_pct": 14, "source": "tmux"}
        s = _FakeSession(status="idle", activity_type="sleeping",
                         _last_broadcast_status=cached, _last_context_pct=14)
        assert _replay_status_payload(s) is cached

    def test_idle_session_skips_stale_spinning_cache(self):
        # A still-spinning cache is a working label left over from a turn that
        # has since finished — must NOT replay it (that would show a spinner on
        # an idle session). Fall back to the fresh enum label instead.
        from bridge.ws_handler import _replay_status_payload
        cached = {"label": "Blanching", "spinner": True,
                  "context_pct": 60, "source": "tmux"}
        s = _FakeSession(status="idle", activity_type="sleeping",
                         _last_broadcast_status=cached, _last_context_pct=60)
        p = _replay_status_payload(s)
        assert p is not cached
        assert p["label"] == "Idle"
        assert p["context_pct"] == 60


# ---------------------------------------------------------------------------
# action_hint
# ---------------------------------------------------------------------------


class TestActionHint:

    def test_approval_takes_priority(self):
        s = _FakeSession(status="active", activity_type="approval")
        assert action_hint(s) == "Tap: approve · Hold: deny"

    def test_active_status_means_interrupt(self):
        s = _FakeSession(status="active", activity_type="writing")
        assert action_hint(s) == "Tap: interrupt"

    def test_input_request(self):
        s = _FakeSession(status="active", activity_type="input")
        assert action_hint(s) == "Tap: type · Hold: voice"

    def test_idle_default(self):
        s = _FakeSession(status="idle", activity_type="sleeping")
        assert action_hint(s) == "Tap: voice"


# ---------------------------------------------------------------------------
# Idle footer shows the most recent action ("Ran 1 shell command") instead
# of the bare "Waiting for input".
# ---------------------------------------------------------------------------

class TestRecentActionInFooter:
    def test_idle_shows_recent_shell_command_action(self):
        lines = [
            "⏺ Done — committed and pushed.",
            "  Thought for 38s, ran 1 shell command",
            "",
            "❯",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Ran 1 shell command"
        assert s.spinner is False

    def test_idle_standalone_ran_shell_command(self):
        lines = ["  Ran 1 shell command", "❯"]
        s = extract_status_from_tmux(lines)
        assert s is not None and s.label == "Ran 1 shell command"

    def test_idle_keeps_called_tool_preamble(self):
        lines = ["Thought for 15s, called claude-in-chrome, ran 1 shell command", "❯"]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label.lower().startswith("called claude-in-chrome")

    def test_idle_without_action_still_waiting_for_input(self):
        lines = ["some answer Claude printed", "", "❯"]
        s = extract_status_from_tmux(lines)
        assert s is not None and s.label == "Waiting for input"

    def test_recent_action_ignores_prose(self):
        # Doesn't open with the verbatim "Thought for <dur>" header, nor the
        # quantified/end-anchored bare-action shape.
        assert _extract_recent_action(["I ran the build and it worked"]) is None
        assert _extract_recent_action(["the script ran for a while"]) is None
        assert _extract_recent_action(["called the helper function twice"]) is None
        assert _extract_recent_action([]) is None

    def test_recent_action_picks_most_recent(self):
        lines = [
            "Ran 5 shell commands",
            "more output",
            "Ran 1 shell command",
        ]
        assert _extract_recent_action(lines) == "Ran 1 shell command"

    # --- Generalized beyond shell commands: every action type Claude reports ---

    def test_idle_shows_read_file_action(self):
        lines = ["⏺ Here's what I found.", "  Thought for 30s, read 1 file", "❯"]
        s = extract_status_from_tmux(lines)
        assert s is not None and s.label == "Read 1 file"

    def test_idle_shows_edited_files_action(self):
        lines = ["Thought for 12s, edited 3 files", "❯"]
        s = extract_status_from_tmux(lines)
        assert s is not None and s.label == "Edited 3 files"

    def test_idle_bare_read_files(self):
        assert _extract_recent_action(["Read 3 files", "❯"]) == "Read 3 files"

    def test_idle_bare_wrote_file(self):
        assert _extract_recent_action(["Wrote 1 file", "❯"]) == "Wrote 1 file"

    def test_pure_thinking_turn_surfaces_thought(self):
        lines = ["⏺ Sure — here's the answer.", "Thought for 1m 3s", "❯"]
        s = extract_status_from_tmux(lines)
        assert s is not None and s.label == "Thought for 1m 3s"

    def test_thought_for_prose_is_not_a_summary(self):
        # "Thought for a moment" has no time token in the dur slot, so the
        # "Thought for <dur>" anchor must reject it (no false action surface).
        assert _extract_recent_action(["Thought for a moment, then I replied"]) is None

    def test_bare_action_with_trailing_prose_not_matched(self):
        # A real summary ends right after the noun; prose continues past it.
        assert _extract_recent_action(["ran 5 shell commands to verify the fix"]) is None
        assert _extract_recent_action(["read 2 files and then summarized them"]) is None

    def test_picks_latest_across_mixed_action_types(self):
        lines = [
            "Thought for 40s, edited 2 files",
            "⏺ tool output",
            "Thought for 8s, read 1 file",   # latest summary (closest to bottom)
            "⏺ Done.",
            "❯",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None and s.label == "Read 1 file"

    def test_called_tool_only_form(self):
        assert _extract_recent_action(["Called claude-in-chrome", "❯"]) == "Called claude-in-chrome"

    def test_completion_summary_shows_while_user_is_typing(self):
        # After a finished turn the user starts typing: the prompt holds text
        # (so it's NOT a bare ❯) and the completion line sits above. The footer
        # shows the completion summary ("Churned · 1m 9s"), not a stale enum or
        # the phantom "Testing 0s". The user's own typed text must NOT interfere.
        lines = [
            "⏺ Done — committed and pushed.",
            "  Thought for 38s, ran 1 shell command",
            "✻ Churned for 1m 9s",
            "❯ i see ran 1 shell command and now testing 0s",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Churned"
        assert s.elapsed_s == 69  # 1m 9s
        assert s.spinner is False

    def test_action_label_is_fallback_when_no_completion_line(self):
        # No completion line in view (scrolled off) → fall back to the recent
        # action rather than "Waiting for input" or a stale enum.
        lines = [
            "⏺ Done.",
            "  Thought for 38s, ran 1 shell command",
            "❯ typing a reply here",
        ]
        s = extract_status_from_tmux(lines)
        assert s is not None
        assert s.label == "Ran 1 shell command"
        assert s.spinner is False

    def test_extract_completion_duration_forms(self):
        assert _extract_completion(["✻ Crunched for 9m 27s"]) == ("Crunched", 567)
        assert _extract_completion(["✻ Churned for 1m 9s"]) == ("Churned", 69)
        assert _extract_completion(["✻ Worked for 54s"]) == ("Worked", 54)
        assert _extract_completion(["✻ Worked for 6m"]) == ("Worked", 360)
        assert _extract_completion(["✻ Cooked for 1h 2m 3s"]) == ("Cooked", 3723)

    def test_extract_completion_requires_leading_glyph_not_prose(self):
        # Plain prose has no leading glyph → not a completion line.
        assert _extract_completion(["Waited for 5s", "❯"]) is None
        assert _extract_completion(["I worked for hours on this", "❯"]) is None
        assert _extract_completion(["the build ran for a while", "❯"]) is None
