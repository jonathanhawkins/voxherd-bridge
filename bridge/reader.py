"""The Reader's bridge half.

A registered project's files, highlighted; the regions a hand can point at;
the working tree's diff; and a pointed-at region turned into words the
assistant understands. The headset renders; everything that needs a grammar,
a repository or a filesystem is here.

**This reads arbitrary project files, which makes it the riskiest surface the
bridge has.** Its posture is ``transcript_render``'s applied to a project
directory, and its redaction *is* ``transcript_render``'s -- imported, not
copied, so the two cannot drift:

- the root is a live session's registered ``project_dir``, never a path from
  the request;
- a requested path is relative, resolved with ``realpath``, and must stay
  under the root, so a symlink out of the project is refused, not followed;
- the final open is ``O_NOFOLLOW | O_NONBLOCK`` and must find a regular file:
  a symlink swapped in after the check fails, and a FIFO cannot hang a request;
- sizes are capped;
- a line holding a secret is redacted, keeping its number so every line number
  stays true -- the wearer points at lines -- and a PEM block is redacted whole,
  because a key's body is lines no pattern recognises;
- files that are secrets by nature (``.env``, private keys, credential stores,
  anything under ``.git``) are refused outright.

Synchronous apart from ``git``, which runs with a timeout; the routes call
this from a thread.
"""

from __future__ import annotations

import importlib
import os
import re
import stat
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass

from bridge.env_utils import get_subprocess_env
from bridge.transcript_render import _contains_secret

# --- limits -----------------------------------------------------------------

MAX_FILE_BYTES = 2 * 1024 * 1024
"""Larger files are refused: this is for reading code, not logs or data."""

MAX_LINES = 20_000
"""Lines served from one file; past this it is truncated, and says so."""

MAX_PAGE_LINES = 4_000
"""Highlighted lines in one response. Ask for more with ``start``/``end``."""

MAX_LINE_CHARS = 2_000
"""A minified line is cut here; the rest is not code anyone reads in a headset."""

MAX_PATH_CHARS = 1_024

MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_DIFF_FILES = 200
MAX_UNTRACKED_BYTES = 256 * 1024
MAX_LISTED_FILES = 20_000
GIT_TIMEOUT_S = 10.0

REDACTED = "[redacted — contains credential]"


class ReaderError(Exception):
    """A request the Reader will not serve; ``status`` is its HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# What is never served
# ---------------------------------------------------------------------------

# Directories whose contents are credentials, or a repository's internals
# (a remote URL in .git/config can carry a token).
_REFUSED_DIRS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"})

# Key and credential material by extension.
_REFUSED_EXTS = frozenset({".pem", ".key", ".p12", ".pfx", ".p8", ".jks", ".keystore", ".ppk", ".kdbx"})

_REFUSED_NAMES = frozenset({
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd", ".envrc",
    "credentials", "credentials.json",
    # The bridge's own bearer token. Serving it hands out the credential that
    # guards every other endpoint, including dispatch -- and it was reachable,
    # as ~/.voxherd/auth_token, from any root at or above ~/.voxherd.
    "auth_token",
})
_SSH_KEY_RE = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(?!.*\.pub$)", re.IGNORECASE)
# .env, .env.local, prod.env -- but not the templates that exist to be read.
_ENV_RE = re.compile(r"^\.env(\..+)?$|^.+\.env$", re.IGNORECASE)
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")
# Secrets.xcconfig, secrets.yaml, secret.json -- config, not code named "secrets".
_SECRETS_CONFIG_RE = re.compile(
    r"^secrets?([._-][^/]*)?\.(xcconfig|json|ya?ml|toml|ini|env|properties|plist|cfg|conf|txt)$",
    re.IGNORECASE,
)
_SERVICE_ACCOUNT_RE = re.compile(r"(service[-_]?account|adminsdk).*\.json$", re.IGNORECASE)
# Agent and MCP configuration: a server entry carries live credentials in
# headers and env under names no redaction pattern knows (X-Api-Key, ...).
_REFUSED_ANYWHERE = frozenset({".mcp.json", "mcp.json", "mcp_config.json"})
_REFUSED_IN = {
    ".codex": frozenset({"config.toml", "auth.json"}),
    ".gemini": frozenset({"settings.json", "oauth_creds.json"}),
    ".claude": frozenset({"settings.local.json", ".credentials.json"}),
}


def refusal(rel_path: str) -> str | None:
    """Why ``rel_path`` is never served, or None. Judged on the resolved path."""
    parts = [p for p in re.split(r"[\\/]", rel_path) if p and p != "."]
    # Case-folded on every component, not just the basename: macOS (APFS/HFS+)
    # and Windows resolve ".GIT" to ".git", and realpath does not canonicalise
    # case, so a capitalised directory would otherwise carry the attacker's
    # spelling straight past these checks and open the real file.
    lows = [p.lower() for p in parts]
    for part in lows:
        if part in _REFUSED_DIRS:
            return f"nothing under {part} is served"
    name = parts[-1] if parts else ""
    low = lows[-1] if lows else ""
    if low in _REFUSED_ANYWHERE or any(low in _REFUSED_IN.get(part, ()) for part in lows[:-1]):
        return "agent configuration is never served"
    if low in _REFUSED_NAMES or _SSH_KEY_RE.match(name):
        return "credential files are never served"
    if os.path.splitext(low)[1] in _REFUSED_EXTS:
        return "key material is never served"
    if _ENV_RE.match(name) and not low.endswith(_TEMPLATE_SUFFIXES):
        return "environment files are never served"
    if _SECRETS_CONFIG_RE.match(name) or _SERVICE_ACCOUNT_RE.search(name):
        return "secrets files are never served"
    return None


# ---------------------------------------------------------------------------
# Paths and reading
# ---------------------------------------------------------------------------


def project_root(project_dir: str) -> str:
    """The real path of a registered project's directory."""
    if not project_dir:
        # realpath("") is the bridge's own working directory.
        raise ReaderError(404, "the session has no project directory")
    root = os.path.realpath(project_dir)
    if not os.path.isdir(root):
        raise ReaderError(404, "the project's directory is gone")
    return root


