"""Tests for HMAC signing and verification in the VoxHerd bridge server.

Covers:
- _sign_message() / verify_message() round-trip
- Canonical JSON serialization matching Python's json.dumps(sort_keys=True)
- Cross-language test vectors from scripts/hmac-test-vectors.json
- Edge cases: nested dicts, special chars, null, booleans, large integers, unicode
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bridge.server_state import _sign_message, verify_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-hmac-hardening-token-do-not-use-in-production"


def _canonical(obj: dict) -> str:
    """Produce canonical JSON matching the bridge's signing format."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _patch_token(token: str = TEST_TOKEN):
    """Context manager to set the auth token for signing/verification."""
    return patch("bridge.auth._AUTH_TOKEN", token)


# ---------------------------------------------------------------------------
# Basic round-trip tests
# ---------------------------------------------------------------------------


class TestSignVerifyRoundTrip:
    """Verify that _sign_message() output passes verify_message()."""

    def test_simple_message(self):
        with _patch_token():
            msg = {"type": "state_sync", "status": "ok"}
            signed = _sign_message(msg)
            assert "_sig" in signed
            assert signed["type"] == "state_sync"
            assert verify_message(dict(signed)) is True

    def test_nested_dicts(self):
        with _patch_token():
            msg = {
                "type": "test",
                "outer": {"inner_b": 2, "inner_a": 1},
                "list": [1, "two", True],
            }
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True

    def test_unicode_values(self):
        with _patch_token():
            msg = {"type": "test", "text": "café — µs ⏺ 😀🔥"}
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True

    def test_null_values(self):
        with _patch_token():
            msg = {"type": "test", "value": None, "present": "yes"}
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True

    def test_booleans(self):
        with _patch_token():
            msg = {"type": "test", "flag": True, "off": False, "num": 1}
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True

    def test_large_integers(self):
        with _patch_token():
            msg = {"type": "test", "ts": 1700000000000, "neg": -9007199254740991}
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True

    def test_empty_collections(self):
        with _patch_token():
            msg = {"type": "test", "d": {}, "l": [], "s": ""}
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True

    def test_special_chars(self):
        with _patch_token():
            msg = {
                "type": "test",
                "quotes": 'He said "hello"',
                "backslash": "path\\to\\file",
                "newline": "line1\nline2",
                "tab": "col1\tcol2",
            }
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True

    def test_control_chars(self):
        with _patch_token():
            msg = {
                "type": "test",
                "backspace": "before\bafter",
                "formfeed": "before\fafter",
            }
            signed = _sign_message(msg)
            assert verify_message(dict(signed)) is True


# ---------------------------------------------------------------------------
# Verification failure tests
# ---------------------------------------------------------------------------


class TestVerifyMessageFailures:
    """Verify that tampered or unsigned messages are rejected."""

    def test_missing_sig(self):
        with _patch_token():
            msg = {"type": "test", "data": "value"}
            assert verify_message(msg) is False

    def test_wrong_sig(self):
        with _patch_token():
            msg = {"type": "test", "data": "value", "_sig": "0" * 64}
            assert verify_message(msg) is False

    def test_tampered_value(self):
        with _patch_token():
            signed = _sign_message({"type": "test", "data": "original"})
            signed["data"] = "tampered"
            assert verify_message(dict(signed)) is False

    def test_extra_field(self):
        with _patch_token():
            signed = _sign_message({"type": "test"})
            signed["injected"] = "evil"
            assert verify_message(dict(signed)) is False

    def test_wrong_token(self):
        with _patch_token("correct-token"):
            signed = _sign_message({"type": "test"})

        with _patch_token("wrong-token"):
            assert verify_message(dict(signed)) is False

    def test_no_auth_token_always_passes(self):
        """When auth is disabled (no token), verification always succeeds."""
        with _patch_token(None):
            msg = {"type": "test", "data": "anything"}
            assert verify_message(msg) is True

    def test_no_auth_token_skips_signing(self):
        """When auth is disabled, _sign_message returns message unchanged."""
        with _patch_token(None):
            msg = {"type": "test"}
            result = _sign_message(msg)
            assert "_sig" not in result


# ---------------------------------------------------------------------------
# Canonical JSON tests
# ---------------------------------------------------------------------------


