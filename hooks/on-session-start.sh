#!/bin/bash
# VoxHerd - SessionStart hook
# Registers the new session with the bridge server.

# Prevent recursive hook execution (e.g. when on-stop.sh spawns claude -p)
if [ -n "$VOXHERD_HOOK_RUNNING" ]; then
  exit 0
fi
export VOXHERD_HOOK_RUNNING=1

# Ensure jq is available
command -v jq >/dev/null 2>&1 || exit 0

# Read all stdin into a variable first
INPUT=$(cat)

# Extract fields
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
ASSISTANT=$(echo "${VOXHERD_HOOK_ASSISTANT:-claude}" | tr '[:upper:]' '[:lower:]')

PROJECT_DIR="$CWD"
PROJECT_NAME=$(basename "$PROJECT_DIR")

# Ensure log directory exists
LOG_DIR="$HOME/.voxherd/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/hook-errors.log"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Detect tmux target if running inside tmux
# Use $TMUX_PANE (pane ID like %5) with -t for correct resolution,
# even when Claude Code was dispatched from a different tmux session (e.g. bridge).
TMUX_TARGET=""
if [ -n "$TMUX_PANE" ]; then
  TMUX_TARGET=$(tmux display-message -t "$TMUX_PANE" -p '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || true)
elif [ -n "$TMUX" ]; then
  TMUX_TARGET=$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || true)
fi

# Build JSON payload
PAYLOAD=$(jq -n \
  --arg sid "$SESSION_ID" \
  --arg project "$PROJECT_NAME" \
  --arg project_dir "$PROJECT_DIR" \
  --arg assistant "$ASSISTANT" \
  --arg status "active" \
  --arg ts "$TIMESTAMP" \
  --arg tmux "$TMUX_TARGET" \
  '{
    session_id: $sid,
    project: $project,
    project_dir: $project_dir,
    assistant: $assistant,
    status: $status,
    timestamp: $ts,
    tmux_target: (if $tmux != "" then $tmux else null end)
  }')

# Ensure temp files are cleaned up even if the script is killed
cleanup() { [ -n "$CURL_AUTH_FILE" ] && rm -f "$CURL_AUTH_FILE"; }
trap cleanup EXIT

# Read auth token if available — use temp file to keep it out of process list
TOKEN_FILE="$HOME/.voxherd/auth_token"
CURL_AUTH_FILE=""
if [ -f "$TOKEN_FILE" ]; then
  AUTH_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null)
  if [ -n "$AUTH_TOKEN" ]; then
    CURL_AUTH_FILE=$(mktemp /tmp/vh-curl-XXXXXX)
    printf 'header = "Authorization: Bearer %s"\n' "$AUTH_TOKEN" > "$CURL_AUTH_FILE"
    chmod 600 "$CURL_AUTH_FILE"
  fi
fi

# POST to bridge server
if [ -n "$CURL_AUTH_FILE" ]; then
  RESPONSE=$(curl -s --max-time 5 \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    --config "$CURL_AUTH_FILE" \
    -d "$PAYLOAD" \
    "http://localhost:7777/api/sessions/register" 2>&1)
  rm -f "$CURL_AUTH_FILE"
else
  RESPONSE=$(curl -s --max-time 5 \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    -d "$PAYLOAD" \
    "http://localhost:7777/api/sessions/register" 2>&1)
fi

CURL_EXIT=$?
if [ "$CURL_EXIT" -ne 0 ]; then
  echo "[$TIMESTAMP] on-session-start: curl failed (exit $CURL_EXIT): $RESPONSE" >> "$LOG_FILE"
fi
