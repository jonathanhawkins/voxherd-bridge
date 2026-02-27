#!/usr/bin/env bash
# Build the complete VoxHerd Bridge macOS application.
#
# Steps:
#   1. Build the Python bridge binary with PyInstaller
#   2. Build the Swift menu bar app with xcodebuild
#   3. Copy the PyInstaller output into the app bundle's Resources
#   4. Copy hook scripts into the app bundle's Resources
#   5. Sign everything with Developer ID for distribution
#
# Usage:
#   bash macos/build-app.sh              # Release build (Developer ID signed)
#   bash macos/build-app.sh --debug      # Debug build (ad-hoc signed)
#
# Output: macos/dist/VoxHerdBridge.app

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DERIVED_DATA="/tmp/vh-macos-build"
APP_OUTPUT="$SCRIPT_DIR/dist/VoxHerdBridge.app"
ENTITLEMENTS="$SCRIPT_DIR/VoxHerdBridge/VoxHerdBridge.entitlements"
# Set your own Developer ID signing identity here, or override via VOXHERD_SIGN_IDENTITY env var.
# Example: "Developer ID Application: Your Name (YOUR_TEAM_ID)"
SIGN_IDENTITY="${VOXHERD_SIGN_IDENTITY:-Developer ID Application}"
DEBUG_MODE=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --debug) DEBUG_MODE=true; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if $DEBUG_MODE; then
    SIGN_IDENTITY="-"
    echo "=== VoxHerd Bridge App Builder (DEBUG) ==="
else
    echo "=== VoxHerd Bridge App Builder (RELEASE) ==="
fi
echo ""

# Step 1: Build the Python bridge binary
echo "--- Step 1: Building Python bridge binary ---"
bash "$SCRIPT_DIR/build-bridge.sh"
echo ""

# Verify PyInstaller output
BRIDGE_DIST="$SCRIPT_DIR/dist/voxherd-bridge"
if [ ! -x "$BRIDGE_DIST/voxherd-bridge" ]; then
    echo "ERROR: PyInstaller build failed - binary not found at $BRIDGE_DIST/voxherd-bridge"
    exit 1
fi

# Step 2: Build the Swift menu bar app
echo "--- Step 2: Building Swift menu bar app ---"

# Regenerate Xcode project
if command -v xcodegen &>/dev/null; then
    cd "$SCRIPT_DIR/VoxHerdBridge"
    xcodegen generate
    cd "$SCRIPT_DIR"
else
    echo "WARNING: xcodegen not found, using existing .xcodeproj"
fi

CONFIGURATION="Release"
if $DEBUG_MODE; then
    CONFIGURATION="Debug"
fi

xcodebuild \
    -project "$SCRIPT_DIR/VoxHerdBridge/VoxHerdBridge.xcodeproj" \
    -scheme VoxHerdBridge \
    -configuration "$CONFIGURATION" \
    -derivedDataPath "$DERIVED_DATA" \
    -quiet

# Find the built app
BUILT_APP="$DERIVED_DATA/Build/Products/$CONFIGURATION/VoxHerdBridge.app"
if [ ! -d "$BUILT_APP" ]; then
    echo "ERROR: xcodebuild failed - app not found at $BUILT_APP"
    exit 1
fi
echo ""

# Step 3: Assemble the final app bundle
echo "--- Step 3: Assembling app bundle ---"

# Clean previous output
rm -rf "$APP_OUTPUT"
cp -R "$BUILT_APP" "$APP_OUTPUT"

# Create Resources directory
RESOURCES="$APP_OUTPUT/Contents/Resources"
mkdir -p "$RESOURCES"

# Copy PyInstaller bridge binary into Resources
echo "  Copying bridge binary..."
cp -R "$BRIDGE_DIST" "$RESOURCES/voxherd-bridge"

