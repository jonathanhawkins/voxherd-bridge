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

echo "Compiling voxherd-listen..."
swiftc -O -o "$OUT" "$SRC" -framework Speech -framework AVFoundation -framework CoreAudio

chmod +x "$OUT"
echo "Built: $OUT"
echo "Test with: $OUT --timeout 5"
echo "Mic test: $OUT --mic-test --timeout 10"
