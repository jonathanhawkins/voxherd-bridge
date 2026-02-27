#!/usr/bin/env bash
# Notarize a VoxHerd Bridge .app or .dmg with Apple.
#
# Usage:
#   bash macos/notarize.sh macos/dist/VoxHerdBridge-1.0.0.dmg
#   bash macos/notarize.sh macos/dist/VoxHerdBridge.app
#
# Prerequisites:
#   xcrun notarytool store-credentials "VoxHerd" \
#     --apple-id YOUR_APPLE_ID \
#     --team-id YOUR_TEAM_ID
#
# This stores an app-specific password in the keychain. Generate one at:
#   https://appleid.apple.com/account/manage → App-Specific Passwords

set -euo pipefail

KEYCHAIN_PROFILE="VoxHerd"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <path-to-.app-or-.dmg>"
    exit 1
fi

INPUT_PATH="$1"

if [ ! -e "$INPUT_PATH" ]; then
    echo "ERROR: File not found: $INPUT_PATH"
    exit 1
fi

# Determine if we need to zip first (notarytool needs zip/dmg/pkg)
SUBMIT_PATH="$INPUT_PATH"
TEMP_ZIP=""

if [ -d "$INPUT_PATH" ] && [[ "$INPUT_PATH" == *.app ]]; then
    echo "Zipping .app for submission..."
    TEMP_ZIP=$(mktemp /tmp/voxherd-notarize-XXXXXX.zip)
    ditto -c -k --keepParent "$INPUT_PATH" "$TEMP_ZIP"
    SUBMIT_PATH="$TEMP_ZIP"
fi

cleanup() {
    if [ -n "$TEMP_ZIP" ] && [ -f "$TEMP_ZIP" ]; then
        rm -f "$TEMP_ZIP"
    fi
}
trap cleanup EXIT

echo "=== Notarizing VoxHerd Bridge ==="
echo "Submitting: $SUBMIT_PATH"
echo ""

# Submit and wait
RESULT=$(xcrun notarytool submit "$SUBMIT_PATH" \
    --keychain-profile "$KEYCHAIN_PROFILE" \
    --wait \
    --output-format json 2>&1) || true

echo "$RESULT"
echo ""

# Parse the submission ID and status
SUBMISSION_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")

if [ "$STATUS" = "Accepted" ]; then
    echo "Notarization ACCEPTED!"
    echo ""

    # Staple the ticket
    echo "Stapling ticket..."
    if [[ "$INPUT_PATH" == *.dmg ]] || [ -d "$INPUT_PATH" ]; then
        xcrun stapler staple "$INPUT_PATH"
        echo "Stapled successfully."
    else
        echo "Skipping staple (not a .app or .dmg)"
    fi

    # Verify
    echo ""
    echo "Verifying..."
    if [ -d "$INPUT_PATH" ]; then
        spctl --assess --type execute -v "$INPUT_PATH" 2>&1 || true
    elif [[ "$INPUT_PATH" == *.dmg ]]; then
        spctl --assess --type open --context context:primary-signature -v "$INPUT_PATH" 2>&1 || true
    fi

    echo ""
    echo "=== Notarization complete ==="
else
    echo "Notarization FAILED (status: $STATUS)"
    echo ""

    # Fetch the log for debugging
    if [ -n "$SUBMISSION_ID" ]; then
        echo "--- Notarization log ---"
        xcrun notarytool log "$SUBMISSION_ID" \
            --keychain-profile "$KEYCHAIN_PROFILE" 2>&1 || true
    fi

    echo ""
    echo "Common issues:"
    echo "  - Missing Developer ID signing: re-run build-app.sh (not --debug)"
    echo "  - Unsigned dylibs: check PyInstaller bundle signing"
    echo "  - Missing hardened runtime: ensure --options runtime in codesign"
    exit 1
fi