def resolve_path(root: str, rel: object) -> tuple[str, str]:
    """``rel`` under ``root``, as (real path, path relative to the root).

    The containment and refusal checks run on the *resolved* path, so a
    symlink inside the project that points out of it, or at a secret, is
    refused like the target itself would be.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ReaderError(400, "path is required")
    if len(rel) > MAX_PATH_CHARS or "\x00" in rel:
        raise ReaderError(400, "invalid path")
    rel = rel.strip()
    if os.path.isabs(rel) or rel.startswith("~"):
        raise ReaderError(400, "path must be relative to the project")
    real = os.path.realpath(os.path.join(root, rel))
    if not real.startswith(root + os.sep):
        raise ReaderError(403, "path is outside the project")
    rel_real = os.path.relpath(real, root)
    reason = refusal(rel_real) or _home_refusal(real)
    if reason:
        raise ReaderError(403, reason)
    return real, rel_real


def _home_refusal(real: str) -> str | None:
    """``refusal`` again, on the path as spelled from $HOME, or None.

    The root-relative spelling is what a caller asked for, but every rule that
    keys on a directory name stops applying once that directory is at or above
    the root -- a root of ``~/.ssh`` makes ``.ssh`` invisible to
    :func:`refusal`. Judging the same file a second time from the home
    directory down restores those rules whatever the root happens to be.
    ``_validate_project_dir`` already refuses such roots; this is the backstop
    for a root that reached the registry another way.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    if not real.startswith(home + os.sep):
        return None
    rel = os.path.relpath(real, home)
    # The same set validation refuses as a project ROOT, applied here to any
    # file beneath one. The two lists were separate and validation's was larger
    # by seven entries, so a directory judged too dangerous to be a root was
    # served freely one level down: `~/.claude/projects` as a root handed out
    # every session transcript for every project, sidestepping the Reader's
    # per-project containment entirely.
    from bridge.validation import _FORBIDDEN_HOME_DIRS

    first = rel.split(os.sep, 1)[0].lower()
    if first in _FORBIDDEN_HOME_DIRS and first not in _REFUSED_DIRS:
        return f"nothing under ~/{first} is served"
    return refusal(rel)


def _read_regular(real: str) -> tuple[bytes, os.stat_result]:
    """Read a regular file without following a final symlink or blocking."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(real, flags)
    except FileNotFoundError:
        raise ReaderError(404, "no such file") from None
    except OSError:
        # ELOOP: a symlink appeared after the path was resolved.
        raise ReaderError(403, "file could not be opened safely") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ReaderError(400, "not a regular file")
        if st.st_size > MAX_FILE_BYTES:
            raise ReaderError(413, f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_BYTES:  # grew while being read
            raise ReaderError(413, "file is too large")
        return data, st
    finally:
        os.close(fd)


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def split_lines(text: str) -> list[str]:
    """Lines as an editor numbers them: a trailing newline does not start one."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.removeprefix("﻿")
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def redact_lines(lines: list[str]) -> tuple[list[str], list[int]]:
    """Secret-bearing lines replaced, numbers kept; returns the 1-based numbers.

    A line is judged by ``transcript_render``'s own test. A PEM block goes
    whole, BEGIN to END -- or to the end of the file when it never ends --
    since only its first line carries anything a pattern can see.
    """
    out: list[str] = []
    redacted: list[int] = []
    in_block = False
    for number, line in enumerate(lines, 1):
        begin, end = line.rfind("-----BEGIN"), line.rfind("-----END")
        if in_block or begin >= 0:
            # Whichever marker comes last on the line says whether a block is open after it.
            if begin >= 0 or end >= 0:
                in_block = begin > end
            out.append(REDACTED)
            redacted.append(number)
        elif _contains_secret(line):
            out.append(REDACTED)
            redacted.append(number)
        else:
            out.append(line)
    return out, redacted


