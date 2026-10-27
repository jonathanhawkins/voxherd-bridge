#!/usr/bin/env bash
# Compile the voxherd-listen Swift CLI for macOS speech-to-text.
# Produces bridge/stt/voxherd-listen binary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SRC="$PROJECT_DIR/bridge/stt/voxherd-listen.swift"
OUT="$PROJECT_DIR/bridge/stt/voxherd-listen"

if [ ! -f "$SRC" ]; then
    echo "Error: source file not found: $SRC" >&2
    exit 1
fi

# A bare executable has no bundle, so it has no Info.plist -- and macOS kills
# any process that touches the microphone or the Speech framework without the
# matching usage strings. It does so with SIGABRT and *nothing on stderr*: the
# explanation goes to the crash reporter, so from the bridge's side the child
# simply vanished. `mac_stt.listen()` saw no output and returned None, which is
# indistinguishable from silence, so the wake word appeared to be listening and
# hearing nothing. The fix is to embed a real Info.plist in the binary's
# __TEXT,__info_plist section, which is what `_spawn_disclaimed` in
# bridge/mac_stt.py exists to have TCC evaluate: it disclaims responsibility so
# the child is judged on its own usage strings rather than on Python's.
#
# NSMicrophoneUsageDescription alone is not enough -- SFSpeechRecognizer needs
# NSSpeechRecognitionUsageDescription, and that is the one that was missing.
PLIST="$(mktemp -t voxherd-listen-plist).plist"
cat > "$PLIST" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.voxherd.listen</string>
    <key>CFBundleName</key>
    <string>VoxHerd Listen</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>VoxHerd listens for spoken commands to your coding agents.</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>VoxHerd transcribes your spoken commands so it can route them to the right coding agent.</string>
</dict>
</plist>
PLIST_EOF

echo "Compiling voxherd-listen..."
swiftc -O -o "$OUT" "$SRC" \
    -framework Speech -framework AVFoundation -framework CoreAudio \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$PLIST"

# TCC keys a remembered grant to the code signature, so an unsigned binary
# would re-prompt (or silently fail) on every rebuild.
codesign --force --sign - "$OUT"
rm -f "$PLIST"

chmod +x "$OUT"

# Fail loudly here rather than at 4am through a wake word that hears nothing.
if ! otool -s __TEXT __info_plist "$OUT" >/dev/null 2>&1 \
   || ! otool -X -s __TEXT __info_plist "$OUT" | grep -q .; then
    echo "Error: Info.plist was not embedded -- TCC will SIGABRT this binary." >&2
    exit 1
fi
echo "Built: $OUT"
echo "Test with: $OUT --timeout 5"
echo "Mic test: $OUT --mic-test --timeout 10"
