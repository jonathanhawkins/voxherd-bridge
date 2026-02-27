#!/usr/bin/env bash
# Create a distributable DMG for VoxHerd Bridge.
#
# Usage: bash macos/create-dmg.sh [--version X.Y.Z]
#
# Requires: create-dmg (brew install create-dmg)
# Input:    macos/dist/VoxHerdBridge.app (from build-app.sh)
# Output:   macos/dist/VoxHerdBridge-X.Y.Z.dmg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH="$SCRIPT_DIR/dist/VoxHerdBridge.app"
VERSION=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Validate app exists
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: App not found at $APP_PATH"
    echo "Run: bash macos/build-app.sh"
    exit 1
fi

# Read version from Info.plist if not provided
if [ -z "$VERSION" ]; then
    VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP_PATH/Contents/Info.plist" 2>/dev/null || echo "1.0.0")
fi

DMG_NAME="VoxHerdBridge-${VERSION}.dmg"
DMG_PATH="$SCRIPT_DIR/dist/$DMG_NAME"
VOLUME_NAME="VoxHerd Bridge $VERSION"

echo "=== Creating DMG ==="
echo "App:     $APP_PATH"
echo "Version: $VERSION"
echo "Output:  $DMG_PATH"
echo ""

# Remove previous DMG if it exists
rm -f "$DMG_PATH"

# Check for create-dmg
if command -v create-dmg &>/dev/null; then
    echo "Using create-dmg for polished layout..."

    create-dmg \
        --volname "$VOLUME_NAME" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "VoxHerdBridge.app" 150 190 \
        --app-drop-link 450 190 \
        --hide-extension "VoxHerdBridge.app" \
        --no-internet-enable \
        "$DMG_PATH" \
        "$APP_PATH"
else
    echo "create-dmg not found, using hdiutil fallback..."
    echo "(Install create-dmg for a nicer DMG: brew install create-dmg)"
    echo ""

    # Create a temporary directory for DMG contents
    STAGING=$(mktemp -d)
    trap 'rm -rf "$STAGING"' EXIT

    cp -R "$APP_PATH" "$STAGING/"
    ln -s /Applications "$STAGING/Applications"

    hdiutil create \
        -volname "$VOLUME_NAME" \
        -srcfolder "$STAGING" \
        -ov \
        -format UDZO \
        -imagekey zlib-level=9 \
        "$DMG_PATH"
fi

# Verify
if [ -f "$DMG_PATH" ]; then
    SIZE=$(du -sh "$DMG_PATH" | cut -f1)
    SHA256=$(shasum -a 256 "$DMG_PATH" | cut -d' ' -f1)
    echo ""
    echo "=== DMG created ==="
    echo "File:   $DMG_PATH"
    echo "Size:   $SIZE"
    echo "SHA256: $SHA256"
    echo ""
    echo "Next: notarize with 'bash macos/notarize.sh $DMG_PATH'"
else
    echo "ERROR: DMG creation failed"
    exit 1
fi