@dataclass
class _Loaded:
    rel: str
    lines: list[str]  # original, cut to MAX_LINES
    shown: list[str]  # redacted and length-capped: the only text that leaves
    redacted: list[int]
    truncated: bool
    size: int
    mtime: float


def load(root: str, rel: object) -> _Loaded:
    """Resolve, read and redact one project file."""
    real, rel_real = resolve_path(root, rel)
    data, st = _read_regular(real)
    if _is_binary(data):
        raise ReaderError(415, "binary file")
    lines = split_lines(data.decode("utf-8", errors="replace"))
    truncated = len(lines) > MAX_LINES
    lines = lines[:MAX_LINES]
    shown, redacted = redact_lines(lines)
    shown = [ln if len(ln) <= MAX_LINE_CHARS else ln[:MAX_LINE_CHARS] + " …" for ln in shown]
    return _Loaded(rel_real, lines, shown, redacted, truncated, st.st_size, st.st_mtime)


# ---------------------------------------------------------------------------
# Highlighting (Pygments, server-side: the headset needs no grammars)
# ---------------------------------------------------------------------------


def _lexer_for(path: str):
    from pygments.lexers import TextLexer, get_lexer_for_filename
    from pygments.util import ClassNotFound

    options = {"stripnl": False, "stripall": False, "ensurenl": True}
    try:
        return get_lexer_for_filename(os.path.basename(path), **options)
    except ClassNotFound:
        return TextLexer(**options)


def _short_class(ttype) -> str:
    """Pygments' short CSS class for a token type: "k", "s2", "nf", "c1"...

    A client themes by prefix (k keyword, s string, c comment, n name, m
    number, o operator, p punctuation). Whitespace and plain text are "".
    """
    from pygments.token import STANDARD_TYPES, Token

    while ttype not in STANDARD_TYPES and ttype is not Token:
        ttype = ttype.parent
    short = STANDARD_TYPES.get(ttype, "")
    return "" if short == "w" else short


def highlight(lines: list[str], path: str) -> tuple[str, list[list[list[str]]]]:
    """Each line as runs of ``[text, class]``, and the language's name.

    Lexed as one text, so a string or comment spanning lines is coloured
    right on every one of them.
    """
    lexer = _lexer_for(path)
    out: list[list[list[str]]] = [[]]
    for ttype, value in lexer.get_tokens("\n".join(lines)):
        cls = _short_class(ttype)
        for i, part in enumerate(value.split("\n")):
            if i:
                out.append([])
            if not part:
                continue
            row = out[-1]
            if row and row[-1][1] == cls:
                row[-1][0] += part
            else:
                row.append([part, cls])
    out = out[: len(lines)]
    out.extend([] for _ in range(len(lines) - len(out)))
    return lexer.name, out


_HIGHLIGHT_CACHE: OrderedDict[tuple, tuple[str, list]] = OrderedDict()
_HIGHLIGHT_CACHE_SIZE = 8
_HIGHLIGHT_LOCK = threading.Lock()


def _highlight_cached(loaded: _Loaded, key: tuple) -> tuple[str, list]:
    with _HIGHLIGHT_LOCK:
        hit = _HIGHLIGHT_CACHE.get(key)
        if hit is not None:
            _HIGHLIGHT_CACHE.move_to_end(key)
            return hit
    result = highlight(loaded.shown, loaded.rel)
    with _HIGHLIGHT_LOCK:
        _HIGHLIGHT_CACHE[key] = result
        while len(_HIGHLIGHT_CACHE) > _HIGHLIGHT_CACHE_SIZE:
            _HIGHLIGHT_CACHE.popitem(last=False)
    return result


def file_payload(root: str, rel: object, start: int | None = None, end: int | None = None) -> dict:
    """``GET /api/file``: one file's lines as highlighted runs.

    ``start``/``end`` (1-based, inclusive) page through a long file; the
    whole file is lexed either way, so a page starting inside a string is
    still coloured as one.
    """
    loaded = load(root, rel)
    total = len(loaded.shown)
    first = max(1, start or 1)
    last = min(total, end if end is not None else first + MAX_PAGE_LINES - 1)
    last = min(last, first + MAX_PAGE_LINES - 1)
    key = (root, loaded.rel, loaded.size, loaded.mtime, len(loaded.redacted))
    language, runs = _highlight_cached(loaded, key)
    page = runs[first - 1:last] if total else []
    return {
        "path": loaded.rel,
        "language": language,
        "line_count": total,
        "start": first if page else 0,
        "end": last if page else 0,
        "lines": page,
        "redacted": loaded.redacted,
        "truncated": loaded.truncated,
        "size": loaded.size,
        "mtime": loaded.mtime,
    }


# ---------------------------------------------------------------------------
# Regions: what a hand can point at
# ---------------------------------------------------------------------------


