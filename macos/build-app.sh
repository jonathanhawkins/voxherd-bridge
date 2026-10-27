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
#   bash macos/build-app.sh                       # Release build (Developer ID signed)
#   bash macos/build-app.sh --debug               # Debug build (ad-hoc signed)
#   bash macos/build-app.sh --debug --install     # Build, kill running app, replace /Applications copy, relaunch
#
# Output: macos/dist/VoxHerdBridge.app

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DERIVED_DATA="/tmp/vh-macos-build"
APP_OUTPUT="$SCRIPT_DIR/dist/VoxHerdBridge.app"
INSTALL_DEST="/Applications/VoxHerdBridge.app"
ENTITLEMENTS="$SCRIPT_DIR/VoxHerdBridge/VoxHerdBridge.entitlements"
# Set your own Developer ID signing identity here, or override via VOXHERD_SIGN_IDENTITY env var.
# Example: "Developer ID Application: Your Name (YOUR_TEAM_ID)"
SIGN_IDENTITY="${VOXHERD_SIGN_IDENTITY:-Developer ID Application}"
DEBUG_MODE=false
INSTALL_MODE=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --debug) DEBUG_MODE=true; shift ;;
        --install) INSTALL_MODE=true; shift ;;
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

# In --debug mode we re-sign ad-hoc in Step 5, so disable Xcode's own signing
# during the build itself. This avoids a "No 'Mac Development' signing certificate"
# error when the dev environment only has an "Apple Development" cert configured.
XCODE_SIGN_ARGS=()
if $DEBUG_MODE; then
    XCODE_SIGN_ARGS=(
        CODE_SIGNING_REQUIRED=NO
        CODE_SIGNING_ALLOWED=NO
        # No separate "debug dylib". Xcode 15+ splits a Debug build into a thin
        # executable plus VoxHerdBridge.debug.dylib, and the hardened runtime
        # (Step 5) enables library validation, which refuses to map a dylib
        # whose signature does not share a Team ID with the process. An ad-hoc
        # signature has no Team ID, so the app died at launch with
        #   Library not loaded: @rpath/VoxHerdBridge.debug.dylib
        #   ... mapping process and mapped file (non-platform) have different Team IDs
        # Linking it in means there is no second image to validate, so the debug
        # build keeps the hardened runtime instead of having to give it up.
        ENABLE_DEBUG_DYLIB=NO
    )
fi

# The version the app reports. project.yml holds a dev placeholder, and a
# release used to ship it: `release-macos.sh --version` named the DMG but never
# reached the build, so a 1.3.0 DMG held an app calling itself 0.1.0-dev.
XCODE_VERSION_ARGS=()
if [ -n "${VOXHERD_VERSION:-}" ]; then
    XCODE_VERSION_ARGS=(MARKETING_VERSION="$VOXHERD_VERSION")
fi

# Strip the release binary, as an Archive does. Release already resolves
# STRIP_INSTALLED_PRODUCT=YES and DEBUG_INFORMATION_FORMAT=dwarf-with-dsym, but
# Xcode only strips with DEPLOYMENT_POSTPROCESSING on, so the shipped binary
# kept its debug map: one /Users/<name>/... object-file path per source file
# (14 in 1.3.0-beta.2). dsymutil runs before the strip, so the dSYM still has
# everything; Step 3 keeps a copy of it for symbolicating crash reports.
XCODE_STRIP_ARGS=()
if ! $DEBUG_MODE; then
    XCODE_STRIP_ARGS=(DEPLOYMENT_POSTPROCESSING=YES)
fi

