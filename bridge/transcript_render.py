"""Render an assistant transcript JSONL into terminal-style display lines.

Why this exists: Claude Code / Codex render their TUI in the terminal's
*alternate screen* (``alternate_on=1``), where tmux keeps **zero** scrollback
(``history_size=0``). The phone's terminal mirror is fed by ``tmux capture-pane``,
so on those panes it can only ever show the single visible screen — there is no
history for tmux to hand back. The full conversation, however, is written to
disk by the assistant as a JSONL transcript. This module reads that file and
renders the conversation into a flat list of display lines that the bridge can
splice in front of the live tmux tail (see ``ws_handler._terminal_poll_loop``),
giving the phone deep scroll-back through the real chat log.

Pure + synchronous + stdlib-only. Every public function is defensive: it never
raises and returns ``[]`` / ``None`` on any error, so a missing/oversize/garbled
transcript silently falls back to the plain tmux mirror — no regression.

The security posture (allowed-root validation, ``O_NOFOLLOW`` open, 50MB cap,
secret redaction) is ported verbatim from ``hooks/on-stop.py`` — we do NOT import
the hook, which has import-time side effects (reads stdin, sets env, makes log
dirs).
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import threading

_IS_WINDOWS = sys.platform == "win32"

# --- limits -----------------------------------------------------------------
_MAX_TRANSCRIPT_FILE_BYTES = 50 * 1024 * 1024  # hard reject (matches on-stop.py)
_DEFAULT_MAX_LINES = 2000  # display-line cap (keep the newest)
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # stop reading the file after this many bytes
# Per display-line char cap. Real transcripts contain single source lines up to
# ~29k chars (pasted blobs); uncut they bloat the WS payload and render as
# monster wrapped rows on the phone. 2000 chars ≈ 25 wrapped terminal rows —
# plenty for context, safe for SwiftUI.
_MAX_LINE_CHARS = 2000
# Total char budget across all rendered lines (newest kept). The iOS
# URLSessionWebSocketTask receive cap is 1 MiB per message; this keeps the
# whole terminal_content JSON comfortably under it even with JSON escaping
# overhead, so one pathological transcript can't wedge the phone into a
# receive-fail loop.
_MAX_TOTAL_CHARS = 600_000

# --- display prefixes -------------------------------------------------------
_PREFIX_USER = "▶ You: "  # ▶
_PREFIX_ASST = "● Claude: "  # ●
_PREFIX_THINK = "  ✶ (thinking) "  # ✶
_PREFIX_TOOL = "  ⏺ "  # ⏺
_CONT_INDENT = "    "  # continuation rows for a wrapped multi-line message
_TRUNCATION_MARKER = "… (earlier history truncated) …"

# Top-level transcript record types worth rendering. Everything else
# (system / attachment / file-history-snapshot / mode / permission-mode /
# last-prompt / ai-title / queue-operation / agent-name / ...) is metadata noise.
_RENDERABLE_TYPES = frozenset({"user", "assistant"})

# A session_id must look like an assistant session UUID before we glob with it,
# so a hostile value can't smuggle in wildcards or "../" path segments.
_UUID_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,63}")


# ---------------------------------------------------------------------------
# Secret redaction (ported from hooks/on-stop.py:279-303)
# ---------------------------------------------------------------------------

# Matched case-INSENSITIVELY (see _contains_secret). The list used to be
# compared with a plain `in`, which meant only SHOUTING env-var spellings were
# caught: `password = "..."`, `api_key = "..."`, `"client_secret": "..."` and
# `auth_token: ...` all went through untouched. This function is the only
# content-level gate on three egress paths -- the summary sent to a third-party
# model, file content served to the phone and lens, and the transcript streamed
# over the WebSocket -- so a miss here is a credential leaving the machine.
# Shapes distinctive enough to redact on sight: a provider prefix or a key
# block is never anything else. Matched as a case-insensitive substring.
_SECRET_SHAPES = [
    "ghp_", "gho_", "ghu_", "ghs_", "github_pat_",
    "xoxb-", "xoxp-", "xapp-",
    "akia",             # AWS access key id
    "aiza",             # Google API key
    "gocspx-",          # Google OAuth client secret
    "glpat-",           # GitLab personal access token
    "npm_",             # npm automation token
    "sk_live_", "sk_test_", "rk_live_",   # Stripe
    "-----begin",       # PEM block
    "eyj",              # JWT (base64 of '{"')
    "service_account",  # GCP service-account JSON
    "ls0tls1",          # base64 of "-----BEGIN": a PEM block inlined in YAML/JSON
]

# Generic *names* need the shape of an assignment to count, because as bare
# substrings they are ordinary English: "auth" is in "authoritative", "token" is
# in "token count", "secret" is in "secretary". Matching them loosely redacted
# 2.1% of this repo's own source -- mostly comments -- which is both useless and
# teaches the reader to ignore the redaction notice. Requiring `name: value` or
# `name = value` keeps `password = "x"`, `"client_secret": "..."` and
# `auth_token: abc` while leaving prose alone.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    # No leading \b: the name is usually prefixed (db_password, MY_API_KEY,
    # gitlab_token), and "_" is a word character so \b would not match inside
    # them. The trailing \b plus the required separator is what keeps this from
    # firing on prose.
    (?: api[_-]?key | apikey
        | secret | secrets
        | token | tokens
        | password | passwd | passphrase
        | credential | credentials
        | private[_-]?key | privatekey
        | client[_-]?secret | refresh[_-]?token | access[_-]?key
        | session[_-]?key | auth[_-]?token | authorization
        | key[_-]?data            # kubeconfig client-key-data
      )
    [A-Za-z0-9_-]*                # ...and any suffix: SECRET_VALUE, TOKEN_ID,
                                  # PASSWORD_HASH, secret_key_base. A trailing
                                  # \b required the word to be the LAST
                                  # component before the separator, which let
                                  # every prefixed spelling through -- 381 lines
                                  # in this repo that the previous list caught.
    ["'\s]* [:=] (?!=)            # ":" or "=", not "=="
    \s* \S                        # ...with something after it
    """
)