@dataclass
class Region:
    kind: str
    name: str
    start: int  # 1-based, inclusive
    end: int
    depth: int

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "start": self.start, "end": self.end, "depth": self.depth}


@dataclass(frozen=True)
class _Grammar:
    module: str
    function: str
    defs: dict  # node type -> kind
    wrappers: dict  # node type -> the field holding the real declaration
    containers: frozenset  # kinds whose bodies hold nested definitions


_JS_DEFS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "internal_module": "module",
    "module": "module",
    "lexical_declaration": "variable",
    "variable_declaration": "variable",
    "public_field_definition": "field",
}
_JS_WRAPPERS = {"export_statement": "declaration"}
_CLASSLIKE = frozenset({
    "class", "interface", "object", "struct", "enum", "extension", "actor",
    "protocol", "impl", "trait", "module", "record",
})
# A function in one of these is a method; in a module or namespace it is not.
_METHOD_HOSTS = _CLASSLIKE - {"module"}

_GRAMMARS: dict[str, _Grammar] = {
    "python": _Grammar(
        "tree_sitter_python", "language",
        {"function_definition": "function", "class_definition": "class"},
        {"decorated_definition": "definition"}, _CLASSLIKE,
    ),
    "javascript": _Grammar("tree_sitter_javascript", "language", _JS_DEFS, _JS_WRAPPERS, _CLASSLIKE),
    "typescript": _Grammar("tree_sitter_typescript", "language_typescript", _JS_DEFS, _JS_WRAPPERS, _CLASSLIKE),
    "tsx": _Grammar("tree_sitter_typescript", "language_tsx", _JS_DEFS, _JS_WRAPPERS, _CLASSLIKE),
    "kotlin": _Grammar(
        "tree_sitter_kotlin", "language",
        {
            "class_declaration": "class",
            "object_declaration": "object",
            "companion_object": "object",
            "function_declaration": "function",
            "secondary_constructor": "constructor",
            "type_alias": "type",
        },
        {}, _CLASSLIKE,
    ),
    "swift": _Grammar(
        "tree_sitter_swift", "language",
        {
            "class_declaration": "class",
            "protocol_declaration": "protocol",
            "function_declaration": "function",
            "protocol_function_declaration": "function",
            "init_declaration": "function",
            "deinit_declaration": "function",
            "subscript_declaration": "function",
            "typealias_declaration": "type",
        },
        {}, _CLASSLIKE,
    ),
    "go": _Grammar(
        "tree_sitter_go", "language",
        {"function_declaration": "function", "method_declaration": "method", "type_declaration": "type"},
        {}, frozenset(),
    ),
    "rust": _Grammar(
        "tree_sitter_rust", "language",
        {
            "function_item": "function",
            "function_signature_item": "function",
            "struct_item": "struct",
            "enum_item": "enum",
            "union_item": "struct",
            "trait_item": "trait",
            "impl_item": "impl",
            "mod_item": "module",
            "macro_definition": "macro",
            "type_item": "type",
        },
        {}, _CLASSLIKE,
    ),
    "java": _Grammar(
        "tree_sitter_java", "language",
        {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "record_declaration": "record",
            "annotation_type_declaration": "interface",
            "method_declaration": "method",
            "constructor_declaration": "constructor",
        },
        {}, _CLASSLIKE,
    ),
}

_EXTENSIONS = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

# Siblings that belong to the definition after them: doc comments, Rust
# attributes, decorators that the grammar keeps outside the declaration.
_LEADING = frozenset({
    "comment", "line_comment", "block_comment", "multiline_comment",
    "attribute_item", "decorator", "annotation",
})
_FUNCTION_VALUES = frozenset({
    "arrow_function", "function_expression", "function", "generator_function",
})
_BODY_TYPES = frozenset({
    "class_body", "enum_class_body", "protocol_body", "interface_body", "enum_body",
    "declaration_list", "object_type", "statement_block", "block", "record_body",
})
_NAME_RE = re.compile(r"\s+")
_MAX_DEPTH = 4

_LANGUAGES: dict[str, object] = {}
_LANGUAGE_LOCK = threading.Lock()


def _parser(language: str):
    """A new tree-sitter parser for ``language``, or None where there is no grammar.

    New each time: a parser is not safe to share between the threads that
    requests run on. The grammar it loads is shared, once.
    """
    with _LANGUAGE_LOCK:
        if language not in _LANGUAGES:
            loaded = None
            spec = _GRAMMARS.get(language)
            if spec is not None:
                try:
                    from tree_sitter import Language

                    module = importlib.import_module(spec.module)
                    loaded = Language(getattr(module, spec.function)())
                except Exception:
                    loaded = None
            _LANGUAGES[language] = loaded
        grammar = _LANGUAGES[language]
    if grammar is None:
        return None
    try:
        from tree_sitter import Parser

        return Parser(grammar)
    except Exception:
        return None


