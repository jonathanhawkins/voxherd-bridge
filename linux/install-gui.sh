#!/usr/bin/env bash
# install-gui.sh - Install VoxHerd GUI components for Linux (Waybar module + GTK4 panel).
#
# What it does:
#   1. Detects distro (pacman/apt/dnf)
#   2. Installs system packages: python-gobject, gtk4, libadwaita, python-qrcode, python-pillow
#   3. Copies voxherd_panel/ to ~/.local/share/voxherd/voxherd_panel/
#   4. Copies waybar_module.py to ~/.local/share/voxherd/
#   5. Installs .desktop file to ~/.local/share/applications/
#   6. Copies Waybar support files and offers to configure Waybar
#
# Usage:
#   bash linux/install-gui.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[info]${NC} $*"; }
success() { echo -e "${GREEN}[ok]${NC} $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC} $*"; }
error()   { echo -e "${RED}[error]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Support both repo layout (linux/install-gui.sh) and tarball layout
if [ -d "$SCRIPT_DIR/voxherd_panel" ]; then
  LINUX_DIR="$SCRIPT_DIR"
elif [ -d "$SCRIPT_DIR/linux/voxherd_panel" ]; then
  LINUX_DIR="$SCRIPT_DIR/linux"
else
  error "Cannot find voxherd_panel/. Run this from the linux/ directory or repo root."
fi

info "Source directory: $LINUX_DIR"

# ---------- Detect package manager ----------

detect_pkg_manager() {
  if command -v pacman &>/dev/null; then
    echo "pacman"
  elif command -v apt-get &>/dev/null; then
    echo "apt"
  elif command -v dnf &>/dev/null; then
    echo "dnf"
  else
    echo "unknown"
  fi
}

PKG_MGR="$(detect_pkg_manager)"
info "Detected package manager: $PKG_MGR"

# ---------- Install system dependencies ----------

MISSING_PKGS=()

check_python_module() {
  local module="$1"
  local pkg_pacman="$2"
  local pkg_apt="$3"
  local pkg_dnf="$4"

  if python3 -c "import $module" 2>/dev/null; then
    success "Python module '$module' available"
  else
    warn "Python module '$module' not found"
    case "$PKG_MGR" in
      pacman) MISSING_PKGS+=("$pkg_pacman") ;;
      apt)    MISSING_PKGS+=("$pkg_apt") ;;
      dnf)    MISSING_PKGS+=("$pkg_dnf") ;;
      *)      MISSING_PKGS+=("$module") ;;
    esac
  fi
}

# GTK4 and libadwaita via PyGObject
check_python_module "gi" "python-gobject" "python3-gi" "python3-gobject"

# Check for GTK4 typelib
if python3 -c "import gi; gi.require_version('Gtk', '4.0')" 2>/dev/null; then
  success "GTK4 typelib available"
else
  warn "GTK4 typelib not found"
  case "$PKG_MGR" in
    pacman) MISSING_PKGS+=("gtk4") ;;
    apt)    MISSING_PKGS+=("gir1.2-gtk-4.0") ;;
    dnf)    MISSING_PKGS+=("gtk4") ;;
  esac
fi

# Check for libadwaita typelib
if python3 -c "import gi; gi.require_version('Adw', '1')" 2>/dev/null; then
  success "libadwaita typelib available"
else
  warn "libadwaita typelib not found"
  case "$PKG_MGR" in
    pacman) MISSING_PKGS+=("libadwaita") ;;
    apt)    MISSING_PKGS+=("gir1.2-adw-1") ;;
    dnf)    MISSING_PKGS+=("libadwaita") ;;
  esac
fi

# QR code dependencies
check_python_module "qrcode" "python-qrcode" "python3-qrcode" "python3-qrcode"
check_python_module "PIL" "python-pillow" "python3-pil" "python3-pillow"

