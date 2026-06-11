"""Tests for bridge/transcript_render.py — rendering an assistant transcript
JSONL into terminal display lines, plus path acquisition and the mtime cache.

These are sync unit tests (no event loop). Fixtures write small JSONL files into
``tmp_path`` and monkeypatch ``_allowed_transcript_roots`` so the path-validation
accepts the temp dir instead of the real ``~/.claude`` roots.
"""

import json
import os
import types

import pytest

from bridge import transcript_render as tr


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------

def _write_jsonl(path, records):
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return str(path)


@pytest.fixture
def allow_tmp(tmp_path, monkeypatch):
    """Make ``tmp_path`` an allowed transcript root for all assistants."""
    root = os.path.realpath(str(tmp_path))
    monkeypatch.setattr(tr, "_allowed_transcript_roots", lambda assistant="claude": [root])
    # Caches are module-global; keep tests independent.
    tr._RENDER_CACHE.clear()
    return root


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _user_blocks(blocks):
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def _assistant(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


# ---------------------------------------------------------------------------
# rendering: user rows
# ---------------------------------------------------------------------------

def test_render_user_string_and_skips_system_injection(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _user("hello there"),
        _user("<system-reminder>ignore me</system-reminder>"),
    ])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert lines == ["▶ You: hello there"]


def test_render_user_blocks(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _user_blocks([{"type": "text", "text": "from a block"}]),
    ])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert lines == ["▶ You: from a block"]


def test_render_user_tool_result_only_is_skipped(tmp_path, allow_tmp):
    # A user row carrying only a tool_result (no text) is tool output echo —
    # we don't render it as a "You:" turn.
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _user_blocks([{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
    ])
    assert tr.render_transcript_lines(p, assistant="claude") == []


# ---------------------------------------------------------------------------
# rendering: assistant rows
# ---------------------------------------------------------------------------

def test_render_assistant_text_thinking_tooluse(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([
            {"type": "thinking", "thinking": "long internal reasoning\nsecond line"},
            {"type": "text", "text": "Here is the answer."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b/foo.py"}},
        ]),
    ])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert lines[0].startswith("  ✶ (thinking) long internal reasoning")
    assert lines[0].endswith("…")  # folded to one line
    assert "● Claude: Here is the answer." in lines
    assert "  ⏺ Edit foo.py" in lines


def test_assistant_plain_string_content(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        {"type": "assistant", "message": {"role": "assistant", "content": "just a string"}},
    ])
    assert tr.render_transcript_lines(p, assistant="claude") == ["● Claude: just a string"]


def test_multiline_message_continuation_indent(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "line one\nline two"}]),
    ])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert lines == ["● Claude: line one", "    line two"]


# ---------------------------------------------------------------------------
# tool_use formatting
# ---------------------------------------------------------------------------

def test_tool_use_formatting():
    assert tr._format_tool_use({"name": "Edit", "input": {"file_path": "/x/y/z.py"}}) == "Edit z.py"
    assert tr._format_tool_use({"name": "Read", "input": {"file_path": "/x/README.md"}}) == "Read README.md"
    assert tr._format_tool_use({"name": "Grep", "input": {"pattern": "needle"}}) == "Grep 'needle'"
    assert tr._format_tool_use(
        {"name": "Agent", "input": {"subagent_type": "Explore", "description": "d"}}
    ) == "Agent: Explore"
    bash = tr._format_tool_use({"name": "Bash", "input": {"command": "npm test"}})
    assert bash == "Bash: npm test"


def test_bash_command_truncated():
    long_cmd = "echo " + ("a" * 200)
    out = tr._format_tool_use({"name": "Bash", "input": {"command": long_cmd}})
    assert out.startswith("Bash: ")
    assert len(out) <= len("Bash: ") + 60


# ---------------------------------------------------------------------------
# secret redaction
# ---------------------------------------------------------------------------

def test_secret_redaction_in_text(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "your key is sk-ant-abc123def456"}]),
    ])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert any("[redacted" in ln for ln in lines)
    assert not any("sk-ant-abc123" in ln for ln in lines)


def test_secret_redaction_in_bash_arg(tmp_path, allow_tmp):
    # A secret in a Bash command must not survive inside the 60-char truncation.
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "export API_KEY=supersecretvalue && run"}},
        ]),
    ])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert not any("supersecretvalue" in ln for ln in lines)
    assert any("redacted" in ln for ln in lines)


# ---------------------------------------------------------------------------
# record-type / sidechain filtering
# ---------------------------------------------------------------------------

def test_skipped_record_types(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        {"type": "system", "message": {"role": "system", "content": "x"}},
        {"type": "attachment", "foo": 1},
        {"type": "mode", "mode": "plan"},
        {"type": "ai-title", "title": "t"},
        {"type": "agent-name", "name": "n"},
        {"type": "file-history-snapshot"},
        {"type": "queue-operation"},
    ])
    assert tr.render_transcript_lines(p, assistant="claude") == []


def test_sidechain_rows_skipped(tmp_path, allow_tmp):
    p = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "main turn"}]),
        {"type": "assistant", "isSidechain": True,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "sub-agent prose"}]}},
    ])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert lines == ["● Claude: main turn"]


def test_garbled_lines_are_skipped(tmp_path, allow_tmp):
    path = tmp_path / "t.jsonl"
    with open(path, "w") as f:
        f.write("not json at all\n")
        f.write(json.dumps(_user("real")) + "\n")
        f.write("{ broken json\n")
    lines = tr.render_transcript_lines(str(path), assistant="claude")
    assert lines == ["▶ You: real"]