def _end_line(node) -> int:
    row, column = node.end_point
    # A node ending at the start of a line ends on the line before it.
    return max(node.start_point[0] + 1, row if column == 0 else row + 1)


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None and node.text else ""


def _clean_name(raw: str) -> str:
    return _NAME_RE.sub(" ", raw).strip()[:80]


class _Outline:
    """Definitions in one parse tree, as regions."""

    def __init__(self, grammar: _Grammar) -> None:
        self.g = grammar

    def definition(self, node):
        """(kind, name, declaration node) when ``node`` defines something, else None."""
        inner = node
        field = self.g.wrappers.get(node.type)
        if field is not None:
            inner = node.child_by_field_name(field)
            if inner is None:
                return None
        kind = self.g.defs.get(inner.type)
        if kind is None:
            return None
        if inner.type in ("lexical_declaration", "variable_declaration", "public_field_definition"):
            return self._binding(node, inner)
        kind = self._refine(inner, kind)
        return kind, _clean_name(self._name(inner)), inner

    def _binding(self, node, inner):
        """``const f = () => {}`` is a function; a many-line value is worth a region too."""
        declarator = inner
        if inner.type != "public_field_definition":
            declarator = next((c for c in inner.named_children if c.type == "variable_declarator"), None)
            if declarator is None:
                return None
        value = declarator.child_by_field_name("value")
        name = _clean_name(_text(declarator.child_by_field_name("name")))
        if value is not None and value.type in _FUNCTION_VALUES:
            return ("method" if inner.type == "public_field_definition" else "function"), name, inner
        if value is not None and value.type == "class":
            return "class", name, inner
        if inner.type == "public_field_definition":
            return None
        if _end_line(node) > node.start_point[0] + 1:
            return "variable", name, inner
        return None

    def _refine(self, node, kind: str) -> str:
        if kind == "class":
            keywords = {c.type for c in node.children if not c.is_named}
            for word in ("struct", "enum", "extension", "actor", "interface", "protocol", "object"):
                if word in keywords:
                    return word
        if kind == "type" and node.type == "type_declaration":
            spec = next((c for c in node.named_children if c.type in ("type_spec", "type_alias")), None)
            shape = spec.child_by_field_name("type") if spec is not None else None
            if shape is not None and shape.type == "struct_type":
                return "struct"
            if shape is not None and shape.type == "interface_type":
                return "interface"
        return kind

    def _name(self, node) -> str:
        if node.type == "impl_item":
            target = _text(node.child_by_field_name("type"))
            trait = _text(node.child_by_field_name("trait"))
            return f"{trait} for {target}" if trait else target
        if node.type == "type_declaration":
            spec = next((c for c in node.named_children if c.type in ("type_spec", "type_alias")), None)
            return _text(spec.child_by_field_name("name")) if spec is not None else ""
        if node.type == "init_declaration":
            return "init"
        if node.type == "deinit_declaration":
            return "deinit"
        if node.type == "companion_object":
            named = node.child_by_field_name("name")
            return _text(named) if named is not None else "companion"
        named = node.child_by_field_name("name")
        if named is not None:
            return _text(named)
        for child in node.named_children:
            if child.type in ("identifier", "type_identifier", "simple_identifier", "property_identifier"):
                return _text(child)
        return ""

    def body(self, node):
        found = node.child_by_field_name("body")
        if found is not None:
            return found
        return next((c for c in node.named_children if c.type in _BODY_TYPES), None)

    def regions(self, parent, depth: int, top: bool, out: list[Region], container_kind: str | None) -> None:
        """Definitions among ``parent``'s children; at the top, blocks for the rest."""
        children = parent.named_children
        leading: list = []  # comments waiting to see whether a definition follows
        pending: list[tuple[int, int]] = []  # top-level spans that are not definitions

        def flush_leading() -> None:
            if top:
                pending.extend((c.start_point[0] + 1, _end_line(c)) for c in leading)
            leading.clear()

        for node in children:
            if node.type in _LEADING:
                if leading and node.start_point[0] > _end_line(leading[-1]):
                    flush_leading()
                leading.append(node)
                continue
            found = self.definition(node)
            if found is None:
                flush_leading()
                if top:
                    pending.append((node.start_point[0] + 1, _end_line(node)))
                continue
            kind, name, declaration = found
            start = node.start_point[0] + 1
            if leading and _end_line(leading[-1]) >= start - 1:
                start = leading[0].start_point[0] + 1
                leading.clear()
            else:
                flush_leading()
            if top:
                out.extend(_blocks(pending, depth))
                pending.clear()
            if kind == "function" and container_kind in _METHOD_HOSTS:
                kind = "method"
            out.append(Region(kind, name, start, _end_line(node), depth))
            if kind in self.g.containers and depth + 1 < _MAX_DEPTH:
                body = self.body(declaration)
                if body is not None:
                    self.regions(body, depth + 1, False, out, kind)
        flush_leading()
        if top:
            out.extend(_blocks(pending, depth))


