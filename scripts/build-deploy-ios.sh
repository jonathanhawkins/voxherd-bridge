#!/usr/bin/env bash
# Build the VoxHerd iOS app and deploy it to a connected iPhone.
#
# Usage:
#   bash scripts/build-deploy-ios.sh              # build + install on the connected iPhone (auto-detected)
#   bash scripts/build-deploy-ios.sh --build-only # build, skip install
#   bash scripts/build-deploy-ios.sh --device <id> # target a specific device UDID
#
# Device selection (no UDID is hardcoded so this script is safe to publish):
#   --device <id>  >  $VOXHERD_DEVICE_ID env var  >  auto-detect the single
#   connected iPhone via `xcrun devicectl list devices`.
#
# Requires:
#   - ios/VoxHerd/Secrets.xcconfig with CLIENT_TOKEN (gitignored; ask the user
#     for the Meta DAT client token if missing)
#   - Apple Developer signing identity in Keychain
#   - An iPhone unlocked, plugged in, and trusting this Mac

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IOS_DIR="$PROJECT_DIR/ios/VoxHerd"
DERIVED_DATA="/tmp/vh-build"
APP_PATH="$DERIVED_DATA/Build/Products/Debug-iphoneos/VoxHerd.app"
# Device UDID is resolved at install time (flag > env > auto-detect). Never
# hardcode a UDID here — scripts/ syncs to the public repo, guarded by
# bridge/tests/test_sensitive_data.py.
DEVICE_ID="${VOXHERD_DEVICE_ID:-}"
BUILD_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build-only) BUILD_ONLY=true; shift ;;
        --device) DEVICE_ID="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,17p' "$0"
            exit 0
            ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Sanity check: Secrets.xcconfig must exist or xcodebuild fails with a confusing
# "Unable to open base configuration reference file" error.
if [ ! -f "$IOS_DIR/Secrets.xcconfig" ]; then
    echo "ERROR: $IOS_DIR/Secrets.xcconfig is missing."
    echo ""
    echo "Copy the template and add your Meta DAT CLIENT_TOKEN (Meta Developer Portal → app → Client Token):"
    echo "  cp $IOS_DIR/Secrets.xcconfig.template $IOS_DIR/Secrets.xcconfig"
    echo "  \$EDITOR $IOS_DIR/Secrets.xcconfig"
    exit 1
fi

echo "=== Building VoxHerd iOS ==="
# -allowProvisioningUpdates lets xcodebuild refresh the cached automatic-signing
# profile from Apple. Without it, a stale local profile causes:
#   "Provisioning profile ... doesn't include signing certificate ..."
xcodebuild \
    -project "$IOS_DIR/VoxHerd.xcodeproj" \
    -scheme VoxHerd \
    -sdk iphoneos \
    -configuration Debug \
    -derivedDataPath "$DERIVED_DATA" \
    -allowProvisioningUpdates \
    -quiet

if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: build succeeded but $APP_PATH was not produced."
    exit 1
fi

echo "Build output: $APP_PATH"

if $BUILD_ONLY; then
    exit 0
fi

# Resolve the target device if none was passed via --device / $VOXHERD_DEVICE_ID.
# `devicectl device install --device` accepts the CoreDevice identifier shown
# in `devicectl list devices` (a standard 8-4-4-4-12 UUID), so we grab the
# first CONNECTED device's identifier. This avoids hardcoding the permanent
# hardware UDID entirely.
if [ -z "$DEVICE_ID" ]; then
    DEVICE_ID="$(xcrun devicectl list devices 2>/dev/null \
        | grep -i 'connected' \
        | grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}' \
        | head -1 || true)"
fi
if [ -z "$DEVICE_ID" ]; then
    echo "ERROR: no connected iPhone detected."
    echo "  Plug in + unlock + trust this Mac, then retry — or pass --device <udid>"
    echo "  (or set VOXHERD_DEVICE_ID). List devices: xcrun devicectl list devices"
    exit 1
fi

echo ""
echo "=== Installing to device $DEVICE_ID ==="
xcrun devicectl device install app --device "$DEVICE_ID" "$APP_PATH"

echo ""
echo "=== Done ==="
echo "Launch from the home screen, or stream logs with:"
echo "  idevicesyslog -p VoxHerd --no-color"
