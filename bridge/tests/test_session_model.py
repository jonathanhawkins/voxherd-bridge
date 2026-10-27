"""A session's model, tracked apart from its assistant.

One assistant runs many models -- Antigravity is multi-provider, and Claude
Code switches with ``/model`` -- so ``Session.model`` is its own axis. It comes
from the SessionStart hook when the assistant says, and is refreshed from the
transcript at every Stop, because the Stop hook does not carry it.

Every failure here is silent: a header naming the wrong model looks exactly
like one naming the right one.
"""

from __future__ import annotations

import json
import os
import types
from unittest.mock import patch

import httpx
import pytest

from bridge import bridge_server
from bridge import session_manager as sm
from bridge import transcript_render as tr
from bridge.session_manager import SessionManager
from bridge.tests.conftest import _TEST_PROJECT_DIR, auth_headers
from bridge.validation import _sanitize_model

# ---------------------------------------------------------------------------
# What counts as a model id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", [
    "claude-opus-5-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5[1m]",
    "us.anthropic.claude-opus-4-20250514-v1:0",
    "claude-opus-4@20250514",
    "anthropic/claude-opus-4",
    "qwen3.8:27b-mlx",
    "gemini-3.5-flash-lite",
    "gpt-5.5-codex",
])
def test_every_provider_spelling_is_a_model(model: str) -> None:
    assert _sanitize_model(model) == model


def test_surrounding_whitespace_is_not_part_of_it() -> None:
    assert _sanitize_model("  claude-opus-5-5\n") == "claude-opus-5-5"


@pytest.mark.parametrize("value", [
    "<synthetic>",  # Claude Code's marker for a turn it made up
    "", "   ", None, 42, {"id": "claude-opus-5-5"},
    "claude; rm -rf ~", "$(whoami)", "two words", "-flag-looking",
    "x" * 101,
])
def test_anything_else_is_dropped(value: object) -> None:
    assert _sanitize_model(value) == ""


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

@pytest.fixture
def mgr(tmp_path, monkeypatch) -> SessionManager:
    monkeypatch.setattr(sm, "_PERSIST_PATH", str(tmp_path / "sessions.json"))
    manager = SessionManager()
    manager._save = lambda: None
    return manager


def _register(mgr: SessionManager, sid: str, *, model: str = "", assistant: str = "claude",
              tmux: str | None = None):
    session, _ = mgr.register_session(
        sid, "voxherd", _TEST_PROJECT_DIR, tmux_target=tmux, assistant=assistant, model=model,
    )
    return session


def test_register_records_the_model(mgr: SessionManager) -> None:
    assert _register(mgr, "s-1", model="claude-opus-5-5").model == "claude-opus-5-5"
    assert mgr.to_dict()["s-1"]["model"] == "claude-opus-5-5"


def test_an_unknown_model_is_empty_not_absent(mgr: SessionManager) -> None:
    # Clients read the key; an older bridge simply leaves it out.
    _register(mgr, "s-1")
    assert mgr.to_dict()["s-1"]["model"] == ""


def test_re_registering_without_a_model_keeps_it(mgr: SessionManager) -> None:
    _register(mgr, "s-1", model="claude-opus-5-5")
    assert _register(mgr, "s-1").model == "claude-opus-5-5"


def test_a_new_session_in_the_same_pane_keeps_the_model(mgr: SessionManager) -> None:
    # /clear starts a new session id in the same terminal, same process.
    _register(mgr, "old", model="claude-opus-5-5", tmux="vh-voxherd:0.0")
    session = _register(mgr, "new", tmux="vh-voxherd:0.0")
    assert mgr.get_session("old") is None
    assert session.model == "claude-opus-5-5"


def test_a_pane_that_changed_assistant_does_not_keep_the_model(mgr: SessionManager) -> None:
    _register(mgr, "old", model="claude-opus-5-5", tmux="vh-voxherd:0.0")
    assert _register(mgr, "new", assistant="codex", tmux="vh-voxherd:0.0").model == ""


def test_a_named_model_beats_the_inherited_one(mgr: SessionManager) -> None:
    _register(mgr, "s-1", model="claude-opus-5")
    assert _register(mgr, "s-1", model="claude-fable-5-1").model == "claude-fable-5-1"


def test_set_model_reports_a_change_once(mgr: SessionManager) -> None:
    _register(mgr, "s-1", model="claude-opus-5")
    assert mgr.set_model("s-1", "claude-opus-5-5") is True
    assert mgr.get_session("s-1").model == "claude-opus-5-5"
    assert mgr.set_model("s-1", "claude-opus-5-5") is False


