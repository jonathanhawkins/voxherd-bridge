"""The secret-redaction logic exists twice. This fails when the copies drift.

``bridge/transcript_render.py`` guards what the Reader serves and what the
transcript stream carries; ``hooks/on-stop.py`` guards what the stop hook sends
to a summariser, which on an opted-in install is a third-party API. They are
deliberate duplicates -- the hook must work with nothing but the stdlib and
whatever ``install.sh`` copied next to it -- so nothing but a test can keep them
honest, and a silent divergence here is a credential leaving the machine.

Comparing the parsed source rather than the text: formatting may differ, meaning
may not.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SHARED_NAMES = (
    "_SECRET_SHAPES",
    "_SECRET_ASSIGNMENT_RE",
    "_SK_KEY_RE",
    "_BEARER_RE",
    "_URL_CREDENTIALS_RE",
    "_contains_secret",
    "_redact_secrets",
)

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _definitions(relative: str) -> dict[str, str]:
    tree = ast.parse((_REPO / relative).read_text())
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = getattr(target, "id", "")
                if name in _SHARED_NAMES:
                    found[name] = ast.unparse(node)
        elif isinstance(node, ast.FunctionDef) and node.name in _SHARED_NAMES:
            found[node.name] = ast.unparse(node)
    return found


@pytest.fixture(scope="module")
def copies() -> tuple[dict[str, str], dict[str, str]]:
    return _definitions("bridge/transcript_render.py"), _definitions("hooks/on-stop.py")


@pytest.mark.parametrize("name", _SHARED_NAMES)
def test_both_copies_define_it(name, copies):
    bridge_defs, hook_defs = copies
    assert name in bridge_defs, f"{name} vanished from bridge/transcript_render.py"
    assert name in hook_defs, f"{name} vanished from hooks/on-stop.py"


@pytest.mark.parametrize("name", _SHARED_NAMES)
def test_the_two_copies_agree(name, copies):
    bridge_defs, hook_defs = copies
    assert bridge_defs.get(name) == hook_defs.get(name), (
        f"{name} has drifted between bridge/transcript_render.py and "
        f"hooks/on-stop.py — whichever is weaker is now leaking"
    )


def test_a_representative_credential_is_caught_by_both():
    """Behavioural spine, so the parity above cannot pass on two broken copies."""
    from bridge.transcript_render import _contains_secret

    for line in (
        'password = "hunter2"',
        "db_password: swordfish",
        "SECRET_VALUE = \"x\"",
        "key: AIzaSyD-0123456789",
        "Authorization: Bearer abc.def",
        "-----BEGIN RSA PRIVATE KEY-----",
        "url = https://user:pw@github.com/x/y.git",
    ):
        assert _contains_secret(line), line
    for line in ("def main():", "# the authoritative list", "import secrets",
                 "# token count: 150.9k/200k", "# a task-file write race"):
        assert not _contains_secret(line), line
