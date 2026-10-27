"""The STT helper must declare its own privacy usage strings.

`voxherd-listen` uses `SFSpeechRecognizer` and `AVAudioEngine`, and macOS kills
any process that touches either without the matching usage description. It does
so with **SIGABRT and nothing on stderr** — the explanation goes to the crash
reporter — so from the bridge's side the child simply vanished:
`mac_stt.listen()` saw no output and returned `None`, which is exactly what it
returns for silence. The wake word therefore looked like it was listening
attentively and hearing nothing, for as long as it took someone to check a
crash log.

`NSMicrophoneUsageDescription` alone is not enough. The Speech framework wants
`NSSpeechRecognitionUsageDescription`, and that was the one missing — from both
the helper's embedded plist and the containing app bundle, which is what TCC
attributes a nested helper to.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

_BINARY = Path(__file__).resolve().parents[1] / "stt" / "voxherd-listen"

_REQUIRED = ("NSMicrophoneUsageDescription", "NSSpeechRecognitionUsageDescription")

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not _BINARY.exists(),
    reason="macOS-only; requires scripts/build-stt.sh to have run",
)


def _embedded_plist_text(path: Path) -> str:
    """The binary's `__TEXT,__info_plist` section, as text.

    `otool` prints little-endian words, so each 4-byte group has to be reversed
    before the bytes mean anything — reading them in printed order yields
    convincing-looking garbage that contains none of the keys.

    Matched as text rather than parsed as a plist on purpose: otool's dump of
    the section is not always terminated on a word boundary, so the closing
    tag can be clipped and a strict parse fails on a binary that is perfectly
    correct. The question here is only whether the keys are declared.
    """
    out = subprocess.run(
        ["otool", "-X", "-s", "__TEXT", "__info_plist", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    raw = bytearray()
    for line in out.splitlines():
        for word in line.split()[1:]:
            if re.fullmatch(r"[0-9a-fA-F]{8}", word):
                raw += bytes.fromhex(word)[::-1]
    text = raw.decode("utf-8", "ignore")
    assert "<plist" in text, "no Info.plist embedded in __TEXT,__info_plist"
    return text


@pytest.mark.parametrize("key", _REQUIRED)
def test_binary_declares_usage_description(key):
    text = _embedded_plist_text(_BINARY)
    assert f"<key>{key}</key>" in text, (
        f"{key} missing from voxherd-listen's embedded Info.plist — macOS will "
        f"SIGABRT it on first use, silently. Rebuild with scripts/build-stt.sh."
    )


def test_binary_is_code_signed():
    # TCC keys a remembered grant to the signature; unsigned means re-prompting
    # (or silently failing) after every rebuild.
    result = subprocess.run(
        ["codesign", "--verify", "--strict", str(_BINARY)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"voxherd-listen is not validly signed: {result.stderr}"


@pytest.mark.parametrize("key", _REQUIRED)
def test_app_bundle_declares_usage_description(key):
    # TCC attributes a helper nested inside an .app to that .app, so the key is
    # needed here too -- the embedded one alone did not stop the crash.
    project = Path(__file__).resolve().parents[2] / "macos/VoxHerdBridge/project.yml"
    if not project.exists():
        pytest.skip("macOS app project not present")
    assert f"INFOPLIST_KEY_{key}" in project.read_text(), (
        f"INFOPLIST_KEY_{key} missing from the menu bar app's project.yml"
    )
