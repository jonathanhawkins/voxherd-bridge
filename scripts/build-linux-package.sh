#!/usr/bin/env bash
# build-linux-package.sh - Build a self-contained tarball for Linux deployment.
#
# Produces voxherd-bridge.tar.gz that can be extracted and installed on any
# Linux box (Arch, Ubuntu/Debian, Fedora) without needing the full repo.
#
# Usage:
#   bash scripts/build-linux-package.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[info]${NC} $*"; }
success() { echo -e "${GREEN}[ok]${NC} $*"; }
error()   { echo -e "${RED}[error]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ ! -f "$REPO_ROOT/bridge/__main__.py" ]; then
  error "Cannot find bridge/__main__.py. Run this from the VoxHerd repo root."
fi

# Mac-only Python files to exclude from the tarball
MAC_ONLY_FILES="mac_tts.py mac_stt.py mac_voice_loop.py"

# ---------- Stage ----------

STAGING="$(mktemp -d)"
PACKAGE_DIR="$STAGING/voxherd-bridge"
trap 'rm -rf "$STAGING"' EXIT

info "Staging package in $PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR/bridge" "$PACKAGE_DIR/hooks"

# Copy bridge Python files (excluding mac-only)
for f in "$REPO_ROOT"/bridge/*.py; do
  basename="$(basename "$f")"
  skip=false
  for mac_file in $MAC_ONLY_FILES; do
    if [ "$basename" = "$mac_file" ]; then
      skip=true
      break
    fi
  done
  if [ "$skip" = false ]; then
    cp "$f" "$PACKAGE_DIR/bridge/"
  fi
done

# Copy requirements.txt (stripping test deps) and pyproject.toml
grep -v -E '^(pytest|#.*[Tt]est)' "$REPO_ROOT/bridge/requirements.txt" \
  | sed '/^$/N;/^\n$/d' > "$PACKAGE_DIR/bridge/requirements.txt"
cp "$REPO_ROOT/bridge/pyproject.toml" "$PACKAGE_DIR/bridge/"

# Copy all hook scripts (skip __pycache__ and directories)
for f in "$REPO_ROOT"/hooks/*; do
  [ -f "$f" ] || continue
  cp "$f" "$PACKAGE_DIR/hooks/"
done

# Copy systemd service template to top level
cp "$REPO_ROOT/scripts/voxherd-bridge.service" "$PACKAGE_DIR/"

# Copy installer to top level
cp "$REPO_ROOT/scripts/install-linux.sh" "$PACKAGE_DIR/install.sh"
chmod +x "$PACKAGE_DIR/install.sh"

# Copy Linux GUI files (panel app + Waybar module)
if [ -d "$REPO_ROOT/linux" ]; then
  info "Including Linux GUI files..."
  mkdir -p "$PACKAGE_DIR/linux/voxherd_panel"
  cp "$REPO_ROOT"/linux/voxherd_panel/*.py "$PACKAGE_DIR/linux/voxherd_panel/"
  cp "$REPO_ROOT"/linux/waybar_module.py "$PACKAGE_DIR/linux/"
  cp "$REPO_ROOT"/linux/waybar_config.jsonc "$PACKAGE_DIR/linux/"
  cp "$REPO_ROOT"/linux/waybar_style.css "$PACKAGE_DIR/linux/"
  cp "$REPO_ROOT"/linux/voxherd-panel.desktop "$PACKAGE_DIR/linux/"
  cp "$REPO_ROOT"/linux/install-gui.sh "$PACKAGE_DIR/linux/"
  chmod +x "$PACKAGE_DIR/linux/install-gui.sh"
fi

info "Staged files:"
(cd "$PACKAGE_DIR" && find . -type f | sort | sed 's|^./|  |')

# ---------- Build tarball ----------

OUTPUT="$REPO_ROOT/voxherd-bridge.tar.gz"

(cd "$STAGING" && tar czf "$OUTPUT" voxherd-bridge/)

SIZE="$(du -h "$OUTPUT" | cut -f1)"
success "Package built: $OUTPUT ($SIZE)"

echo ""
echo -e "${BOLD}To deploy on Linux:${NC}"
echo "  scp $OUTPUT user@host:~/"
echo "  ssh user@host 'tar xzf voxherd-bridge.tar.gz && cd voxherd-bridge && bash install.sh'"
echo ""
echo -e "${BOLD}Optional: Install GUI (Waybar + GTK4 panel):${NC}"
echo "  ssh user@host 'cd voxherd-bridge && bash linux/install-gui.sh'"
