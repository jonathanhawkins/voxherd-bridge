#!/usr/bin/env bash
set -e

# dev-setup.sh - One-command local dev setup for VoxHerd.
# Creates venv, installs deps, deploys hooks, starts bridge server,
# and verifies it's responding on port 7777.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BRIDGE_DIR="$REPO_ROOT/bridge"
VENV_DIR="$BRIDGE_DIR/.venv"
BRIDGE_PID=""

# ---------- Cleanup ----------

cleanup() {
  if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo ""
    echo "Stopping bridge server (PID $BRIDGE_PID)..."
    kill "$BRIDGE_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

# ---------- Prerequisites ----------

echo "Checking prerequisites..."

# Python 3.11+
if ! command -v python3 &>/dev/null; then
  echo "Error: python3 is required but not found."
  echo "Install Python 3.11+ from https://www.python.org/downloads/ or via:"
  echo "  brew install python@3.11"
  exit 1
fi

PYTHON_VERSION="$(python3 --version 2>&1 | awk '{print $2}')"
PYTHON_MAJOR="$(echo "$PYTHON_VERSION" | cut -d. -f1)"
PYTHON_MINOR="$(echo "$PYTHON_VERSION" | cut -d. -f2)"

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
  echo "Error: Python 3.11+ is required, but found $PYTHON_VERSION."
  echo "Install a newer version from https://www.python.org/downloads/ or via:"
  echo "  brew install python@3.11"
  exit 1
fi
echo "  python3 $PYTHON_VERSION OK"

# jq
if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not found."
  echo "  brew install jq"
  exit 1
fi
echo "  jq OK"

# claude CLI (optional)
if ! command -v claude &>/dev/null; then
  echo "  Warning: claude CLI not found."
  echo "  Install from https://docs.anthropic.com/en/docs/claude-code"
else
  echo "  claude CLI OK"
fi

# codex CLI (optional)
if ! command -v codex &>/dev/null; then
  echo "  Warning: codex CLI not found."
  echo "  Install from https://developers.openai.com/codex"
else
  echo "  codex CLI OK"
fi

# gemini CLI (optional)
if ! command -v gemini &>/dev/null; then
  echo "  Warning: gemini CLI not found."
  echo "  Install from https://github.com/google-gemini/gemini-cli"
else
  echo "  gemini CLI OK"
fi

# wscat (optional)
if ! command -v wscat &>/dev/null; then
  echo "  Warning: wscat not found. Manual WebSocket testing won't be available."
  echo "  Install with: npm install -g wscat"
else
  echo "  wscat OK"
fi

echo ""

# ---------- Python venv ----------

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating Python venv at $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
else
  echo "Python venv already exists at $VENV_DIR"
fi

echo "Installing Python dependencies..."
source "$VENV_DIR/bin/activate"
pip install -q -r "$BRIDGE_DIR/requirements.txt"
deactivate
echo "  Dependencies installed."
echo ""

# ---------- Deploy hooks ----------

HOOK_STATUS="skipped (no assistant CLI found)"

# install.sh auto-detects installed CLIs when HOOK_AGENTS is unset,
# but honour an explicit override if the caller set HOOK_AGENTS.
echo "Deploying hooks (auto-detecting installed CLIs)..."
if HOOK_AGENTS="${HOOK_AGENTS:-}" bash "$REPO_ROOT/hooks/install.sh"; then
  HOOK_STATUS="deployed (auto-detected)"
else
  echo "Warning: hook installation had errors."
  HOOK_STATUS="errors during install"
fi
echo ""

# ---------- Start bridge server ----------

echo "Starting bridge server..."
source "$VENV_DIR/bin/activate"
python -m bridge run &
BRIDGE_PID=$!
deactivate

echo "  Bridge server started (PID $BRIDGE_PID)"
echo "  Waiting for server on port 7777..."

ATTEMPTS=0
MAX_ATTEMPTS=10
SERVER_READY=false

while [ "$ATTEMPTS" -lt "$MAX_ATTEMPTS" ]; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if curl -s -o /dev/null -w "" http://localhost:7777/ 2>/dev/null; then
    SERVER_READY=true
    break
  fi
  sleep 1
done

if [ "$SERVER_READY" = false ]; then
  echo "Error: Bridge server did not respond on port 7777 after $MAX_ATTEMPTS attempts."
  echo "Check logs for errors. PID was $BRIDGE_PID."
  exit 1
fi

echo "  Server is responding on port 7777."
echo ""

# ---------- Summary ----------

echo "========================================"
echo "  VoxHerd dev environment is ready!"
echo "========================================"
echo ""
echo "What was set up:"
echo "  - Python venv:   $VENV_DIR"
echo "  - Dependencies:  installed from bridge/requirements.txt"
echo "  - Hooks:         $HOOK_STATUS"
echo "  - Bridge server: running on http://localhost:7777 (PID $BRIDGE_PID)"
echo ""
echo "Test with:"
echo "  curl http://localhost:7777/"
echo "  curl -X POST http://localhost:7777/api/sessions/register -H 'Content-Type: application/json' -d '{\"session_id\": \"test\"}'"
echo "  curl -X POST http://localhost:7777/api/events -H 'Content-Type: application/json' -d '{\"type\": \"stop\", \"session_id\": \"test\"}'"
echo "  wscat -c ws://localhost:7777/ws/ios"
echo ""
echo "Stop the server:"
echo "  kill $BRIDGE_PID"
echo "  Or press Ctrl-C"
echo ""

# Keep script alive so trap can clean up on Ctrl-C
wait "$BRIDGE_PID"