# ---------------------------------------------------------------------------
# path acquisition + safety
# ---------------------------------------------------------------------------

def test_find_transcript_path_globs_by_session_id(tmp_path, monkeypatch):
    root = os.path.realpath(str(tmp_path))
    monkeypatch.setattr(tr, "_allowed_transcript_roots", lambda assistant="claude": [root])
    sid = "5d2c1f42-7def-43ef-bc86-b02ade04a7cf"
    proj = tmp_path / "projects" / "-Users-bone-dev-web-apps-voxherd"
    proj.mkdir(parents=True)
    target = _write_jsonl(proj / f"{sid}.jsonl", [_user("hi")])
    session = types.SimpleNamespace(assistant="claude", session_id=sid)
    assert tr.find_transcript_path(session) == os.path.realpath(target)


def test_find_transcript_path_missing_returns_none(tmp_path, monkeypatch):
    root = os.path.realpath(str(tmp_path))
    monkeypatch.setattr(tr, "_allowed_transcript_roots", lambda assistant="claude": [root])
    session = types.SimpleNamespace(assistant="claude",
                                    session_id="00000000-0000-0000-0000-000000000000")
    assert tr.find_transcript_path(session) is None


def test_session_id_traversal_rejected(tmp_path, monkeypatch):
    root = os.path.realpath(str(tmp_path))
    monkeypatch.setattr(tr, "_allowed_transcript_roots", lambda assistant="claude": [root])
    for bad in ("../../etc/passwd", "*", "..", "a/b", ""):
        session = types.SimpleNamespace(assistant="claude", session_id=bad)
        assert tr.find_transcript_path(session) is None


def test_render_missing_file_returns_empty(allow_tmp):
    assert tr.render_transcript_lines("/no/such/file.jsonl", assistant="claude") == []


def test_symlink_outside_root_rejected(tmp_path, monkeypatch):
    # A transcript symlink whose target lives outside the allowed roots must be
    # rejected (realpath-under-root check; O_NOFOLLOW backstop).
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write_jsonl(outside / "real.jsonl", [_user("leak")])
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    link = allowed / "link.jsonl"
    os.symlink(secret, link)
    monkeypatch.setattr(
        tr, "_allowed_transcript_roots",
        lambda assistant="claude": [os.path.realpath(str(allowed))],
    )
    assert tr.render_transcript_lines(str(link), assistant="claude") == []


def test_oversize_file_rejected(tmp_path, allow_tmp, monkeypatch):
    p = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
    monkeypatch.setattr(tr, "_MAX_TRANSCRIPT_FILE_BYTES", 1)  # any real file exceeds 1 byte
    assert tr.render_transcript_lines(p, assistant="claude") == []


# ---------------------------------------------------------------------------
# tail-cap
# ---------------------------------------------------------------------------

def test_max_lines_tail_cap_keeps_newest(tmp_path, allow_tmp):
    records = [_assistant([{"type": "text", "text": f"msg {i}"}]) for i in range(100)]
    p = _write_jsonl(tmp_path / "t.jsonl", records)
    lines = tr.render_transcript_lines(p, assistant="claude", max_lines=10)
    assert lines[0] == tr._TRUNCATION_MARKER
    assert len(lines) == 11
    assert lines[-1] == "● Claude: msg 99"  # newest kept


def test_per_line_char_cap(tmp_path, allow_tmp):
    # A single source line of ~30k chars (pasted blob) must be truncated so it
    # can't bloat the WS payload or render as a monster row on the phone.
    blob = "x" * 30_000
    p = _write_jsonl(tmp_path / "t.jsonl", [_assistant([{"type": "text", "text": blob}])])
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert all(len(ln) <= tr._MAX_LINE_CHARS + 2 for ln in lines)  # +2 for " …"
    assert any(ln.endswith("…") for ln in lines)


def test_total_char_budget_keeps_newest(tmp_path, allow_tmp, monkeypatch):
    # Total rendered chars must stay under _MAX_TOTAL_CHARS (bounds the WS
    # message under the iOS 1 MiB receive cap), keeping the NEWEST content.
    monkeypatch.setattr(tr, "_MAX_TOTAL_CHARS", 500)
    records = [_assistant([{"type": "text", "text": f"message number {i} " + "y" * 40}])
               for i in range(50)]
    p = _write_jsonl(tmp_path / "t.jsonl", records)
    lines = tr.render_transcript_lines(p, assistant="claude")
    assert lines[0] == tr._TRUNCATION_MARKER
    assert sum(len(ln) for ln in lines) <= 500 + len(tr._TRUNCATION_MARKER)
    assert "message number 49" in lines[-1]  # newest survives


# ---------------------------------------------------------------------------
# mtime cache
# ---------------------------------------------------------------------------

def test_mtime_cache_hit_and_invalidate(tmp_path, allow_tmp, monkeypatch):
    p = _write_jsonl(tmp_path / "t.jsonl", [_user("one")])

    calls = {"n": 0}
    real = tr.render_transcript_lines

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(tr, "render_transcript_lines", counting)

    a = tr.render_transcript_cached(p, assistant="claude")
    b = tr.render_transcript_cached(p, assistant="claude")
    assert a == b == ["▶ You: one"]
    assert calls["n"] == 1  # second call served from cache

    # Bump mtime → cache invalidates → re-parse.
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    tr.render_transcript_cached(p, assistant="claude")
    assert calls["n"] == 2