# No coverage instrumentation in an app that ships. The scheme's build
# settings resolve CLANG_COVERAGE_MAPPING=YES, so the 1.3.0-beta.1 binary
# carried __llvm_prf/__llvm_covmap sections and ~900 copies of this machine's
# source paths (/Users/<name>/...) for anyone who downloaded it. `xcodebuild
# test` turns coverage on for itself; nothing built here is a test run.
xcodebuild \
    -project "$SCRIPT_DIR/VoxHerdBridge/VoxHerdBridge.xcodeproj" \
    -scheme VoxHerdBridge \
    -configuration "$CONFIGURATION" \
    -derivedDataPath "$DERIVED_DATA" \
    CLANG_COVERAGE_MAPPING=NO \
    ENABLE_CODE_COVERAGE=NO \
    ${XCODE_SIGN_ARGS[@]+"${XCODE_SIGN_ARGS[@]}"} \
    ${XCODE_VERSION_ARGS[@]+"${XCODE_VERSION_ARGS[@]}"} \
    ${XCODE_STRIP_ARGS[@]+"${XCODE_STRIP_ARGS[@]}"} \
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

# The stripped binary's symbols live only in the dSYM now, and its UUID must
# match the build that shipped, so keep one per version beside the DMG (dist/
# is gitignored; the dSYM names this machine's paths and is not for release).
if ! $DEBUG_MODE && [ -d "$BUILT_APP.dSYM" ]; then
    DSYM_OUTPUT="$SCRIPT_DIR/dist/VoxHerdBridge-${VOXHERD_VERSION:-dev}.app.dSYM"
    rm -rf "$DSYM_OUTPUT"
    cp -R "$BUILT_APP.dSYM" "$DSYM_OUTPUT"
    echo "  dSYM kept at $DSYM_OUTPUT"
fi

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
    # hooks/install.sh installs the shared model-profile resolver next to
    # on-stop.py, and looks for it in ../bridge. There is no bridge/ source
    # tree inside the bundle, so stage a copy alongside the hooks.
    if [ -f "$PROJECT_DIR/bridge/model_profile.py" ]; then
        cp "$PROJECT_DIR/bridge/model_profile.py" "$RESOURCES/hooks/model_profile.py"
    fi
fi
# Bytecode left in hooks/ by local test runs is gitignored but not absent, and
# the copy above (and Xcode's own resources phase) took it along: each .pyc
# names the developer's path in co_filename. Python rebuilds caches on use.
find "$RESOURCES/hooks" -name '__pycache__' -type d -prune -exec rm -r {} + 2>/dev/null || true
find "$RESOURCES/hooks" -name '*.pyc' -delete 2>/dev/null || true

# Step 5: Code signing
echo ""
echo "--- Step 5: Code signing ---"

# One signing routine for both identities. It used to be two branches, and
# they drifted: the ad-hoc branch was a single `codesign ... "$APP_OUTPUT"`,
# which signs the OUTER bundle only. The embedded PyInstaller binary is a
# separate Mach-O launched as its own process, so it kept its own signature --
# measured `flags=0x2(adhoc)`, no `runtime` -- and that is the process which
# spawns `claude`, holds the mic and the network listener. Verified by
# experiment: with DYLD_INSERT_LIBRARIES set, a constructor in an arbitrary
# dylib ran inside the embedded bridge while the same injection against the
# outer app was refused. Inside-out order, hardened runtime on every piece.
#
# Mic/Speech access comes from the Info.plist usage strings, not from
# VoxHerdBridge.entitlements (which is an empty dict) -- --entitlements is
# passed to keep every signature shaped the same.
# Identity flags only; each target appends its own --entitlements below.
sign_args=(--force --options runtime)
# The embedded PyInstaller pieces need their own entitlements when signing
# AD-HOC. `--options runtime` turns on library validation, which requires every
# library a process maps to be signed by the same Team ID or be a platform
# binary -- and an ad-hoc signature has no Team ID at all, so the bundled
# CPython framework was refused at dlopen:
#
#     dlopen(.../_internal/Python): code signature not valid for use in
#     process: mapping process and mapped file (non-platform) have different
#     Team IDs
#
# The GUI wrapper then had no bridge and port 7777 was dead, while Step 6
# printed "Installed and running" because it only calls `open`.
# `voxherd-bridge.entitlements` grants com.apple.security.cs.
# disable-library-validation, which fixes that WITHOUT reopening the hole the
# hardened runtime is on for: DYLD_INSERT_LIBRARIES is governed by a different
# entitlement (allow-dyld-environment-variables) that is not granted. Verified
# by experiment -- an injected dylib's constructor does not run.
#
# Keep that file free of XML comments. AMFI's plist parser is stricter than
# plutil's and rejects them ("Failed to parse entitlements: AMFIUnserializeXML:
# syntax error"), which fails the build at the signing step.
#
# The certificate path does not need any of this: there every piece is signed
# with the same Developer ID, so the Team IDs match and validation passes.
BRIDGE_ENTITLEMENTS="$ENTITLEMENTS"
if [ "$SIGN_IDENTITY" = "-" ]; then
    echo "  Ad-hoc signing (debug mode, hardened runtime on)..."
    # --options runtime even ad-hoc. Without it dyld honours
    # DYLD_INSERT_LIBRARIES and library validation is unenforced -- and this app
    # holds Microphone and Speech Recognition TCC grants and registers itself to
    # launch at login, so an injected dylib inherits the mic grant (the cdhash is
    # unchanged, so TCC never re-prompts) and persists across reboots. This is
    # the documented install path (`build-app.sh --debug --install`), so it is
    # what actually ships to the developer's /Applications.
    sign_args+=(--sign -)
    BRIDGE_ENTITLEMENTS="$SCRIPT_DIR/VoxHerdBridge/voxherd-bridge.entitlements"
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
    sign_args+=(--timestamp --sign "$SIGN_IDENTITY")