# OpenAI/Anthropic keys. A length requirement rather than a bare "sk-" prefix,
# which matched any word ending in "sk" before a hyphen -- "task-file",
# "disk-usage", "risk-free".
_SK_KEY_RE = re.compile(r"(?i)\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}")

# An Authorization header or a bearer token, wherever it appears.
_BEARER_RE = re.compile(r"(?i)\b(?:bearer|authorization\s*:)\s*\S")

# Only a URL carrying credentials in its userinfo part, not every URL.
_URL_CREDENTIALS_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


def _contains_secret(text: str) -> bool:
    """Return True if text appears to contain a secret/credential."""
    low = text.lower()
    if any(shape in low for shape in _SECRET_SHAPES):
        return True
    if _SK_KEY_RE.search(text) or _SECRET_ASSIGNMENT_RE.search(text):
        return True
    if _BEARER_RE.search(text):
        return True
    return bool(_URL_CREDENTIALS_RE.search(text))


def _redact_secrets(text: str) -> str:
    """Replace lines containing secrets with a redaction notice."""
    lines = text.split("\n")
    result = []
    for line in lines:
        if _contains_secret(line):
            result.append("[redacted — contains credential]")
        else:
            result.append(line)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Content extraction (ported from hooks/on-stop.py:231-244)
# ---------------------------------------------------------------------------

_SYSTEM_INJECTION_PREFIXES = (
    "<local-command-caveat>", "<task-notification>", "<system-reminder>",
)


def _extract_user_text(content) -> str:
    """Extract user text from content (handles both string and list formats).

    Returns "" for system-injected content (reminders, task notifications,
    local-command caveats) so machine chatter never reaches the display.
    """
    if isinstance(content, str) and content.strip():
        text = content.strip()
        if text.startswith(_SYSTEM_INJECTION_PREFIXES):
            return ""
        return text
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and not text.startswith(_SYSTEM_INJECTION_PREFIXES):
                    return text
    return ""


