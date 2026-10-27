"""The wake phrase: "hey agent", with "hey claude" kept as an alias.

"Hey Claude" named one of the three assistants VoxHerd drives, which was wrong
for anyone driving the other two. "Hey agent" names none of them; which session
hears the command is decided by what the sentence names, then by focus.

"agent" is an ordinary word in a developer's day, so the phrase only counts at
the start of an utterance and as whole words: "the agent said" and "hey agents"
must not wake anything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bridge.mac_voice_loop import WAKE_PHRASES, _strip_wake_word

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("heard", "rest"),
    [
        ("hey agent", ""),
        ("Hey agent, run the tests", "run the tests"),
        # The recogniser punctuates a greeting as one.
        ("Hey, agent. Run the tests.", "Run the tests."),
        ("OK agent what's checkout doing", "what's checkout doing"),
        ("okay agent: approve", "approve"),
        ("  hey agent war room", "war room"),
        # A flag after the phrase is the command's, not punctuation.
        ("Hey agent, --background", "--background"),
        # The old phrase still works.
        ("Hey Claude, war room", "war room"),
        ("hey clod war room", "war room"),
    ],
)
def test_a_wake_phrase_is_stripped(heard: str, rest: str) -> None:
    assert _strip_wake_word(heard) == rest


@pytest.mark.parametrize(
    "heard",
    [
        "the agent said the tests pass",
        "hey agents are running",
        "hey agentic workflows",
        "tell the agent hey agent",
        "agent run the tests",
        "hey claudette",
        "",
    ],
)
def test_anything_else_is_not_addressed(heard: str) -> None:
    assert _strip_wake_word(heard) is None


def _quoted(source: str, start: str, end: str) -> list[str]:
    block = source[source.index(start):]
    return re.findall(r'"([^"]+)"', block[: block.index(end)])


KOTLIN_INTENT = ROOT / "android/core/src/main/kotlin/com/voxherd/core/VoiceIntent.kt"
KOTLIN_MIC_WORDS = ROOT / "android/core/src/main/kotlin/com/voxherd/core/MicWords.kt"
SWIFT_PHRASES = ROOT / "ios/VoxHerd/VoxHerd/Voice/WakePhrase.swift"

# The client sources live only in the monorepo; the public bridge repo has
# the bridge alone, and these would fail there on a missing file.
needs_clients = pytest.mark.skipif(
    not (KOTLIN_INTENT.exists() and SWIFT_PHRASES.exists()),
    reason="client sources are not in this checkout",
)


@needs_clients
def test_every_client_takes_the_same_phrases() -> None:
    """A phrase that works at the desk and fails on your face, for no reason the
    wearer can see, is the failure the three lists exist to avoid."""
    kotlin = KOTLIN_INTENT.read_text()
    swift = SWIFT_PHRASES.read_text()
    assert _quoted(kotlin, "val WAKE_PHRASES = listOf(", ")") == WAKE_PHRASES
    assert _quoted(swift, "static let phrases = [", "]") == WAKE_PHRASES


@needs_clients
def test_the_phrase_the_headset_quotes_is_the_first_one() -> None:
    mic_words = KOTLIN_MIC_WORDS.read_text()
    assert re.search(r'const val WAKE_HINT = "([^"]+)"', mic_words).group(1) == WAKE_PHRASES[0]