class TestCanonicalJSON:
    """Verify the canonical JSON format used for signing."""

    def test_sorted_keys(self):
        assert _canonical({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_no_spaces(self):
        result = _canonical({"key": "value"})
        assert " " not in result.replace("value", "")

    def test_uuid_key_ordering(self):
        """UUID keys sort by byte comparison, not natural/locale sort."""
        result = _canonical({
            "7b359a2c": "second",
            "73877b3e": "first",
        })
        # '7' == '7', then '3' < 'b' in byte comparison
        assert result == '{"73877b3e":"first","7b359a2c":"second"}'

    def test_natural_sort_trap(self):
        """Byte sort: a1 < a10 < a2 (not a1, a2, a10 as natural sort would do)."""
        result = _canonical({"a10": "ten", "a2": "two", "a1": "one"})
        assert result == '{"a1":"one","a10":"ten","a2":"two"}'

    def test_nested_keys_sorted(self):
        result = _canonical({"z": {"b": 2, "a": 1}, "a": "first"})
        assert result == '{"a":"first","z":{"a":1,"b":2}}'

    def test_ensure_ascii_false(self):
        """Non-ASCII characters should appear as-is, not escaped."""
        result = _canonical({"text": "café"})
        assert "café" in result
        assert "\\u" not in result

    def test_boolean_lowercase(self):
        result = _canonical({"t": True, "f": False})
        assert '"t":true' in result
        assert '"f":false' in result

    def test_null_literal(self):
        result = _canonical({"n": None})
        assert '"n":null' in result

    def test_backspace_formfeed_escaping(self):
        """Python escapes \\b and \\f specifically (not \\u0008/\\u000c)."""
        result = _canonical({"bs": "\b", "ff": "\f"})
        assert '"bs":"\\b"' in result
        assert '"ff":"\\f"' in result


# ---------------------------------------------------------------------------
# Cross-language test vectors
# ---------------------------------------------------------------------------


class TestCrossLanguageVectors:
    """Verify signing against pre-computed test vectors.

    These vectors are generated by scripts/hmac-test-vectors.py and shared
    with the Swift test suite to ensure byte-for-byte compatibility.
    """

    @staticmethod
    def _load_vectors() -> list[dict]:
        vectors_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "hmac-test-vectors.json"
        if not vectors_path.exists():
            pytest.skip(f"Test vectors not found at {vectors_path}")
        with open(vectors_path) as f:
            data = json.load(f)
        assert data["token"] == TEST_TOKEN, "Token mismatch in test vectors file"
        return data["vectors"]

    def test_canonical_json_matches_vectors(self):
        """Verify our canonical JSON output matches the pre-computed vectors."""
        for vec in self._load_vectors():
            result = _canonical(vec["payload"])
            assert result == vec["canonical_json"], (
                f"Canonical JSON mismatch for '{vec['name']}':\n"
                f"  got:      {result}\n"
                f"  expected: {vec['canonical_json']}"
            )

    def test_hmac_matches_vectors(self):
        """Verify our HMAC output matches the pre-computed vectors."""
        for vec in self._load_vectors():
            body = _canonical(vec["payload"])
            sig = hmac.new(
                TEST_TOKEN.encode(), body.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            assert sig == vec["hmac_sha256"], (
                f"HMAC mismatch for '{vec['name']}':\n"
                f"  got:      {sig}\n"
                f"  expected: {vec['hmac_sha256']}"
            )

    def test_sign_message_matches_vectors(self):
        """Verify _sign_message() produces the expected signature for each vector."""
        with _patch_token():
            for vec in self._load_vectors():
                signed = _sign_message(dict(vec["payload"]))
                assert signed["_sig"] == vec["hmac_sha256"], (
                    f"_sign_message() mismatch for '{vec['name']}':\n"
                    f"  got:      {signed['_sig']}\n"
                    f"  expected: {vec['hmac_sha256']}"
                )

    def test_verify_message_accepts_vectors(self):
        """Verify verify_message() accepts signatures from the test vectors."""
        with _patch_token():
            for vec in self._load_vectors():
                msg = dict(vec["payload"])
                msg["_sig"] = vec["hmac_sha256"]
                assert verify_message(msg) is True, (
                    f"verify_message() rejected valid signature for '{vec['name']}'"
                )


# ---------------------------------------------------------------------------
# The generator and the committed fixtures must agree.
#
# `scripts/hmac-test-vectors.json` is what the Kotlin (`HmacVectorTest`) and
# Swift (`HMACTests`) suites verify their canonical-JSON encoders against, and
# `scripts/hmac-test-vectors.py` is supposed to be what produces it. They had
# drifted: `float_wall_clock` and `float_notation_boundaries` were added to the
# JSON by hand, so re-running the generator would have silently DELETED them --
# and those two are the regression coverage for the one place the encoders
# genuinely diverge (Python's `repr` vs Java's `Double.toString`, which pick
# different notation for 1e7 <= |x| < 1e16, e.g. a wall-clock timestamp).
#
# Losing a vector does not fail anything: the clients would keep passing against
# a smaller set.
# ---------------------------------------------------------------------------


def _require_vector_files() -> "tuple[pathlib.Path, pathlib.Path]":
    """The generator and the fixtures, or skip.

    Both are in ``scripts/sync-to-public.sh``'s EXCLUDE_PATTERNS while this test
    file is synced (``bridge/`` is an INCLUDE_DIR), so in the public
    ``voxherd-bridge`` repo neither exists -- and without this guard these tests
    were four hard FileNotFoundErrors on a fresh clone of a BUSL-licensed repo.
    ``TestCrossLanguageVectors._load_vectors`` above skips for exactly this
    reason; this is the same guard, which the new tests were missing.
    """
    import pathlib

    scripts = pathlib.Path(__file__).resolve().parents[2] / "scripts"
    generator = scripts / "hmac-test-vectors.py"
    fixtures = scripts / "hmac-test-vectors.json"
    for path in (generator, fixtures):
        if not path.exists():
            pytest.skip(f"not present in this checkout: {path.name}")
    return generator, fixtures


def _generated_vectors() -> dict:
    """Run the generator and parse what it prints."""
    import json
    import subprocess
    import sys

    generator, _ = _require_vector_files()
    result = subprocess.run(
        [sys.executable, str(generator)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    # `check=False` plus this assert: `check=True` surfaced a crashing generator
    # as a bare CalledProcessError with the traceback swallowed.
    assert result.returncode == 0, (
        f"{generator.name} exited {result.returncode}:\n{result.stderr[-2000:]}"
    )
    return json.loads(result.stdout)


def _committed_vectors() -> dict:
    import json

    _, fixtures = _require_vector_files()
    return json.loads(fixtures.read_text())


class TestTheGeneratorReproducesTheFixtures:
    """Compared by name, not by file bytes.

    The committed file escapes non-ASCII as ``\\uXXXX`` and orders the vectors
    differently, both of which are invisible to a JSON parser -- and both client
    suites iterate the array and look nothing up by position. So the contract is
    the SET of vectors, not the file's formatting.
    """

    def test_no_vector_is_missing_from_either_side(self):
        generated = {v["name"] for v in _generated_vectors()["vectors"]}
        committed = {v["name"] for v in _committed_vectors()["vectors"]}
        assert generated == committed, (
            f"only generated: {sorted(generated - committed)}; "
            f"only committed: {sorted(committed - generated)} — regenerating "
            f"hmac-test-vectors.json would change which vectors the Kotlin and "
            f"Swift suites check"
        )

    def test_every_vector_agrees_on_its_canonical_form_and_signature(self):
        generated = {v["name"]: v for v in _generated_vectors()["vectors"]}
        committed = {v["name"]: v for v in _committed_vectors()["vectors"]}
        for name in sorted(set(generated) & set(committed)):
            assert generated[name]["canonical_json"] == committed[name]["canonical_json"], name
            assert generated[name]["hmac_sha256"] == committed[name]["hmac_sha256"], name

    def test_the_token_matches(self):
        assert _generated_vectors()["token"] == _committed_vectors()["token"]

    def test_the_float_divergence_vectors_are_present(self):
        """Named explicitly: these are the ones that went missing.

        The Kotlin encoder reproduces Python's `repr` by hand, and 12345678.0 is
        the value that proves it has to -- Python writes `12345678.0`, Java's
        `Double.toString` writes `1.2345678E7`, and a mismatched canonical
        string means a rejected HMAC on every signed frame carrying a timestamp.
        """
        committed = {v["name"]: v for v in _committed_vectors()["vectors"]}
        assert "float_wall_clock" in committed
        assert "float_notation_boundaries" in committed
        boundaries = committed["float_notation_boundaries"]
        assert "12345678.0" in boundaries["canonical_json"]
        assert "1.2345678E7" not in boundaries["canonical_json"]
