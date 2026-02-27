#!/usr/bin/env bash
# install-linux.sh - One-command Linux installer for the VoxHerd bridge server.
#
# What it does:
#   1. Detects distro (apt/dnf/pacman)
#   2. Checks prerequisites (Python 3.11+, jq, curl, tmux)
#   3. Offers to install missing packages
#   4. Creates Python venv and installs requirements
#   5. Runs hooks/install.sh to deploy assistant hooks (Claude/Gemini)
#   6. Generates auth token if not present
#   7. Installs and enables a systemd user service
#   8. Starts the bridge
#   9. Prints connection info for the iOS app
#
# Usage:
#   bash scripts/install-linux.sh

set -euo pipefail

# ---------- Colors ----------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()    { echo -e "${CYAN}[info]${NC} $*"; }
success() { echo -e "${GREEN}[ok]${NC} $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC} $*"; }
error()   { echo -e "${RED}[error]${NC} $*"; exit 1; }

# ---------- Locate root ----------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Support both repo layout (scripts/install-linux.sh -> repo root is ..)
# and tarball layout (install.sh at top level alongside bridge/)
if [ -f "$SCRIPT_DIR/bridge/__main__.py" ]; then
  # Tarball layout: install.sh is at the package root
  REPO_ROOT="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../bridge/__main__.py" ]; then
  # Repo layout: scripts/install-linux.sh
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  error "Cannot find bridge/__main__.py. Run this from the VoxHerd repo or extracted tarball."
fi

info "VoxHerd root: $REPO_ROOT"

# ---------- Detect package manager ----------

detect_pkg_manager() {
  if command -v apt-get &>/dev/null; then
    echo "apt"
  elif command -v dnf &>/dev/null; then
    echo "dnf"
  elif command -v pacman &>/dev/null; then
    echo "pacman"
  else
    echo "unknown"
  fi
}

PKG_MGR="$(detect_pkg_manager)"
info "Detected package manager: $PKG_MGR"

install_pkg() {
  local pkg="$1"
  case "$PKG_MGR" in
    apt)    sudo apt-get install -y "$pkg" ;;
    dnf)    sudo dnf install -y "$pkg" ;;
    pacman) sudo pacman -S --noconfirm "$pkg" ;;
    *)      error "Unknown package manager. Please install '$pkg' manually." ;;
  esac
}

# ---------- Check Python 3.11+ ----------

check_python() {
  local py=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
      py="$candidate"
      break
    fi
  done

  if [ -z "$py" ]; then
    error "Python 3.11+ is required but not found. Install it with your package manager."
  fi

  # Check version >= 3.11
  local version
  version="$($py -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  local major minor
  major="$(echo "$version" | cut -d. -f1)"
  minor="$(echo "$version" | cut -d. -f2)"

  if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
    error "Python $version found, but 3.11+ is required."
  fi

  # Print status to stderr so it doesn't pollute the captured output
  success "Python $version found: $(command -v "$py")" >&2
  echo "$py"
}

PYTHON="$(check_python)"

# ---------- Check / install prerequisites ----------

MISSING_PKGS=()

check_prereq() {
  local cmd="$1"
  local pkg_apt="${2:-$1}"
  local pkg_dnf="${3:-$1}"
  local pkg_pacman="${4:-$1}"

  if command -v "$cmd" &>/dev/null; then
    success "$cmd found: $(command -v "$cmd")"
  else
    warn "$cmd not found."
    case "$PKG_MGR" in
      apt)    MISSING_PKGS+=("$pkg_apt") ;;
      dnf)    MISSING_PKGS+=("$pkg_dnf") ;;
      pacman) MISSING_PKGS+=("$pkg_pacman") ;;
      *)      MISSING_PKGS+=("$cmd") ;;
    esac
  fi
}

check_prereq jq jq jq jq
check_prereq curl curl curl curl
check_prereq tmux tmux tmux tmux
check_prereq espeak-ng espeak-ng espeak-ng espeak-ng

