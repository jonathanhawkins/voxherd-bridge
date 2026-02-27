#!/usr/bin/env python3
"""Generate HMAC test vectors for cross-language verification.

Produces a JSON file with test payloads, their canonical JSON representation
(as json.dumps produces it), and the HMAC-SHA256 signature for a fixed test
token. Both Python and Swift test suites import these vectors to verify
byte-for-byte serialization compatibility.

Usage:
    python3 scripts/hmac-test-vectors.py > scripts/hmac-test-vectors.json

The output JSON has this structure:
    {
      "token": "test-token-...",
      "vectors": [
        {
          "name": "description of the test case",
          "payload": { ... },
          "canonical_json": "...",
          "hmac_sha256": "..."
        },
        ...
      ]
    }
"""

import hashlib
import hmac
import json
import sys


TEST_TOKEN = "test-hmac-hardening-token-do-not-use-in-production"


def canonical(obj: dict) -> str:
    """Produce the canonical JSON string matching the bridge's signing format."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign(obj: dict) -> str:
    """Produce the HMAC-SHA256 hex digest for a payload."""
    body = canonical(obj)
    return hmac.new(
        TEST_TOKEN.encode(), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# Test vectors
# ---------------------------------------------------------------------------

vectors = []


def add(name: str, payload: dict) -> None:
    vectors.append({
        "name": name,
        "payload": payload,
        "canonical_json": canonical(payload),
        "hmac_sha256": sign(payload),
    })


# 1. Simple flat message
add("simple_flat", {
    "type": "state_sync",
    "status": "ok",
})

# 2. UUID-like keys (the original bug: localizedStandardCompare vs byte sort)
add("uuid_keys", {
    "73877b3e-4321-4abc-9def-111111111111": "first",
    "7b359a2c-1234-4abc-9def-222222222222": "second",
    "type": "test",
})

# 3. Keys that differ only by digit-letter boundary
# localizedStandardCompare sorts "a2" before "a10" (natural sort)
# byte comparison sorts "a10" before "a2" (because '1' < '2')
add("natural_sort_trap", {
    "a10": "ten",
    "a2": "two",
    "a1": "one",
})

# 4. Non-ASCII characters (ensure_ascii=False)
add("non_ascii", {
    "type": "test",
    "text": "caf\u00e9 \u2014 \u00b5s \u23fa recording",
    "emoji": "\U0001f600\U0001f525",
})

# 5. Nested dicts with mixed key ordering
add("nested_dicts", {
    "outer_z": {"inner_b": 2, "inner_a": 1},
    "outer_a": {"inner_y": True, "inner_x": False},
    "type": "nested",
})

# 6. Booleans (must be true/false, not 1/0)
add("booleans", {
    "flag_true": True,
    "flag_false": False,
    "number_one": 1,
    "number_zero": 0,
    "type": "booleans",
})

# 7. Null values
add("null_values", {
    "present": "yes",
    "absent": None,
    "type": "nulls",
})

# 8. Empty collections
add("empty_collections", {
    "empty_dict": {},
    "empty_list": [],
    "empty_string": "",
    "type": "empties",
})

# 9. Large integers (tests Int64 handling, not Int32)
add("large_integers", {
    "timestamp_ms": 1700000000000,
    "big_negative": -9007199254740991,
    "small": 42,
    "type": "integers",
})

# 10. Strings with special characters
add("special_chars", {
    "quotes": 'He said "hello"',
    "backslash": "path\\to\\file",
    "newline": "line1\nline2",
    "tab": "col1\tcol2",
    "carriage_return": "before\rafter",
    "type": "special_chars",
})

# 11. Backspace and formfeed (the escaping mismatch we found)
add("backspace_formfeed", {
    "backspace": "before\bafter",
    "formfeed": "before\fafter",
    "type": "control_chars",
})

# 12. Other control characters (below 0x20)
add("low_control_chars", {
    "null_char": "before\x00after",
    "bell": "before\x07after",
    "escape": "before\x1bafter",
    "type": "low_controls",
})

# 13. Arrays with mixed types
add("mixed_arrays", {
    "items": [1, "two", True, None, {"nested": "dict"}, [3, 4]],
    "type": "arrays",
})

# 14. Deeply nested structure
add("deeply_nested", {
    "level1": {
        "level2": {
            "level3": {
                "value": "deep"
            }
        }
    },
    "type": "deep",
})

# 15. Keys that are pure digits (edge case for sorting)
add("digit_keys", {
    "100": "hundred",
    "2": "two",
    "30": "thirty",
    "type": "digit_keys",
})

# 16. Real-world state_sync message shape
add("realistic_state_sync", {
    "type": "state_sync",
    "sessions": {
        "sess-abc123": {
            "project": "voxherd",
            "status": "active",
            "last_summary": "Implemented HMAC signing",
            "activity_type": "writing",
        },
        "sess-def456": {
            "project": "aligned-tools",
            "status": "idle",
            "last_summary": "Tests passing",
            "activity_type": "completed",
        },
    },
    "active_project": "voxherd",
    "last_announced_session_id": "sess-abc123",
})

# 17. Floats
add("float_values", {
    "pi": 3.14159265358979,
    "negative": -0.5,
    "type": "floats",
})

# 18. Unicode keys
add("unicode_keys", {
    "caf\u00e9": "coffee",
    "na\u00efve": "innocent",
    "type": "unicode_keys",
})


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

output = {
    "token": TEST_TOKEN,
    "vectors": vectors,
}

json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
print()  # trailing newline