def test_nothing_known_never_clears_what_is(mgr: SessionManager) -> None:
    _register(mgr, "s-1", model="claude-opus-5-5")
    for reading in ("", "<synthetic>", "not a model"):
        assert mgr.set_model("s-1", reading) is False
    assert mgr.get_session("s-1").model == "claude-opus-5-5"
    assert mgr.set_model("gone", "claude-opus-5-5") is False


def test_the_model_survives_a_bridge_restart(tmp_path, monkeypatch) -> None:
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(sm, "_PERSIST_PATH", str(path))
    before = SessionManager()
    before.register_session("s-1", "voxherd", _TEST_PROJECT_DIR, model="claude-opus-5-5")
    after = SessionManager()
    assert after.get_session("s-1").model == "claude-opus-5-5"


def test_a_tampered_model_on_disk_is_dropped(tmp_path, monkeypatch) -> None:
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(sm, "_PERSIST_PATH", str(path))
    path.write_text(json.dumps({"sessions": {"s-1": {
        "session_id": "s-1", "project": "voxherd", "project_dir": _TEST_PROJECT_DIR,
        "model": "$(curl evil)",
    }}}))
    assert SessionManager().get_session("s-1").model == ""


# ---------------------------------------------------------------------------
# Reading the model from the transcript
# ---------------------------------------------------------------------------

_SID = "5d2c1f42-7def-43ef-bc86-b02ade04a7cf"


def _turn(model: str, *, sidechain: bool = False, text: str = "done") -> dict:
    record = {"type": "assistant",
              "message": {"role": "assistant", "model": model,
                          "content": [{"type": "text", "text": text}]}}
    if sidechain:
        record["isSidechain"] = True
    return record