# Step 4: Copy hook scripts into Resources
echo "  Copying hook scripts..."
if [ -d "$PROJECT_DIR/hooks" ]; then
    cp -R "$PROJECT_DIR/hooks" "$RESOURCES/hooks"
    chmod +x "$RESOURCES/hooks/"*.sh 2>/dev/null || true
fi

# Step 5: Code signing
echo ""
echo "--- Step 5: Code signing ---"

if [ "$SIGN_IDENTITY" = "-" ]; then
    echo "  Ad-hoc signing (debug mode)..."
    codesign --force --deep --sign - "$APP_OUTPUT"
else
    # Verify the signing identity exists
    if ! security find-identity -v -p codesigning | grep -q "$SIGN_IDENTITY"; then
        echo "ERROR: Signing identity not found: $SIGN_IDENTITY"
        echo ""
        echo "You need a Developer ID Application certificate."
        echo "Go to: https://developer.apple.com/account/resources/certificates"
        echo ""
        echo "Set your identity via environment variable:"
        echo "  export VOXHERD_SIGN_IDENTITY=\"Developer ID Application: Your Name (TEAM_ID)\""
        echo ""
        echo "For a debug build without signing: bash macos/build-app.sh --debug"
        exit 1
    fi

    echo "  Signing identity: $SIGN_IDENTITY"

    # Sign inner binaries first (inside-out signing order)
    # 1. Sign all .dylib and .so files in the PyInstaller bundle
    echo "  Signing PyInstaller dylibs..."
    find "$RESOURCES/voxherd-bridge" -type f \( -name "*.dylib" -o -name "*.so" \) | while read -r lib; do
        codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" \
            --entitlements "$ENTITLEMENTS" "$lib" 2>/dev/null || true
    done

    # 2. Sign frameworks inside the PyInstaller bundle
    echo "  Signing bundled frameworks..."
    find "$RESOURCES/voxherd-bridge" -type d -name "*.framework" | while read -r fw; do
        codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" \
            --entitlements "$ENTITLEMENTS" "$fw" 2>/dev/null || true
    done

    # 3. Sign all executable binaries in the PyInstaller bundle (catches STT binary etc.)
    echo "  Signing executable binaries..."
    find "$RESOURCES/voxherd-bridge" -type f -perm +111 ! -name "*.dylib" ! -name "*.so" | while read -r bin; do
        codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" \
            --entitlements "$ENTITLEMENTS" "$bin" 2>/dev/null || true
    done

    # 4. Sign the main PyInstaller binary (re-sign to ensure it's on top)
    echo "  Signing bridge binary..."
    codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" \
        --entitlements "$ENTITLEMENTS" "$RESOURCES/voxherd-bridge/voxherd-bridge"

    # 5. Sign the outer app bundle
    echo "  Signing app bundle..."
    codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" \
        --entitlements "$ENTITLEMENTS" "$APP_OUTPUT"

    # 6. Verify
    echo "  Verifying signature..."
    if codesign --verify --strict "$APP_OUTPUT" 2>&1; then
        echo "  Signature valid."
    else
        echo "WARNING: Signature verification had issues (may still work)."
    fi

    if spctl --assess --type execute "$APP_OUTPUT" 2>&1; then
        echo "  Gatekeeper: approved."
    else
        echo "  Gatekeeper: not yet approved (needs notarization)."
    fi
fi

echo ""
echo "=== Build successful ==="
echo "Output: $APP_OUTPUT"
SIZE=$(du -sh "$APP_OUTPUT" | cut -f1)
echo "Size: $SIZE"
echo ""
if [ "$SIGN_IDENTITY" != "-" ]; then
    echo "Next steps:"
    echo "  1. Create DMG:  bash macos/create-dmg.sh"
    echo "  2. Notarize:    bash macos/notarize.sh macos/dist/VoxHerdBridge-X.Y.Z.dmg"
    echo "  3. Or all-in-one: bash scripts/release-macos.sh"
else
    echo "To install: cp -R $APP_OUTPUT /Applications/"
    echo "To test: open $APP_OUTPUT"
fi