fi

# Checked explicitly. The three loops below silence codesign's stderr and
# `|| true` -- a PyInstaller tree holds files codesign cannot sign and the build
# must not die on them -- so a MISSING entitlements file would skip every nested
# dylib with no diagnostic, and only surface at the un-silenced call further
# down. Worth naming because voxherd-bridge.entitlements is a NEW file: a commit
# that stages by name and forgets it leaves the repo in exactly this state.
for ents in "$ENTITLEMENTS" "$BRIDGE_ENTITLEMENTS"; do
    if [ ! -f "$ents" ]; then
        echo "ERROR: entitlements file missing: $ents"
        echo "Both this script and .github/workflows/release.yml reference it."
        exit 1
    fi
done

# Inside-out: the outer seal covers Resources, so it has to be signed last.
inner_args=("${sign_args[@]}" --entitlements "$BRIDGE_ENTITLEMENTS")
outer_args=("${sign_args[@]}" --entitlements "$ENTITLEMENTS")

echo "  Signing PyInstaller dylibs..."
find "$RESOURCES/voxherd-bridge" -type f \( -name "*.dylib" -o -name "*.so" \) | while read -r lib; do
    codesign "${inner_args[@]}" "$lib" 2>/dev/null || true
done

echo "  Signing bundled frameworks..."
find "$RESOURCES/voxherd-bridge" -type d -name "*.framework" | while read -r fw; do
    codesign "${inner_args[@]}" "$fw" 2>/dev/null || true
done

echo "  Signing executable binaries..."
find "$RESOURCES/voxherd-bridge" -type f -perm +111 ! -name "*.dylib" ! -name "*.so" | while read -r bin; do
    codesign "${inner_args[@]}" "$bin" 2>/dev/null || true
done

echo "  Signing bridge binary..."
codesign "${inner_args[@]}" "$RESOURCES/voxherd-bridge/voxherd-bridge"

echo "  Signing app bundle..."
codesign "${outer_args[@]}" "$APP_OUTPUT"

