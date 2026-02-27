#!/usr/bin/env bash
# Build the VoxHerd bridge as a standalone frozen binary using PyInstaller.
# Output: macos/dist/voxherd-bridge/voxherd-bridge

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BRIDGE_DIR="$PROJECT_DIR/bridge"
VENV_DIR="$BRIDGE_DIR/.venv"

echo "=== VoxHerd Bridge Builder ==="
echo "Project: $PROJECT_DIR"
echo ""

# Ensure the venv exists
if [ ! -d "$VENV_DIR" ]; then
    echo "ERROR: Python venv not found at $VENV_DIR"
    echo "Run: cd bridge && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Use venv's python/pip directly (more reliable than source activate in scripts)
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

# Install PyInstaller if not present
if ! "$PYTHON" -m PyInstaller --version &>/dev/null; then
    echo "Installing PyInstaller..."
    "$PIP" install pyinstaller
fi

# Ensure the STT binary is compiled
STT_BINARY="$BRIDGE_DIR/stt/voxherd-listen"
if [ ! -x "$STT_BINARY" ]; then
    echo "Building STT binary..."
    if [ -f "$PROJECT_DIR/scripts/build-stt.sh" ]; then
        bash "$PROJECT_DIR/scripts/build-stt.sh"
    else
        echo "WARNING: STT binary not found and build script missing. Bridge will work but without Mac STT."
    fi
fi

# Clean previous build
rm -rf "$SCRIPT_DIR/dist/voxherd-bridge" "$SCRIPT_DIR/build"

echo "Running PyInstaller..."
"$PYTHON" -m PyInstaller \
    --distpath "$SCRIPT_DIR/dist" \
    --workpath "$SCRIPT_DIR/build" \
    --noconfirm \
    "$SCRIPT_DIR/voxherd-bridge.spec"

# Verify the output
OUTPUT="$SCRIPT_DIR/dist/voxherd-bridge/voxherd-bridge"
if [ -x "$OUTPUT" ]; then
    echo ""
    echo "=== Build successful ==="
    echo "Output: $OUTPUT"
    SIZE=$(du -sh "$SCRIPT_DIR/dist/voxherd-bridge" | cut -f1)
    echo "Size: $SIZE"
    echo ""
    echo "Test with: $OUTPUT run --headless --tts"
else
    echo "ERROR: Build failed - output binary not found"
    exit 1
fi
