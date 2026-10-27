"""Follow mode's bridge half: which file each agent is editing.

The Reader's Follow mode tracks what an agent edits, so the wearer watches the
code change as it happens. The bridge already knows where each agent writes its
transcript -- the terminal mirror reads it -- and the transcript records every
edit, with the file and the lines it changed:

- **Claude Code** writes each tool's result on the user turn after the call, as
  ``toolUseResult``. An Edit's carries ``filePath`` and a ``structuredPatch`` of
  hunks; a Write that created a file carries its ``content``.
- **Codex** writes an ``item_completed`` event holding a ``FileChange`` item for
  every patch it applies: each path, and the patch as a unified diff (a new
  file's ``content`` instead). An edit made with a shell command -- ``cat >``,
  ``sed -i`` -- is not a file change to Codex, so it is not followed.

Anything else is refused by name (:data:`FOLLOWABLE`) until its transcript's
edits have been read. Antigravity documents its step format, but no transcript
on the machine this was built on holds an edit to check a parser against.

The transcript is read under ``transcript_render``'s posture: found by a
UUID-shaped session id, opened by realpath under the assistant's own root with
``O_NOFOLLOW`` and the size cap. What it names is then held to the Reader's: an
edit counts only when its file resolves inside the session's project, and a
file the Reader never serves is reported by name, as ``withheld``, and never
shown.

The newest edit is found once, reading back from the end, and after that only
what the agent appends is read: while a transcript has not grown, a poll costs
one ``fstat``.
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
import time
from dataclasses import dataclass

from bridge import reader, transcript_render

# Whose edits can be followed: the assistants whose transcripts record them in a
# shape this module reads.
FOLLOWABLE = frozenset({"claude", "codex"})

# An edit touching more places than this is shown by its first ones.
MAX_RANGES = 20

# Reading back for the newest edit the first time a transcript is seen.
_SCAN_CHUNK = 256 * 1024
_SCAN_MAX_BYTES = 8 * 1024 * 1024
# More appended than this since the last read (a bridge asleep for an hour) is
# read back from the end instead, under the same budget.
_TAIL_MAX_BYTES = 8 * 1024 * 1024

# A session whose transcript was not found is looked for again after this: a new
# session has none until its first prompt.
_MISS_TTL_S = 10.0
_CACHE_MAX = 64

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CODEX_SESSION_RE = re.compile(
    r"codex-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def followable(assistant: str | None) -> bool:
    return (assistant or "claude").lower() in FOLLOWABLE


# Why any other assistant's edits cannot be followed, as the error says it.
NOT_FOLLOWABLE = "following works with Claude Code and Codex so far"


# ---------------------------------------------------------------------------
# Edits, as a transcript records them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edit:
    """One edit: the file as the transcript names it, and where it changed.

    ``ranges`` are 1-based, inclusive, in the file as it is after the edit --
    where the added lines are, or where deleted ones were.
    """

    path: str
    ranges: tuple[tuple[int, int], ...]
    verb: str  # "edited" | "created"
    id: str
    at: str  # the transcript's ISO timestamp; "" when it has none


def hunk_span(new_start: int, new_lines: int, lines: list) -> tuple[int, int] | None:
    """Where one hunk changed the file: its added lines, or the deletion point.

    Context lines are not the change, so they are walked over, not included.
    A hunk that only deletes is the line just above where its lines were.
    """
    n = new_start
    first = last = None
    deleted_at = None
    for line in lines:
        if not isinstance(line, str) or not line:
            continue
        op = line[0]
        if op == "+":
            if first is None:
                first = n
            last = n
            n += 1
        elif op == " ":
            n += 1
        elif op == "-" and deleted_at is None:
            deleted_at = n
        # "\ No newline at end of file" is not a line.
    if first is not None:
        return first, last
    if deleted_at is None:
        return None
    # Without context, a hunk's zero-length start already names the line above.
    at = new_start if new_lines == 0 and deleted_at == new_start else deleted_at - 1
    return max(at, 1), max(at, 1)


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _created(path: str, content: object, edit_id: str, at: str) -> Edit:
    n = _line_count(content) if isinstance(content, str) else 0
    return Edit(path, ((1, n),) if n else (), "created", edit_id, at)


def _claude_edit(raw: bytes) -> Edit | None:
    """A Claude Code transcript line, when it is an edit's result."""
    # Most lines are not edits: never parse what cannot be one.
    if b'"toolUseResult"' not in raw or b'"filePath"' not in raw:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    result = entry.get("toolUseResult")
    # A failed tool's result is its error message, a string.
    if not isinstance(result, dict):
        return None
    path = result.get("filePath")
    if not isinstance(path, str) or not path:
        return None
    at = str(entry.get("timestamp") or "")
    edit_id = str(entry.get("uuid") or f"{at}:{path}")
    if result.get("type") == "create":
        return _created(path, result.get("content"), edit_id, at)
    patch = result.get("structuredPatch")
    if not isinstance(patch, list):
        return None
    ranges = []
    for hunk in patch:
        if not isinstance(hunk, dict):
            continue
        start, count, lines = hunk.get("newStart"), hunk.get("newLines"), hunk.get("lines")
        if not isinstance(start, int) or not isinstance(lines, list):
            continue
        span = hunk_span(start, count if isinstance(count, int) else 1, lines)
        if span:
            ranges.append(span)
    # A write that changed nothing is not an edit to follow.
    return Edit(path, tuple(ranges), "edited", edit_id, at) if ranges else None