# ---------------------------------------------------------------------------
# Allowed transcript roots (ported from hooks/on-stop.py:247-274; assistant
# is now a parameter instead of a module global)
# ---------------------------------------------------------------------------

def _allowed_transcript_roots(assistant: str) -> list[str]:
    """Return acceptable transcript root dirs for known assistants.

    Assistant CLIs store transcripts under ``~/.<name>/``. On Windows this
    expands to ``%USERPROFILE%\\.claude`` etc. plus APPDATA fallbacks.
    """
    preferred = {
        "claude": "~/.claude",
        "gemini": "~/.gemini",
        "codex": "~/.codex",
        "grok": "~/.grok",
        "composer": "~/.grok",
    }
    roots: list[str] = []
    primary = preferred.get(assistant)
    if primary:
        roots.append(os.path.realpath(os.path.expanduser(primary)))
    for fallback in ("~/.claude", "~/.gemini", "~/.codex", "~/.grok"):
        candidate = os.path.realpath(os.path.expanduser(fallback))
        if candidate not in roots:
            roots.append(candidate)
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            for name in ("claude", "gemini", "codex", "grok"):
                candidate = os.path.realpath(os.path.join(appdata, name))
                if candidate not in roots:
                    roots.append(candidate)
    return roots


# ---------------------------------------------------------------------------
# Path acquisition
# ---------------------------------------------------------------------------

def find_transcript_path(session) -> str | None:
    """Locate a session's transcript JSONL by globbing on its session_id.

    The session_id is a globally-unique UUID equal to the transcript filename,
    so we can glob ``~/.{assistant}/projects/*/{session_id}.jsonl`` and skip all
    cwd→dirname encoding fragility. Returns an absolute realpath that lives under
    an allowed transcript root, or ``None``. Never raises.
    """
    try:
        assistant = (getattr(session, "assistant", "claude") or "claude").lower()
        sid = getattr(session, "session_id", "") or ""
        if not _UUID_RE.fullmatch(sid):
            return None
        roots = _allowed_transcript_roots(assistant)
        for root in roots:
            pattern = os.path.join(root, "projects", "*", sid + ".jsonl")
            for hit in glob.glob(pattern):
                rp = os.path.realpath(hit)
                if any(rp.startswith(r + os.sep) for r in roots) and os.path.isfile(rp):
                    return rp
        return None
    except Exception:
        return None


def _open_transcript_fd(path: str, assistant: str) -> int | None:
    """TOCTOU-safe open: realpath → allowed-root check → O_NOFOLLOW → 50MB cap.

    Mirrors hooks/on-stop.py:316-346. Returns an open fd (caller closes it) or
    ``None``.
    """
    try:
        real_path = os.path.realpath(path)
        allowed_roots = _allowed_transcript_roots(assistant)
        if not any(real_path.startswith(root + os.sep) for root in allowed_roots):
            return None
        open_flags = os.O_RDONLY
        if not _IS_WINDOWS:
            open_flags |= os.O_NOFOLLOW
        fd = os.open(real_path, open_flags)
    except Exception:
        return None

    try:
        if os.fstat(fd).st_size > _MAX_TRANSCRIPT_FILE_BYTES:
            os.close(fd)
            return None
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

# Read backwards in windows of this size, and give up after this many bytes.
# The newest assistant turn is almost always in the first window; the cap only
# matters behind a run of huge tool results.
_MODEL_SCAN_CHUNK = 256 * 1024
_MODEL_SCAN_MAX_BYTES = 4 * 1024 * 1024

# Claude Code writes this on entries it made up itself (an API error, an
# interrupted turn). It names no model.
_SYNTHETIC_MODEL = "<synthetic>"