# Assistant CLIs (optional; only Claude/Gemini currently support lifecycle hooks)
HOOK_AGENTS_TO_INSTALL="${HOOK_AGENTS:-}"
AUTO_HOOK_AGENTS=()

if command -v claude &>/dev/null; then
  success "claude CLI found: $(command -v claude)"
  AUTO_HOOK_AGENTS+=("claude")
else
  warn "claude CLI not found. Install from https://docs.anthropic.com/en/docs/claude-code"
fi

if command -v gemini &>/dev/null; then
  success "gemini CLI found: $(command -v gemini)"
  AUTO_HOOK_AGENTS+=("gemini")
else
  warn "gemini CLI not found. Install from https://github.com/google-gemini/gemini-cli"
fi

if command -v codex &>/dev/null; then
  success "codex CLI found: $(command -v codex)"
else
  warn "codex CLI not found. Install from https://developers.openai.com/codex"
fi

if [ -z "$HOOK_AGENTS_TO_INSTALL" ] && [ ${#AUTO_HOOK_AGENTS[@]} -gt 0 ]; then
  HOOK_AGENTS_TO_INSTALL="$(IFS=,; echo "${AUTO_HOOK_AGENTS[*]}")"
fi

# Check for python3-venv on Debian/Ubuntu (where venv is a separate package)
if [ "$PKG_MGR" = "apt" ]; then
  if ! "$PYTHON" -c "import venv" &>/dev/null; then
    warn "python3-venv not available."
    # Determine the python3-X-venv package name
    PY_VERSION="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    MISSING_PKGS+=("python${PY_VERSION}-venv")
  fi
fi

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
  echo ""
  info "Missing packages: ${MISSING_PKGS[*]}"
  echo -e "${BOLD}Install them now? [Y/n]${NC} "
  read -r REPLY
  REPLY="${REPLY:-Y}"
  if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    if [ "$PKG_MGR" = "apt" ]; then
      sudo apt-get update -qq
    fi
    for pkg in "${MISSING_PKGS[@]}"; do
      info "Installing $pkg..."
      install_pkg "$pkg"
    done
    success "All packages installed."
  else
    warn "Skipping package installation. Some features may not work."
  fi
fi

# ---------- Create Python venv ----------

VENV_DIR="$REPO_ROOT/bridge/.venv"

if [ -d "$VENV_DIR" ]; then
  info "Existing venv found at $VENV_DIR"
else
  info "Creating Python venv at $VENV_DIR..."
  "$PYTHON" -m venv "$VENV_DIR"
  success "Venv created."
fi

info "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_ROOT/bridge/requirements.txt"
success "Python dependencies installed."

# ---------- Install hooks ----------

HOOK_STATUS="skipped"
if [ -f "$REPO_ROOT/hooks/install.sh" ]; then
  if [ -n "$HOOK_AGENTS_TO_INSTALL" ]; then
    info "Installing hooks for: $HOOK_AGENTS_TO_INSTALL"
    HOOK_AGENTS="$HOOK_AGENTS_TO_INSTALL" bash "$REPO_ROOT/hooks/install.sh"
    success "Hooks installed."
    HOOK_STATUS="installed for $HOOK_AGENTS_TO_INSTALL"
  else
    warn "No hook-capable assistant CLI found (Claude/Gemini). Skipping hook installation."
    HOOK_STATUS="skipped (no Claude/Gemini CLI found)"
  fi
else
  warn "hooks/install.sh not found. Skipping hook installation."
  HOOK_STATUS="skipped (hooks/install.sh not found)"
fi

# ---------- Generate auth token ----------

VOXHERD_DIR="$HOME/.voxherd"
AUTH_TOKEN_FILE="$VOXHERD_DIR/auth_token"

mkdir -p "$VOXHERD_DIR/logs"

if [ -f "$AUTH_TOKEN_FILE" ]; then
  info "Auth token already exists at $AUTH_TOKEN_FILE"
else
  info "Generating auth token..."
  TOKEN="$(openssl rand -hex 32)"
  # Write with restrictive permissions
  (umask 077 && echo -n "$TOKEN" > "$AUTH_TOKEN_FILE")
  success "Auth token generated at $AUTH_TOKEN_FILE"
fi

AUTH_TOKEN="$(cat "$AUTH_TOKEN_FILE")"

# ---------- Install systemd user service ----------

# Look for service template in tarball layout first, then repo layout
if [ -f "$REPO_ROOT/voxherd-bridge.service" ]; then
  SERVICE_TEMPLATE="$REPO_ROOT/voxherd-bridge.service"
elif [ -f "$REPO_ROOT/scripts/voxherd-bridge.service" ]; then
  SERVICE_TEMPLATE="$REPO_ROOT/scripts/voxherd-bridge.service"
else
  error "Service template not found. Expected voxherd-bridge.service in package root or scripts/."
fi
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/voxherd-bridge.service"

info "Installing systemd user service..."
mkdir -p "$SERVICE_DIR"

# Template the service file with actual paths
sed \
  -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
  -e "s|__VENV__|$VENV_DIR|g" \
  -e "s|__HOME__|$HOME|g" \
  "$SERVICE_TEMPLATE" > "$SERVICE_FILE"

# Reload and enable
systemctl --user daemon-reload
systemctl --user enable voxherd-bridge.service
success "systemd service installed and enabled."

# Enable lingering so the service runs without an active login session
if command -v loginctl &>/dev/null; then
  loginctl enable-linger "$USER" 2>/dev/null || warn "Could not enable linger. Service may stop on logout."
fi

# ---------- Start the bridge ----------

info "Starting VoxHerd bridge server..."
systemctl --user restart voxherd-bridge.service
sleep 2

if systemctl --user is-active --quiet voxherd-bridge.service; then
  success "Bridge server is running!"
else
  warn "Bridge may not have started. Check with: systemctl --user status voxherd-bridge"
  warn "Logs: journalctl --user -u voxherd-bridge -f"
fi

# ---------- Get IP address ----------

get_ip() {
  # Try to get the primary non-loopback IP
  ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' || \
  hostname -I 2>/dev/null | awk '{print $1}' || \
  echo "YOUR_IP"
}

LOCAL_IP="$(get_ip)"

# ---------- Summary ----------

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${GREEN}${BOLD}  VoxHerd Bridge Server - Installed!${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo -e "  ${BOLD}Bridge URL:${NC}      http://$LOCAL_IP:7777"
echo -e "  ${BOLD}WebSocket:${NC}       ws://$LOCAL_IP:7777/ws/ios"
echo -e "  ${BOLD}Auth token:${NC}      $AUTH_TOKEN"
echo -e "  ${BOLD}Hooks:${NC}           $HOOK_STATUS"
echo ""
echo -e "  ${BOLD}iOS app pairing:${NC}"
echo -e "    Show QR code:  ${CYAN}cd $REPO_ROOT && source bridge/.venv/bin/activate && python -m bridge qr${NC}"
echo -e "    Or manually:   host=${CYAN}$LOCAL_IP${NC}  port=${CYAN}7777${NC}  token=${CYAN}$AUTH_TOKEN${NC}"
echo ""
echo -e "  ${BOLD}Service management:${NC}"
echo -e "    Status:   systemctl --user status voxherd-bridge"
echo -e "    Logs:     journalctl --user -u voxherd-bridge -f"
echo -e "    Restart:  systemctl --user restart voxherd-bridge"
echo -e "    Stop:     systemctl --user stop voxherd-bridge"
echo ""
echo -e "  ${BOLD}Quick test:${NC}"
echo -e "    curl http://localhost:7777/health"
echo -e "    curl -H \"Authorization: Bearer \$AUTH_TOKEN\" http://localhost:7777/api/sessions"
echo -e "    (where AUTH_TOKEN=\$(cat ~/.voxherd/auth_token))"
echo ""