# Deduplicate
MISSING_PKGS=($(printf "%s\n" "${MISSING_PKGS[@]}" | sort -u))

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
  echo ""
  info "Missing packages: ${MISSING_PKGS[*]}"
  echo -en "${BOLD}Install them now? [Y/n]${NC} "
  read -r REPLY
  REPLY="${REPLY:-Y}"
  if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    case "$PKG_MGR" in
      pacman)
        sudo pacman -S --noconfirm "${MISSING_PKGS[@]}"
        ;;
      apt)
        sudo apt-get update -qq
        sudo apt-get install -y "${MISSING_PKGS[@]}"
        ;;
      dnf)
        sudo dnf install -y "${MISSING_PKGS[@]}"
        ;;
      *)
        warn "Unknown package manager. Install these manually: ${MISSING_PKGS[*]}"
        ;;
    esac
    success "Packages installed."
  else
    warn "Skipping package installation. The panel app may not work."
  fi
fi

# ---------- Install files ----------

INSTALL_DIR="$HOME/.local/share/voxherd"
info "Installing to $INSTALL_DIR"

mkdir -p "$INSTALL_DIR/voxherd_panel"

# Copy panel package
cp -r "$LINUX_DIR/voxherd_panel/"*.py "$INSTALL_DIR/voxherd_panel/"
success "Copied voxherd_panel/ to $INSTALL_DIR/voxherd_panel/"

# Copy Waybar module
cp "$LINUX_DIR/waybar_module.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/waybar_module.py"
success "Copied waybar_module.py to $INSTALL_DIR/"

# Copy Waybar support files
cp "$LINUX_DIR/waybar_style.css" "$INSTALL_DIR/"
cp "$LINUX_DIR/waybar_config.jsonc" "$INSTALL_DIR/"
success "Copied Waybar config + CSS to $INSTALL_DIR/"

# Install desktop file
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cp "$LINUX_DIR/voxherd-panel.desktop" "$DESKTOP_DIR/"
success "Installed desktop file to $DESKTOP_DIR/"

# Update desktop database if available
if command -v update-desktop-database &>/dev/null; then
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

# ---------- Waybar configuration ----------

echo ""
WAYBAR_CONFIG_DIR="$HOME/.config/waybar"
if [ -d "$WAYBAR_CONFIG_DIR" ]; then
  info "Waybar config directory found at $WAYBAR_CONFIG_DIR"
  echo -en "${BOLD}Would you like to see the Waybar config snippet to add? [Y/n]${NC} "
  read -r REPLY
  REPLY="${REPLY:-Y}"
  if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    echo ""
    echo -e "${CYAN}Add this to your Waybar config (${WAYBAR_CONFIG_DIR}/config.jsonc):${NC}"
    echo ""
    echo '    "custom/voxherd": {'
    echo "        \"exec\": \"python3 $INSTALL_DIR/waybar_module.py\","
    echo '        "return-type": "json",'
    echo '        "interval": 5,'
    echo '        "on-click": "python3 -m voxherd_panel",'
    echo '        "tooltip": true'
    echo '    }'
    echo ""
    echo -e "${CYAN}Add to your modules-right (or wherever you want it):${NC}"
    echo '    "modules-right": ["custom/voxherd", ...]'
    echo ""
    echo -e "${CYAN}Add to your Waybar style.css:${NC}"
    echo "    @import url(\"$INSTALL_DIR/waybar_style.css\");"
    echo ""
  fi
else
  info "Waybar config directory not found. If you use Waybar, see $INSTALL_DIR/waybar_config.jsonc"
fi

# ---------- Summary ----------

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${GREEN}${BOLD}  VoxHerd GUI - Installed!${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo -e "  ${BOLD}Launch panel:${NC}      python3 -m voxherd_panel"
echo -e "  ${BOLD}Waybar module:${NC}     $INSTALL_DIR/waybar_module.py"
echo -e "  ${BOLD}Desktop file:${NC}      $DESKTOP_DIR/voxherd-panel.desktop"
echo -e "  ${BOLD}Config files:${NC}      $INSTALL_DIR/waybar_config.jsonc"
echo -e "                     $INSTALL_DIR/waybar_style.css"
echo ""
echo -e "  ${BOLD}Test Waybar module:${NC}"
echo -e "    python3 $INSTALL_DIR/waybar_module.py | jq ."
echo ""
echo -e "  ${BOLD}Test panel app:${NC}"
echo -e "    python3 -m voxherd_panel"
echo ""