def _prompt(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """Write a transcript where find_transcript_path looks; return the session."""
    root = os.path.realpath(str(tmp_path))
    monkeypatch.setattr(tr, "_allowed_transcript_roots", lambda assistant="claude": [root])
    folder = tmp_path / "projects" / "-Users-dev-voxherd"
    folder.mkdir(parents=True)

    def write(records: list, tail: str = ""):
        with open(folder / f"{_SID}.jsonl", "w") as f:
            f.writelines(json.dumps(record) + "\n" for record in records)
            f.write(tail)
        return types.SimpleNamespace(assistant="claude", session_id=_SID)

    return write


def test_the_newest_turn_names_the_model(transcript) -> None:
    session = transcript([_prompt(), _turn("claude-opus-5"), _prompt("/model"), _turn("claude-fable-5-1")])
    assert tr.latest_model(session) == "claude-fable-5-1"


def test_a_sub_agent_on_another_model_is_not_the_session(transcript) -> None:
    session = transcript([_turn("claude-opus-5-5"), _turn("claude-haiku-4-5-20251001", sidechain=True)])
    assert tr.latest_model(session) == "claude-opus-5-5"


def test_a_synthetic_turn_names_nothing(transcript) -> None:
    session = transcript([_turn("claude-opus-5-5"), _turn("<synthetic>", text="API Error")])
    assert tr.latest_model(session) == "claude-opus-5-5"


def test_a_line_still_being_written_is_skipped(transcript) -> None:
    session = transcript([_turn("claude-opus-5-5")], tail='{"type":"assistant","message":{"model":"claude-fa')
    assert tr.latest_model(session) == "claude-opus-5-5"


def test_lines_split_across_read_windows_are_joined(transcript, monkeypatch) -> None:
    # Every line is several windows long, so each one is read in pieces.
    monkeypatch.setattr(tr, "_MODEL_SCAN_CHUNK", 16)
    session = transcript([_turn("claude-opus-5"), _prompt("x" * 200), _turn("claude-opus-5-5")])
    assert tr.latest_model(session) == "claude-opus-5-5"


def test_the_first_line_of_the_file_counts(transcript, monkeypatch) -> None:
    monkeypatch.setattr(tr, "_MODEL_SCAN_CHUNK", 16)
    session = transcript([_turn("claude-opus-5-5"), _prompt("y" * 200)])
    assert tr.latest_model(session) == "claude-opus-5-5"


def test_the_scan_gives_up_at_its_budget(transcript, monkeypatch) -> None:
    session = transcript([_turn("claude-opus-5-5"), _prompt("z" * 5000)])
    assert tr.latest_model(session) == "claude-opus-5-5"
    monkeypatch.setattr(tr, "_MODEL_SCAN_CHUNK", 1024)
    monkeypatch.setattr(tr, "_MODEL_SCAN_MAX_BYTES", 2048)
    assert tr.latest_model(session) == ""


def test_no_turn_yet_and_no_transcript_are_both_unknown(transcript) -> None:
    assert tr.latest_model(transcript([_prompt()])) == ""
    missing = types.SimpleNamespace(assistant="claude", session_id="00000000-0000-0000-0000-000000000000")
    assert tr.latest_model(missing) == ""


# ---------------------------------------------------------------------------
# Over the API
# ---------------------------------------------------------------------------

async def _register_via_api(client: httpx.AsyncClient, sid: str, **extra) -> httpx.Response:
    return await client.post(
        "/api/sessions/register",
        json={"session_id": sid, "project": "myproject", "project_dir": _TEST_PROJECT_DIR, **extra},
        headers=auth_headers(),
    )


async def _listed(client: httpx.AsyncClient) -> dict:
    return (await client.get("/api/sessions", headers=auth_headers())).json()


@pytest.mark.asyncio
async def test_register_takes_the_model_from_session_start(client: httpx.AsyncClient) -> None:
    assert (await _register_via_api(client, "m1", model="claude-opus-5-5")).json() == {"ok": True}
    assert (await _listed(client))["m1"]["model"] == "claude-opus-5-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["$(whoami)", 42, {"id": "claude-opus-5-5"}, None])
async def test_a_malformed_model_never_blocks_registration(client: httpx.AsyncClient, junk) -> None:
    assert (await _register_via_api(client, "m2", model=junk)).json() == {"ok": True}
    assert (await _listed(client))["m2"]["model"] == ""


async def _stop(client: httpx.AsyncClient, sid: str) -> list[dict]:
    sent: list[dict] = []

    async def capture(msg: dict) -> None:
        sent.append(msg)

    with patch("bridge.routes.broadcast_to_ios", new=capture):
        resp = await client.post(
            "/api/events",
            json={"event": "stop", "session_id": sid, "project": "myproject",
                  "summary": "did a thing", "stop_reason": "end_turn"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    return [m for m in sent if m.get("type") == "agent_event" and m.get("event") == "stop"]


@pytest.mark.asyncio
async def test_a_stop_reads_the_model_from_the_transcript(client: httpx.AsyncClient) -> None:
    await _register_via_api(client, "m3", model="claude-opus-5")
    with patch.object(tr, "latest_model", return_value="claude-fable-5-1"):
        events = await _stop(client, "m3")
    assert bridge_server.sessions.get_session("m3").model == "claude-fable-5-1"
    assert events and events[0]["model"] == "claude-fable-5-1", \
        "the stop broadcast carries the model, so a client can show a /model switch"


@pytest.mark.asyncio
async def test_a_stop_with_nothing_readable_keeps_the_model(client: httpx.AsyncClient) -> None:
    await _register_via_api(client, "m4", model="claude-opus-5-5")
    with patch.object(tr, "latest_model", return_value=""):
        events = await _stop(client, "m4")
    assert events[0]["model"] == "claude-opus-5-5"


async def _stop_with_model(client: httpx.AsyncClient, sid: str, model: object) -> None:
    await client.post(
        "/api/events",
        json={"event": "stop", "session_id": sid, "project": "myproject",
              "summary": "did a thing", "stop_reason": "end_turn", "model": model},
        headers=auth_headers(),
    )


@pytest.mark.asyncio
async def test_a_stop_can_name_the_model_when_no_transcript_can(client: httpx.AsyncClient) -> None:
    # Antigravity: the bridge cannot read its transcript, but its hooks carry
    # modelName on every call, and one agy runs Gemini, Claude or GPT-OSS.
    await _register_via_api(client, "m5", assistant="antigravity")
    with patch.object(tr, "latest_model", return_value=""):
        await _stop_with_model(client, "m5", "gemini-3.8-pro")
    assert bridge_server.sessions.get_session("m5").model == "gemini-3.8-pro"


@pytest.mark.asyncio
async def test_a_transcript_outranks_what_the_event_says(client: httpx.AsyncClient) -> None:
    await _register_via_api(client, "m6", model="claude-opus-5")
    with patch.object(tr, "latest_model", return_value="claude-opus-5-5"):
        await _stop_with_model(client, "m6", "claude-opus-5")
    assert bridge_server.sessions.get_session("m6").model == "claude-opus-5-5"


@pytest.mark.asyncio
async def test_a_malformed_event_model_changes_nothing(client: httpx.AsyncClient) -> None:
    await _register_via_api(client, "m7", model="claude-opus-5-5")
    with patch.object(tr, "latest_model", return_value=""):
        await _stop_with_model(client, "m7", {"id": "evil"})
    assert bridge_server.sessions.get_session("m7").model == "claude-opus-5-5"

