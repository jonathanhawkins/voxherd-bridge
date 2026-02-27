#!/bin/bash
# VoxHerd - SubagentStart hook
# Notifies the bridge when a sub-agent is spawned.

# Prevent recursive hook execution
if [ -n "$VOXHERD_HOOK_RUNNING" ]; then
  exit 0
fi
export VOXHERD_HOOK_RUNNING=1

# Ensure jq is available
command -v jq >/dev/null 2>&1 || exit 0

# Read all stdin into a variable first
INPUT=$(cat)

# Extract fields from SubagentStart hook JSON
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty')
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
ASSISTANT=$(echo "${VOXHERD_HOOK_ASSISTANT:-claude}" | tr '[:upper:]' '[:lower:]')

# Derive project name
PROJECT_NAME=$(basename "$CWD")

# Ensure log directory exists
LOG_DIR="$HOME/.voxherd/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/hook-errors.log"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Build JSON payload
PAYLOAD=$(jq -n \
  --arg event "subagent_start" \
  --arg sid "$SESSION_ID" \
  --arg project "$PROJECT_NAME" \
  --arg project_dir "$CWD" \
  --arg assistant "$ASSISTANT" \
  --arg agent_id "$AGENT_ID" \
  --arg agent_type "$AGENT_TYPE" \
  --arg ts "$TIMESTAMP" \
  '{
    event: $event,
    session_id: $sid,
    project: $project,
    project_dir: $project_dir,
    assistant: $assistant,
    agent_id: $agent_id,
    agent_type: $agent_type,
    timestamp: $ts
  }')

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

# POST to bridge server (fire and forget, don't block Claude Code)
if [ -n "$CURL_AUTH_FILE" ]; then
  curl -s --max-time 3 \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    --config "$CURL_AUTH_FILE" \
    -d "$PAYLOAD" \
    "http://localhost:7777/api/events" >/dev/null 2>&1
  rm -f "$CURL_AUTH_FILE"
else
  curl -s --max-time 3 \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    -d "$PAYLOAD" \
    "http://localhost:7777/api/events" >/dev/null 2>&1
fi

CURL_EXIT=$?
if [ "$CURL_EXIT" -ne 0 ]; then
  echo "[$TIMESTAMP] on-subagent-start: curl failed (exit $CURL_EXIT)" >> "$LOG_FILE"
fi