def _blocks(spans: list[tuple[int, int]], depth: int) -> list[Region]:
    """Consecutive spans as blocks, a blank line apart making a new one."""
    blocks: list[Region] = []
    for start, end in spans:
        if blocks and start <= blocks[-1].end + 1:
            blocks[-1].end = max(blocks[-1].end, end)
        else:
            blocks.append(Region("block", "", start, end, depth))
    return blocks


_CLOSER_RE = re.compile(r"^([\]\)}]|end\b|fi\b|done\b|esac\b)")


def paragraphs(lines: list[str]) -> list[Region]:
    """Top-level blocks for a language without a grammar.

    A blank line followed by an unindented line starts a new block; indented
    lines and closing brackets stay with the block they close, so a function
    with blank lines inside it is still one region.
    """
    regions: list[Region] = []
    start = last = 0
    gap = False
    for number, line in enumerate(lines, 1):
        if not line.strip():
            gap = start > 0
            continue
        top = not line[0].isspace() and not _CLOSER_RE.match(line)
        if not start:
            start = number
        elif gap and top:
            regions.append(Region("block", "", start, last, 0))
            start = number
        gap = False
        last = number
    if start:
        regions.append(Region("block", "", start, last, 0))
    return regions


def language_of(path: str) -> str | None:
    return _EXTENSIONS.get(os.path.splitext(path)[1].lower())


def find_regions(lines: list[str], path: str) -> tuple[list[Region], str]:
    """The regions of a file and where they came from: "tree-sitter" or "blocks"."""
    language = language_of(path)
    parser = _parser(language) if language else None
    if parser is not None:
        try:
            tree = parser.parse("\n".join(lines).encode("utf-8"))
            out: list[Region] = []
            _Outline(_GRAMMARS[language]).regions(tree.root_node, 0, True, out, None)
            last = len(lines)
            for region in out:
                region.end = min(region.end, last)
            return [r for r in out if r.start <= r.end], "tree-sitter"
        except Exception:
            pass
    return paragraphs(lines), "blocks"


def regions_payload(root: str, rel: object) -> dict:
    """``GET /api/regions``: the pointable region map of one file.

    Parsed from the file as written -- a redacted line would parse as noise --
    but only kinds, names and line numbers leave, never text.
    """
    loaded = load(root, rel)
    regions, source = find_regions(loaded.lines, loaded.rel)
    return {
        "path": loaded.rel,
        "language": language_of(loaded.rel) or "",
        "source": source,
        "line_count": len(loaded.lines),
        "regions": [r.to_dict() for r in regions],
        "truncated": loaded.truncated,
    }


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