def diff_ranges(text: object) -> list[tuple[int, int]]:
    """Where a unified diff's hunks changed the new file, in order."""
    if not isinstance(text, str):
        return []
    spans: list[tuple[int, int]] = []
    header: tuple[int, int] | None = None
    lines: list[str] = []

    def flush() -> None:
        if header is not None:
            span = hunk_span(header[0], header[1], lines)
            if span:
                spans.append(span)

    for line in text.split("\n"):
        match = _HUNK_RE.match(line)
        if match:
            flush()
            header = (int(match.group(3)), int(match.group(4)) if match.group(4) is not None else 1)
            lines = []
        elif header is not None and line[:1] in (" ", "+", "-"):
            lines.append(line)
    flush()
    return spans


def _codex_edit(raw: bytes) -> Edit | None:
    """A Codex rollout line, when it is a patch applied to files."""
    if b'"FileChange"' not in raw:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "FileChange":
        return None
    # A patch that failed changed nothing.
    if item.get("status") != "completed":
        return None
    changes = item.get("changes")
    if not isinstance(changes, dict):
        return None
    at = str(entry.get("timestamp") or "")
    base_id = str(item.get("id") or at)
    # A patch lists its files in the order it wrote them; the first is where
    # a reader would start. A deleted file is nowhere to look.
    for path, change in changes.items():
        if not isinstance(change, dict) or not isinstance(path, str) or not path:
            continue
        kind = change.get("type")
        if kind == "add":
            return _created(path, change.get("content"), f"{base_id}:{path}", at)
        if kind == "update":
            ranges = diff_ranges(change.get("unified_diff"))
            moved = change.get("move_path")
            target = moved if isinstance(moved, str) and moved else path
            if ranges:
                return Edit(target, tuple(ranges), "edited", f"{base_id}:{target}", at)
    return None


_PARSERS = {"claude": _claude_edit, "codex": _codex_edit}