# Verify the hardened runtime is actually on. An `--options runtime` that
# silently failed to apply reads identically to one that worked, and the gap
# above survived precisely because nothing checked.
#
# Over EVERY signed image, not just the two top-level ones. The three loops
# above redirect codesign's stderr to /dev/null and `|| true`, deliberately --
# a PyInstaller tree contains files codesign cannot sign and the build must not
# die on them -- but that also means a dylib whose re-sign genuinely failed is
# invisible. On the certificate path library validation stays ON, so such a
# dylib is refused at its first dlopen: for the tree-sitter grammars that is
# the first /api/file request, long after the DMG was notarized and published.
echo "  Verifying hardened runtime..."
runtime_missing=0
runtime_ok=0
check_runtime() {
    # `|| true`: codesign -d exits non-zero on unsigned code, and under
    # `set -o pipefail` that aborts the script before the error below prints --
    # losing exactly the diagnostic this block exists to produce.
    local flags
    flags=$( { codesign -d --verbose "$1" 2>&1 || true; } | sed -n 's/.*flags=\([^ ]*\).*/\1/p')
    if [[ "$flags" != *runtime* ]]; then
        echo "ERROR: hardened runtime missing on $1 (flags=${flags:-none})"
        runtime_missing=$((runtime_missing + 1))
        return
    fi
    runtime_ok=$((runtime_ok + 1))
    [ -n "${2:-}" ] && echo "    $(basename "$1"): $flags"
    return 0
}

check_runtime "$APP_OUTPUT" show
check_runtime "$RESOURCES/voxherd-bridge/voxherd-bridge" show

# The nested images. Read into an array first: a `while read` in a pipeline
# runs in a subshell on bash 3.2, so the counters would be lost.
nested=()
while IFS= read -r image; do
    nested+=("$image")
done < <(find "$RESOURCES/voxherd-bridge" \
             \( -type f \( -name "*.dylib" -o -name "*.so" \) \) -o \
             \( -type f -perm +111 ! -name "*.dylib" ! -name "*.so" \) 2>/dev/null)
for image in ${nested[@]+"${nested[@]}"}; do
    check_runtime "$image"
done
echo "    $runtime_ok signed images hardened, $runtime_missing missing"

if [ "$runtime_missing" -gt 0 ]; then
    echo "Refusing to ship a bundle whose processes accept injected dylibs."
    exit 1
fi

# The Xcode debug dylib, which is how this bug class first appeared: the outer
# app died at launch with a Team-ID error because Xcode 15+ emits a separate
# `VoxHerdBridge.debug.dylib` for Debug builds. `ENABLE_DEBUG_DYLIB=NO` on the
# xcodebuild command line fixes it, but an UNKNOWN build setting there is
# silently ignored -- no warning, no error -- so on a toolchain that does not
# honour it the dylib comes back and nothing notices.
if find "$APP_OUTPUT" -name "*.debug.dylib" | grep -q .; then
    echo "ERROR: $APP_OUTPUT still contains a debug dylib:"
    find "$APP_OUTPUT" -name "*.debug.dylib"
    echo "ENABLE_DEBUG_DYLIB=NO was ignored by this toolchain. The app will"
    echo "fail at launch with 'Library not loaded ... different Team IDs'."
    exit 1
fi

# Run it. Signing can produce a bundle that verifies perfectly and cannot
# start: the hardened runtime enables library validation, and an ad-hoc
# signature has no Team ID, so the bundled CPython framework was refused at
# dlopen with "mapping process and mapped file (non-platform) have different
# Team IDs". Every signature checked out, `codesign --verify --strict` passed,
# and port 7777 was dead -- while Step 6 printed "Installed and running"
# because it only calls `open`. This has now happened twice (the Xcode debug
# dylib was the first), so the build asserts the binary runs.
echo "  Checking the bridge binary actually starts..."
# Bounded. `$(... --help)` blocks forever if the binary starts but hangs -- and
# a hang is exactly what this project keeps hitting (a `say` reading stdin
# forever, `TestPyInstallerBinary` wedged on a blocking readline). A gate meant
# to catch "cannot start" must not become a gate that hangs the build. No
# `timeout(1)` on macOS, hence the poll.
bridge_help_file="$(mktemp -t voxherd-help)"
"$RESOURCES/voxherd-bridge/voxherd-bridge" --help >"$bridge_help_file" 2>&1 &
bridge_help_pid=$!
for _ in $(seq 1 100); do
    kill -0 "$bridge_help_pid" 2>/dev/null || break
    sleep 0.2
