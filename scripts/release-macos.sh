#!/usr/bin/env bash
# End-to-end macOS release: build → sign → DMG → notarize.
#
# Usage:
#   bash scripts/release-macos.sh                    # Full pipeline
#   bash scripts/release-macos.sh --version 1.2.0    # Override version
#   bash scripts/release-macos.sh --skip-build        # Notarize existing build
#
# Prerequisites:
#   1. Developer ID Application certificate installed
#   2. Notarytool credentials stored:
#      xcrun notarytool store-credentials "VoxHerd" \
#        --apple-id YOUR_APPLE_ID --team-id YOUR_TEAM_ID
#   3. create-dmg installed (brew install create-dmg) [optional, hdiutil fallback]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MACOS_DIR="$PROJECT_DIR/macos"
SKIP_BUILD=false
VERSION=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-build) SKIP_BUILD=true; shift ;;
        --version) VERSION="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--version X.Y.Z] [--skip-build]"
            exit 0
            ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

echo "============================================"
echo "  VoxHerd Bridge — macOS Release Pipeline"
echo "============================================"
echo ""

# Pre-flight checks
echo "--- Pre-flight checks ---"

# Check for Developer ID cert
if ! security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application"; then
    echo "ERROR: No Developer ID Application certificate found."
    echo ""
    echo "To fix:"
    echo "  1. Go to https://developer.apple.com/account/resources/certificates"
    echo "  2. Create a 'Developer ID Application' certificate"
    echo "  3. Download and install it"
    exit 1
fi
echo "  Developer ID cert: found"

# Check for notarytool credentials
if ! xcrun notarytool store-credentials --help &>/dev/null; then
    echo "ERROR: notarytool not available. Update Xcode command line tools."
    exit 1
fi
echo "  notarytool: available"

# Check for create-dmg (optional)
if command -v create-dmg &>/dev/null; then
    echo "  create-dmg: found"
else
    echo "  create-dmg: not found (will use hdiutil fallback)"
fi

echo ""

# Step 1: Build
if $SKIP_BUILD; then
    echo "--- Skipping build (--skip-build) ---"
    if [ ! -d "$MACOS_DIR/dist/VoxHerdBridge.app" ]; then
        echo "ERROR: No existing app at $MACOS_DIR/dist/VoxHerdBridge.app"
        exit 1
    fi
else
    echo "--- Step 1/3: Building and signing app ---"
    bash "$MACOS_DIR/build-app.sh"
fi
echo ""

# Read version
APP_PATH="$MACOS_DIR/dist/VoxHerdBridge.app"
if [ -z "$VERSION" ]; then
    VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
        "$APP_PATH/Contents/Info.plist" 2>/dev/null || echo "1.0.0")
fi
echo "Version: $VERSION"
echo ""

# Step 2: Create DMG
echo "--- Step 2/3: Creating DMG ---"
DMG_ARGS=()
if [ -n "$VERSION" ]; then
    DMG_ARGS+=(--version "$VERSION")
fi
bash "$MACOS_DIR/create-dmg.sh" "${DMG_ARGS[@]}"
echo ""

DMG_PATH="$MACOS_DIR/dist/VoxHerdBridge-${VERSION}.dmg"
if [ ! -f "$DMG_PATH" ]; then
    echo "ERROR: DMG not found at $DMG_PATH"
    exit 1
fi

# Step 3: Notarize
echo "--- Step 3/3: Notarizing DMG ---"
bash "$MACOS_DIR/notarize.sh" "$DMG_PATH"
echo ""

# Final summary
SHA256=$(shasum -a 256 "$DMG_PATH" | cut -d' ' -f1)
SIZE=$(du -sh "$DMG_PATH" | cut -f1)

echo "============================================"
echo "  Release complete!"
echo "============================================"
echo ""
echo "  File:    $DMG_PATH"
echo "  Version: $VERSION"
echo "  Size:    $SIZE"
echo "  SHA256:  $SHA256"
echo ""
echo "  Distribution:"
echo "    - Upload to GitHub Releases"
echo "    - Upload to voxherd.com/download"
echo "    - Homebrew cask: brew install --cask voxherd"
echo ""