def _read(parse, raw: bytes) -> Edit | None:
    """One line through [parse]: a line of a shape nobody foresaw is skipped,
    and never takes the focus down with it."""
    try:
        return parse(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Finding a session's transcript
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
# "assistant:session_id" -> (transcript realpath or None, when it was looked for)
_LOCATED: dict[str, tuple[str | None, float]] = {}


def _codex_rollout(session) -> str | None:
    """Codex's rollout for a session registered as ``codex-<thread id>``."""
    match = _CODEX_SESSION_RE.fullmatch(getattr(session, "session_id", "") or "")
    if not match:
        return None
    roots = transcript_render._allowed_transcript_roots("codex")
    if not roots:
        return None
    pattern = os.path.join(roots[0], "sessions", "*", "*", "*", f"rollout-*-{match.group(1)}.jsonl")
    for hit in sorted(glob.glob(pattern)):
        real = os.path.realpath(hit)
        if any(real.startswith(root + os.sep) for root in roots) and os.path.isfile(real):
            return real
    return None


def transcript_of(session) -> str | None:
    """Where [session]'s agent writes its transcript, when this can read it."""
    assistant = (getattr(session, "assistant", "claude") or "claude").lower()
    # By assistant too: the activity poll corrects a session's assistant when
    # the pane turns out to run another one, and each writes its own file.
    sid = f"{assistant}:{getattr(session, 'session_id', '') or ''}"
    now = time.monotonic()
    with _LOCK:
        cached = _LOCATED.get(sid)
    if cached is not None:
        path, when = cached
        if path is not None and os.path.isfile(path):
            return path
        if path is None and now - when < _MISS_TTL_S:
            return None
    if assistant == "claude":
        path = transcript_render.find_transcript_path(session)
    elif assistant == "codex":
        path = _codex_rollout(session)
    else:
        path = None
    with _LOCK:
        if sid not in _LOCATED and len(_LOCATED) >= _CACHE_MAX:
            _LOCATED.pop(next(iter(_LOCATED)), None)
        _LOCATED[sid] = (path, now)
    return path


# ---------------------------------------------------------------------------
# The newest edit, read incrementally
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tail:
    size: int  # bytes read, ending just after a newline
    edit: Edit | None


# (transcript realpath, project root) -> what has been read of it
_TAILS: dict[tuple[str, str], _Tail] = {}


def _scan_back(f, size: int, parse, keep) -> tuple[Edit | None, int]:
    """The newest kept edit, reading back from ``size``; and where complete lines end."""
    f.seek(max(0, size - _SCAN_CHUNK))
    last = f.read(size - max(0, size - _SCAN_CHUNK))
    newline = last.rfind(b"\n")
    # A last line still being written is read whole next time.
    complete = size - len(last) + newline + 1 if newline >= 0 else size - len(last)
    end, scanned, carry = complete, 0, b""
    while end > 0 and scanned < _SCAN_MAX_BYTES:
        start = max(0, end - _SCAN_CHUNK)
        f.seek(start)
        chunk = f.read(end - start)
        scanned += len(chunk)
        end = start
        lines = (chunk + carry).split(b"\n")
        # Unless this window reached the start, its first line began before it.
        carry = lines.pop(0) if start > 0 else b""
        for raw in reversed(lines):
            edit = _read(parse, raw)
            if edit is not None and keep(edit):
                return edit, complete
    return None, complete


def newest_edit(path: str, assistant: str, root: str, keep) -> Edit | None:
    """The newest edit in the transcript at [path] that [keep] accepts.

    Only what was appended since the last call is read. [keep] decides what
    counts (an edit inside the project); its answer is cached with the
    transcript and [root], so it must depend on nothing else.
    """
    parse = _PARSERS.get(assistant)
    if parse is None:
        return None
    key = (path, root)
    fd = transcript_render._open_transcript_fd(path, assistant)
    if fd is None:
        with _LOCK:
            _TAILS.pop(key, None)
        return None
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        with _LOCK:
            tail = _TAILS.get(key)
        if tail is not None and tail.size == size:
            return tail.edit
        if tail is not None and tail.size < size and size - tail.size <= _TAIL_MAX_BYTES:
            f.seek(tail.size)
            data = f.read(size - tail.size)
            newline = data.rfind(b"\n")
            if newline < 0:
                return tail.edit
            edit = tail.edit
            for raw in data[:newline].split(b"\n"):
                found = _read(parse, raw)
                if found is not None and keep(found):
                    edit = found
            fresh = _Tail(tail.size + newline + 1, edit)
        else:
            # First sight, or the file shrank (replaced), or a long absence.
            edit, complete = _scan_back(f, size, parse, keep)
            fresh = _Tail(complete, edit)
    with _LOCK:
        if key not in _TAILS and len(_TAILS) >= _CACHE_MAX:
            _TAILS.pop(next(iter(_TAILS)), None)
        _TAILS[key] = fresh
    return fresh.edit


# ---------------------------------------------------------------------------
# The focus, as the wire says it
# ---------------------------------------------------------------------------


def in_project(root: str, path: str) -> str | None:
    """[path] relative to [root] when it resolves inside it, else None."""
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > 4 * reader.MAX_PATH_CHARS:
        return None
    full = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(full)
    if not real.startswith(root + os.sep):
        return None
    return os.path.relpath(real, root)


def payload(session) -> dict | None:
    """What [session]'s agent last edited in its project, as ``agent_focus`` carries it.

    None when this assistant cannot be followed, its transcript is not found
    yet, or it has edited nothing inside its project. Never raises.
    """
    try:
        assistant = (getattr(session, "assistant", "claude") or "claude").lower()
        if assistant not in FOLLOWABLE:
            return None
        try:
            root = reader.project_root(getattr(session, "project_dir", "") or "")
        except reader.ReaderError:
            return None
        path = transcript_of(session)
        if not path:
            return None
        edit = newest_edit(path, assistant, root, lambda e: in_project(root, e.path) is not None)
        if edit is None:
            return None
        rel = in_project(root, edit.path)
        if rel is None:
            return None
        return {
            "path": rel,
            "ranges": [[a, b] for a, b in edit.ranges[:MAX_RANGES]],
            "verb": edit.verb,
            "id": edit.id,
            "at": edit.at,
            # Named, never shown: the Reader refuses the file itself.
            "withheld": reader.refusal(rel) is not None,
        }
    except Exception:
        return None


def changed(session, focus: dict | None) -> dict | None:
    """The ``agent_focus`` message for [focus] when it is news; None when it is not.

    The first edit seen after the bridge starts is news too: a client that
    connected before it has nothing yet.
    """
    if focus is None or focus.get("id") == getattr(session, "_last_focus_id", None):
        return None
    session._last_focus_id = focus.get("id")
    return {
        "type": "agent_focus",
        "session_id": session.session_id,
        "project": session.project,
        "focus": focus,
    }


def reset() -> None:
    """Forget every transcript read (tests)."""
    with _LOCK:
        _LOCATED.clear()
        _TAILS.clear()