done
if kill -0 "$bridge_help_pid" 2>/dev/null; then
    kill -9 "$bridge_help_pid" 2>/dev/null || true
    echo "ERROR: the signed bridge binary started but did not exit within 20s:"
    head -5 "$bridge_help_file" || true
    exit 1
fi
bridge_help_rc=0
wait "$bridge_help_pid" || bridge_help_rc=$?
bridge_help=$(cat "$bridge_help_file")
rm -f "$bridge_help_file"
if [ "$bridge_help_rc" -ne 0 ]; then
    # Measured: a failed dlopen of the bundled CPython exits 255, so this is
    # the branch that actually catches it. The checks below are backstops.
    echo "ERROR: the signed bridge binary failed to run:"
    printf '%s\n' "$bridge_help" | head -5 || true
    exit 1
fi
# Named explicitly, because it is the symptom this check was added for and it
# is clearer than "printed no usage".
if [[ "$bridge_help" == *"Failed to load Python shared library"* \
   || "$bridge_help" == *"[PYI-"* ]]; then
    echo "ERROR: the signed bridge binary could not load CPython:"
    printf '%s\n' "$bridge_help" | head -5 || true
    exit 1
fi
# Deliberately NOT "usage: voxherd-bridge". That exact string depends on
# argparse deriving prog from argv[0], which only happens because `--help`
# falls into the old-style-invocation branch in bridge/cli.py; the real parser
# is `prog="voxherd"`. Narrowing that compat branch -- an obvious cleanup --
# would break this build with no hint why.
if [[ "$bridge_help" != *"usage:"* ]]; then
    echo "ERROR: the signed bridge binary ran but printed no usage:"
    printf '%s\n' "$bridge_help" | head -5 || true
    exit 1
fi
echo "    starts cleanly."

echo "  Verifying signature..."
if codesign --verify --strict "$APP_OUTPUT" 2>&1; then
    echo "  Signature valid."
else
    echo "WARNING: Signature verification had issues (may still work)."
fi

if [ "$SIGN_IDENTITY" != "-" ]; then
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

# Step 6: Install to /Applications and relaunch
if $INSTALL_MODE; then
    echo "--- Step 6: Installing to /Applications ---"

    # Kill the running bridge (both the GUI wrapper and the spawned Python process).
    # `killall` returns non-zero when no process matches; that's fine — we just want
    # any running instances gone before we replace the bundle.
    echo "  Stopping running VoxHerdBridge..."
    killall VoxHerdBridge 2>/dev/null || true
    killall voxherd-bridge 2>/dev/null || true
    # Give launchd a moment to actually reap the process before we rm the bundle.
    sleep 1

    # Always rm -rf before cp -R: cp -R into an existing .app silently merges
    # rather than replacing, leaving stale binaries that don't match the new
    # build. (Documented in memory: feedback_macos_rebuild.md)
    if [ -d "$INSTALL_DEST" ]; then
        echo "  Removing old $INSTALL_DEST..."
        rm -rf "$INSTALL_DEST"
    fi

    echo "  Copying new build to $INSTALL_DEST..."
    cp -R "$APP_OUTPUT" "$INSTALL_DEST"

    echo "  Launching $INSTALL_DEST..."
    open "$INSTALL_DEST"
    echo ""
    echo "=== Installed and running ==="
    exit 0
fi

if [ "$SIGN_IDENTITY" != "-" ]; then
    echo "Next steps:"
    echo "  1. Create DMG:  bash macos/create-dmg.sh"
    echo "  2. Notarize:    bash macos/notarize.sh macos/dist/VoxHerdBridge-X.Y.Z.dmg"
    echo "  3. Or all-in-one: bash scripts/release-macos.sh"
else
    echo "To install + relaunch: bash macos/build-app.sh --debug --install"
    echo "Or manually:           cp -R $APP_OUTPUT /Applications/"
    echo "To test in place:      open $APP_OUTPUT"
fi