_GIT_ENV = {
    # A diff must not take index.lock out from under the agent's own git.
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
}
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(root: str, *args: str, cap: int = MAX_DIFF_BYTES) -> tuple[int, bytes, bool]:
    """Run git in ``root``; (returncode, stdout up to ``cap``, whether it was cut)."""
    argv = [
        "git", "-C", root,
        "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "-c", "color.ui=false",
        *args,
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env={**get_subprocess_env(), **_GIT_ENV},
        )
    except OSError:
        raise ReaderError(503, "git is not available") from None
    chunks: list[bytes] = []
    size = 0
    cut = False
    timer = threading.Timer(GIT_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > cap:
                cut = True
                proc.kill()
                break
        proc.wait()
    finally:
        timer.cancel()
        if proc.stdout is not None:
            proc.stdout.close()
    data = b"".join(chunks)
    return proc.returncode, data[:cap], cut


def _require_repo(root: str) -> None:
    code, out, _ = _git(root, "rev-parse", "--is-inside-work-tree", cap=64)
    if code != 0 or out.strip() != b"true":
        raise ReaderError(409, "not a git repository")


def _has_head(root: str) -> bool:
    code, _, _ = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", cap=128)
    return code == 0


# ---------------------------------------------------------------------------
# Diff (Review mode)
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of an unusual path."""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        body = path[1:-1].encode("latin-1", errors="backslashreplace").decode("unicode_escape")
        return body.encode("latin-1", errors="replace").decode("utf-8", errors="replace")
    return path


def _side(raw: str, prefix: str) -> str | None:
    raw = _unquote(raw.strip())
    if raw == "/dev/null":
        return None
    return raw.removeprefix(prefix)


def parse_diff(text: str) -> list[dict]:
    """A unified diff (``git diff``) as files of hunks of ``[op, text]`` lines."""
    files: list[dict] = []
    current: dict | None = None
    hunk: dict | None = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            current = {"path": "", "old_path": None, "status": "modified", "binary": False,
                       "withheld": False, "omitted": False, "hunks": []}
            files.append(current)
            hunk = None
            # Until ---/+++ say otherwise (a binary or mode-only change has none).
            match = re.match(r"^diff --git (\"?a/.*?\"?) (\"?b/.*\"?)$", line)
            if match:
                current["path"] = _side(match.group(2), "b/") or ""
            continue
        if current is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                current["status"] = "added"
            elif line.startswith("deleted file mode"):
                current["status"] = "deleted"
            elif line.startswith("rename from "):
                current["old_path"] = _unquote(line[len("rename from "):])
                current["status"] = "renamed"
            elif line.startswith("rename to "):
                current["path"] = _unquote(line[len("rename to "):])
            elif line.startswith(("Binary files ", "GIT binary patch")):
                current["binary"] = True
            elif line.startswith("--- "):
                old = _side(line[4:], "a/")
                if old is not None and current["status"] == "modified" and old != current["path"]:
                    current["old_path"] = old
            elif line.startswith("+++ "):
                new = _side(line[4:], "b/")
                if new is not None:
                    current["path"] = new
        match = _HUNK_RE.match(line)
        if match:
            hunk = {
                "old_start": int(match.group(1)),
                "old_lines": int(match.group(2)) if match.group(2) is not None else 1,
                "new_start": int(match.group(3)),
                "new_lines": int(match.group(4)) if match.group(4) is not None else 1,
                "heading": match.group(5).strip(),
                "lines": [],
            }
            current["hunks"].append(hunk)
            continue
        if hunk is not None and line[:1] in (" ", "+", "-"):
            hunk["lines"].append([line[0], line[1:]])
    for entry in files:
        if entry["status"] == "deleted" and not entry["path"]:
            entry["path"] = entry["old_path"] or ""
    return files


def _redact_hunks(entry: dict) -> None:
    for hunk in entry["hunks"]:
        texts, _ = redact_lines([text for _, text in hunk["lines"]])
        hunk["lines"] = [
            [op, (text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " …")]
            for (op, _), text in zip(hunk["lines"], texts)
        ]


def _untracked(root: str, budget: int, spent: int) -> tuple[list[dict], bool]:
    """Files the agent created and git does not know yet, as whole-file additions.

    ``withheld`` is a file that is never served; ``omitted``, one too big to
    show -- alone, or after ``spent`` bytes of diff already.
    """
    code, out, cut = _git(root, "ls-files", "--others", "--exclude-standard", "-z", cap=1024 * 1024)
    if code != 0:
        return [], False
    entries: list[dict] = []
    for raw in out.split(b"\x00"):
        if not raw:
            continue
        if len(entries) >= budget:
            return entries, True
        rel = raw.decode("utf-8", errors="replace")
        entry = {"path": rel, "old_path": None, "status": "untracked", "binary": False,
                 "withheld": False, "omitted": False, "hunks": []}
        entries.append(entry)
        try:
            real, _ = resolve_path(root, rel)
        except ReaderError:
            entry["withheld"] = True
            continue
        try:
            if os.stat(real).st_size > MAX_UNTRACKED_BYTES:
                entry["omitted"] = True
                continue
            data, _ = _read_regular(real)
        except (OSError, ReaderError):
            entry["omitted"] = True
            continue
        if _is_binary(data):
            entry["binary"] = True
            continue
        if spent + len(data) > MAX_DIFF_BYTES:
            entry["omitted"] = True
            continue
        spent += len(data)
        lines = split_lines(data.decode("utf-8", errors="replace"))
        entry["hunks"].append({
            "old_start": 0, "old_lines": 0, "new_start": 1 if lines else 0, "new_lines": len(lines),
            "heading": "", "lines": [["+", ln] for ln in lines],
        })
    return entries, cut


def diff_payload(root: str) -> dict:
    """``GET /api/diff``: the working tree against HEAD, with new files, as hunks.

    Paths are relative to the project directory, which need not be the
    repository's top. Secrets are redacted line by line; a file that is never
    served is listed as withheld, without its hunks.
    """
    _require_repo(root)
    base = "HEAD" if _has_head(root) else _EMPTY_TREE
    # Prefixes and colour spelled out: a user's diff.noprefix, mnemonicPrefix
    # or color.diff would otherwise change what is parsed.
    code, out, cut = _git(
        root, "diff", base, "--relative", "--no-ext-diff", "--no-textconv", "--no-color",
        "--src-prefix=a/", "--dst-prefix=b/", "-U3", "--",
    )
    if code != 0 and not cut:
        raise ReaderError(500, "git diff failed")
    files = parse_diff(out.decode("utf-8", errors="replace"))
    truncated = cut
    if len(files) > MAX_DIFF_FILES:
        files, truncated = files[:MAX_DIFF_FILES], True
    for entry in files:
        if refusal(entry["path"]) or (entry["old_path"] and refusal(entry["old_path"])):
            entry["withheld"] = True
            entry["hunks"] = []
        else:
            _redact_hunks(entry)
    extra, extra_cut = _untracked(root, max(0, MAX_DIFF_FILES - len(files)), len(out))
    for entry in extra:
        _redact_hunks(entry)
    return {"files": files + extra, "truncated": truncated or extra_cut}


# ---------------------------------------------------------------------------
# Files (Read mode: "open week.ts")
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", ".gradle",
    ".next", "out", "target", "DerivedData", ".idea", ".mypy_cache", ".pytest_cache",
})


def _all_files(root: str) -> tuple[list[str], str, bool]:
    code, out, cut = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                          cap=8 * 1024 * 1024)
    if code == 0:
        names = [raw.decode("utf-8", errors="replace") for raw in out.split(b"\x00") if raw]
        return list(dict.fromkeys(names)), "git", cut
    found: list[str] = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            found.append(os.path.relpath(os.path.join(directory, name), root))
            if len(found) >= MAX_LISTED_FILES:
                return found, "walk", True
    return found, "walk", False


def _rank(path: str, query: str) -> int | None:
    """Lower is better; None is no match. Basename beats path; prefix beats substring."""
    low = path.lower()
    base = os.path.basename(low)
    stem = os.path.splitext(base)[0]
    if base == query or stem == query:
        return 0
    if base.startswith(query):
        return 1
    if query in base:
        return 2
    if query in low:
        return 3
    it = iter(low)
    if all(ch in it for ch in query):
        return 4
    return None


def files_payload(root: str, query: str = "", limit: int = 50) -> dict:
    """``GET /api/files``: the project's files, best match first.

    With no query, the most recently changed first -- what an agent just
    touched is the likeliest thing to want to read.
    """
    names, source, cut = _all_files(root)
    names = [n for n in names if not refusal(n)]
    limit = max(1, min(int(limit), 500))
    query = (query or "").strip().lower()

    def mtime(rel: str) -> float:
        try:
            return os.stat(os.path.join(root, rel)).st_mtime
        except OSError:
            return 0.0

    if query:
        scored = [(rank, rel) for rel in names if (rank := _rank(rel, query)) is not None]
        scored.sort(key=lambda pair: (pair[0], -mtime(pair[1]), pair[1]))
        chosen = [rel for _, rel in scored[:limit]]
        matches = len(scored)
    else:
        chosen = sorted(names, key=lambda rel: (-mtime(rel), rel))[:limit]
        matches = len(names)
    return {"files": chosen, "matches": matches, "source": source, "truncated": cut or matches > limit}


# ---------------------------------------------------------------------------
# Deixis: what the wearer pointed at, in the assistant's words
# ---------------------------------------------------------------------------

# Assistants whose prompt takes an ``@path`` mention, *as the bridge types it*:
# ``tmux send-keys`` into the live TUI, where an autocomplete popup that holds
# the Enter would pick a file instead of sending the question. Only Claude
# Code has been watched doing it right (2.1.283, 2026-09-27: it read the file
# and answered about the pointed-at function). The rest get the plain path,
# which every one of them can open, until each is watched too.
AT_MENTION = frozenset({"claude"})

_SYMBOL_RE = re.compile(r"^[\w$.:<>\[\]#~-]{1,120}$")
_MAX_LINE_NUMBER = 1_000_000


def _reference_lines(raw: object) -> tuple[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) not in (1, 2):
        raise ReaderError(400, "reference.lines must be [first, last]")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in raw):
        raise ReaderError(400, "reference.lines must be whole numbers")
    first, last = raw[0], raw[-1]
    if not 1 <= first <= last <= _MAX_LINE_NUMBER:
        raise ReaderError(400, "reference.lines must run forward from 1")
    return first, last


def mention(assistant: str, rel: str) -> str:
    """``rel`` as ``assistant`` refers to a file."""
    if assistant in AT_MENTION and not any(ch.isspace() for ch in rel):
        return "@" + rel
    return f'"{rel}"' if any(ch.isspace() for ch in rel) else rel


def expand_reference(message: str, reference: object, assistant: str, root: str) -> str:
    """The wearer's words with what they pointed at, in ``assistant``'s idiom.

    ``{"path": "src/week.ts", "lines": [142, 168], "symbol": "formatISOWeek"}``
    after "what does this do?" becomes, for Claude Code,
    ``what does this do? @src/week.ts (lines 142-168, formatISOWeek)``.
    Server-side so a client never needs to know which assistant it is
    talking to. The path must be a file the Reader would serve.
    """
    if not isinstance(reference, dict):
        raise ReaderError(400, "reference must be an object")
    real, rel = resolve_path(root, reference.get("path"))
    if not os.path.isfile(real):
        raise ReaderError(404, "the referenced file does not exist")
    lines = _reference_lines(reference.get("lines"))
    symbol = reference.get("symbol")
    detail: list[str] = []
    if lines is not None:
        first, last = lines
        detail.append(f"line {first}" if first == last else f"lines {first}-{last}")
    if isinstance(symbol, str) and _SYMBOL_RE.match(symbol.strip()):
        detail.append(symbol.strip())
    words = message.strip()
    referred = mention(assistant, rel) + (f" ({', '.join(detail)})" if detail else "")
    return f"{words} {referred}" if words else referred
