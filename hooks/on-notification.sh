#!/bin/bash
# on-notification.sh — Notification hook for VoxHerd.
# Forwards notification payloads to the bridge server.

# Prevent recursive hook execution (e.g. when on-stop.sh spawns claude -p)
if [ -n "$VOXHERD_HOOK_RUNNING" ]; then
  exit 0
fi
export VOXHERD_HOOK_RUNNING=1

# Ensure jq is available; exit silently if not.
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
PROJECT_NAME=$(basename "$CWD")
ASSISTANT=$(echo "${VOXHERD_HOOK_ASSISTANT:-claude}" | tr '[:upper:]' '[:lower:]')

LOG_DIR="$HOME/.voxherd/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Detect tmux target so the bridge can dispatch via send-keys
TMUX_TARGET=""
if [ -n "$TMUX" ]; then
  TMUX_TARGET=$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || true)
fi

PAYLOAD=$(echo "$INPUT" | jq \
  --arg event "notification" \
  --arg project "$PROJECT_NAME" \
  --arg project_dir "$CWD" \
  --arg assistant "$ASSISTANT" \
  --arg ts "$TIMESTAMP" \
  --arg tmux "$TMUX_TARGET" \
  '. + {event: $event, project: $project, project_dir: $project_dir, assistant: $assistant, timestamp: $ts, tmux_target: (if $tmux != "" then $tmux else null end)}')

if [ $? -ne 0 ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [on-notification] jq payload build failed" >> "$LOG_DIR/hook-errors.log"
  exit 0
fi

# Ensure temp files are cleaned up even if the script is killed
cleanup() { [ -n "$CURL_AUTH_FILE" ] && rm -f "$CURL_AUTH_FILE"; }
trap cleanup EXIT

# Read auth token if available — use temp file to keep it out of process list
CURL_AUTH_FILE=""
TOKEN_FILE="$HOME/.voxherd/auth_token"
if [ -f "$TOKEN_FILE" ]; then
  AUTH_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null)
  if [ -n "$AUTH_TOKEN" ]; then
    CURL_AUTH_FILE=$(mktemp /tmp/vh-curl-XXXXXX)
    printf 'header = "Authorization: Bearer %s"\n' "$AUTH_TOKEN" > "$CURL_AUTH_FILE"
    chmod 600 "$CURL_AUTH_FILE"
  fi
fi

if [ -n "$CURL_AUTH_FILE" ]; then
  HTTP_STATUS=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    --config "$CURL_AUTH_FILE" \
    -d "$PAYLOAD" \
    "http://localhost:7777/api/events")
  rm -f "$CURL_AUTH_FILE"
else
  HTTP_STATUS=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    -d "$PAYLOAD" \
    "http://localhost:7777/api/events")
fi

if [ "$HTTP_STATUS" != "200" ] && [ "$HTTP_STATUS" != "201" ] && [ "$HTTP_STATUS" != "204" ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [on-notification] POST failed with status $HTTP_STATUS (session=$SESSION_ID project=$PROJECT_NAME)" >> "$LOG_DIR/hook-errors.log"
fi