def latest_model(session) -> str:
    """The model on the newest main-thread assistant turn in the transcript.

    From the end, because a session's model can change part way (``/model``)
    and only the latest turn says what it runs now. Sub-agent turns are
    skipped -- an Explore agent on Haiku does not change the session's model --
    and so are synthetic entries. ``""`` when there is no transcript, no
    assistant turn yet, or none within the scan budget. Never raises.

    Only finds transcripts :func:`find_transcript_path` can, which today means
    Claude Code's.
    """
    try:
        path = find_transcript_path(session)
        if not path:
            return ""
        assistant = (getattr(session, "assistant", "claude") or "claude").lower()
        fd = _open_transcript_fd(path, assistant)
        if fd is None:
            return ""
        with os.fdopen(fd, "rb") as f:
            return _scan_for_model(f)
    except Exception:
        return ""


def _scan_for_model(f) -> str:
    end = os.fstat(f.fileno()).st_size
    scanned = 0
    carry = b""
    while end > 0 and scanned < _MODEL_SCAN_MAX_BYTES:
        start = max(0, end - _MODEL_SCAN_CHUNK)
        f.seek(start)
        chunk = f.read(end - start)
        scanned += len(chunk)
        end = start
        lines = (chunk + carry).split(b"\n")
        # Unless this window reached the start of the file, its first line
        # began in the window before it: finish it next time round.
        carry = lines.pop(0) if start > 0 else b""
        for raw in reversed(lines):
            model = _model_of(raw)
            if model:
                return model
    return ""


def _model_of(raw: bytes) -> str:
    # Most lines are not assistant turns; don't parse what cannot match.
    if b'"assistant"' not in raw or b'"model"' not in raw:
        return ""
    try:
        entry = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return ""
    if entry.get("isSidechain"):
        return ""
    message = entry.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    if not isinstance(model, str) or not model or model == _SYNTHETIC_MODEL:
        return ""
    return model


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _format_tool_use(block: dict) -> str:
    """One-line summary of a tool_use block, e.g. ``Edit foo.py`` or ``Bash: …``.

    Bash commands are redacted BEFORE truncation so a credential can't survive
    inside the truncation window.
    """
    name = block.get("name", "") or "tool"
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}

    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        return f"{name} {os.path.basename(fp)}".strip()
    if name == "Read":
        return f"Read {os.path.basename(inp.get('file_path', ''))}".strip()
    if name == "Bash":
        cmd = _redact_secrets(str(inp.get("command", ""))).split("\n")[0]
        return f"Bash: {cmd[:60]}".rstrip()
    if name == "Grep":
        return f"Grep '{str(inp.get('pattern', ''))[:40]}'"
    if name in ("Glob",):
        return f"Glob '{str(inp.get('pattern', ''))[:40]}'"
    if name in ("Agent", "Task"):
        hint = inp.get("subagent_type") or inp.get("description") or ""
        return f"Agent: {hint}".strip()
    if name == "Skill":
        return f"Skill {inp.get('skill', '')}".strip()
    return name


def _emit_text(out: list[str], prefix: str, text: str) -> None:
    """Append a (possibly multi-line) message to ``out`` as one row per source
    line: the first carries ``prefix``, continuation rows are indented."""
    parts = text.split("\n")
    first = True
    for part in parts:
        part = part.rstrip()
        if first:
            out.append(prefix + part)
            first = False
        else:
            out.append(_CONT_INDENT + part)
    # Drop a single trailing blank continuation row for tidiness.
    if len(out) > 1 and out[-1].strip() == "" and out[-1].startswith(_CONT_INDENT):
        out.pop()


