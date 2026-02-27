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