def render_transcript_lines(
    path: str,
    *,
    assistant: str = "claude",
    max_lines: int = _DEFAULT_MAX_LINES,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> list[str]:
    """Render a transcript JSONL into display lines (oldest first).

    Secret-redacted, capped at ``max_lines`` keeping the newest. Returns ``[]``
    on any error (missing/oversize file, bad path, parse failure).
    """
    fd = _open_transcript_fd(path, assistant)
    if fd is None:
        return []

    out: list[str] = []
    try:
        with os.fdopen(fd, "r", errors="replace") as f:
            byte_budget = 0
            for raw in f:  # line-by-line: bounds memory on huge files
                byte_budget += len(raw)
                if byte_budget > max_bytes:
                    break
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") not in _RENDERABLE_TYPES:
                    continue
                if obj.get("isSidechain") is True:
                    # Sub-agent turns live in separate files today; skip
                    # defensively so a future inlining can't double-render them.
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                content = msg.get("content")

                if role == "user":
                    text = _extract_user_text(content)
                    if text:
                        out.append("")  # blank line before each user turn
                        _emit_text(out, _PREFIX_USER, text)
                elif role == "assistant":
                    if isinstance(content, str):
                        if content.strip():
                            _emit_text(out, _PREFIX_ASST, content.strip())
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "text":
                                t = (block.get("text") or "").strip()
                                if t:
                                    _emit_text(out, _PREFIX_ASST, t)
                            elif btype == "thinking":
                                t = (block.get("thinking") or "").strip()
                                if t:
                                    first = t.split("\n")[0]
                                    more = " …" if (len(t) > len(first) or len(first) > 120) else ""
                                    out.append(_PREFIX_THINK + first[:120] + more)
                            elif btype == "tool_use":
                                out.append(_PREFIX_TOOL + _format_tool_use(block))
    except Exception:
        return []

    if not out:
        return []

    # One global redaction pass (cheaper than per-line; same result).
    out = _redact_secrets("\n".join(out)).split("\n")

    # Drop a leading blank (the first user turn prepends one).
    while out and out[0] == "":
        out.pop(0)

    # Per-line cap: pasted blobs produce single source lines up to ~29k chars.
    out = [
        (ln[:_MAX_LINE_CHARS] + " …") if len(ln) > _MAX_LINE_CHARS else ln
        for ln in out
    ]

    if len(out) > max_lines:
        out = [_TRUNCATION_MARKER] + out[-max_lines:]

    # Total char budget (newest kept): bounds the WS payload so it can never
    # exceed the iOS 1 MiB receive cap regardless of line lengths.
    total = sum(len(ln) for ln in out)
    if total > _MAX_TOTAL_CHARS:
        kept: list[str] = []
        budget = _MAX_TOTAL_CHARS
        for ln in reversed(out):
            budget -= len(ln)
            if budget <= 0:
                break
            kept.append(ln)
        kept.reverse()
        out = [_TRUNCATION_MARKER] + kept
    return out


# ---------------------------------------------------------------------------
# mtime cache: re-parse only when the file actually changes (not at 2Hz)
# ---------------------------------------------------------------------------

# path -> (st_mtime_ns, st_size, tuple(lines))
_RENDER_CACHE: dict[str, tuple[int, int, tuple[str, ...]]] = {}
_RENDER_CACHE_LOCK = threading.Lock()
_RENDER_CACHE_MAX = 32


def render_transcript_cached(
    path: str,
    *,
    assistant: str = "claude",
    max_lines: int = _DEFAULT_MAX_LINES,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> list[str]:
    """Like ``render_transcript_lines`` but memoized on (mtime_ns, size).

    The terminal poll loop calls this ~2x/sec per subscriber; the ``os.stat`` is
    microseconds and the expensive parse only runs when the transcript grew
    (i.e. a turn was appended). Multiple subscribers to the same session share
    one parse. Returns ``[]`` on stat/render failure.
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []

    with _RENDER_CACHE_LOCK:
        cached = _RENDER_CACHE.get(path)
        if cached is not None and (cached[0], cached[1]) == key:
            return list(cached[2])

    lines = render_transcript_lines(
        path, assistant=assistant, max_lines=max_lines, max_bytes=max_bytes
    )

    with _RENDER_CACHE_LOCK:
        if path not in _RENDER_CACHE and len(_RENDER_CACHE) >= _RENDER_CACHE_MAX:
            # Evict an arbitrary oldest-ish entry (dicts preserve insert order).
            try:
                del _RENDER_CACHE[next(iter(_RENDER_CACHE))]
            except (StopIteration, KeyError):
                pass
        _RENDER_CACHE[path] = (key[0], key[1], tuple(lines))
    return lines
