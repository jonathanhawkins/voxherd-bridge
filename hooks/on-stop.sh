#!/bin/bash
# VoxHerd on-stop hook
# Runs when an assistant session ends a turn. Generates a summary and notifies the bridge server.
# Must NEVER block the assistant CLI -- no set -e, all external calls have timeouts.

# Prevent recursive hook execution when we spawn `claude -p` for summaries
if [ -n "$VOXHERD_HOOK_RUNNING" ]; then
  exit 0
fi
export VOXHERD_HOOK_RUNNING=1

# Check for jq
if ! command -v jq &>/dev/null; then
  exit 0
fi

# Read all stdin into a variable
INPUT=$(cat)

# Extract fields from stdin JSON
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
STOP_REASON=$(echo "$INPUT" | jq -r '.stop_reason // "completed"')

PROJECT_DIR="$CWD"
PROJECT_NAME=$(basename "$PROJECT_DIR")
ASSISTANT=$(echo "${VOXHERD_HOOK_ASSISTANT:-claude}" | tr '[:upper:]' '[:lower:]')

# Ensure log directory exists
LOG_DIR="$HOME/.voxherd/logs"
mkdir -p "$LOG_DIR"
DEBUG_LOG="$LOG_DIR/on-stop-debug.log"

echo "[$(date)] === on-stop.sh invoked ===" >> "$DEBUG_LOG"
echo "  SESSION_ID=$SESSION_ID" >> "$DEBUG_LOG"
echo "  CWD=$CWD" >> "$DEBUG_LOG"
echo "  PROJECT_NAME=$PROJECT_NAME" >> "$DEBUG_LOG"
echo "  TRANSCRIPT_PATH=$TRANSCRIPT_PATH" >> "$DEBUG_LOG"
echo "  TRANSCRIPT_EXISTS=$([ -f "$TRANSCRIPT_PATH" ] && echo YES || echo NO)" >> "$DEBUG_LOG"
echo "  CLAUDE_PATH=$(which claude 2>/dev/null || echo NOT_FOUND)" >> "$DEBUG_LOG"
echo "  ASSISTANT=$ASSISTANT" >> "$DEBUG_LOG"

# Generate summary via Haiku
SUMMARY=""
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  echo "  Transcript exists, attempting Haiku summary..." >> "$DEBUG_LOG"
  echo "  Transcript tail (last 5 lines):" >> "$DEBUG_LOG"
  tail -5 "$TRANSCRIPT_PATH" >> "$DEBUG_LOG" 2>&1
  SUMMARY=$(tail -50 "$TRANSCRIPT_PATH" | timeout 15 claude -p "Summarize what was just accomplished in 1-2 sentences for a voice announcement. Be concise." --model claude-haiku-4-5-20251001 2>>"$DEBUG_LOG")
  echo "  Haiku exit code: $?" >> "$DEBUG_LOG"
  echo "  SUMMARY=$SUMMARY" >> "$DEBUG_LOG"
else
  echo "  SKIPPED: transcript_path empty or file not found" >> "$DEBUG_LOG"
fi

# Fallback if claude failed or returned empty
if [ -z "$SUMMARY" ]; then
  SUMMARY="Task completed."
  echo "  Using fallback summary" >> "$DEBUG_LOG"
fi

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Ensure temp files are cleaned up even if the script is killed
cleanup() { [ -n "$CURL_AUTH_FILE" ] && rm -f "$CURL_AUTH_FILE"; }
trap cleanup EXIT

# Read auth token if available — use temp file to keep it out of process list
CURL_AUTH_FILE=""
TOKEN_FILE="$HOME/.voxherd/auth_token"
if [ -f "$TOKEN_FILE" ]; then
  AUTH_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$AUTH_TOKEN" ]; then
    CURL_AUTH_FILE=$(mktemp /tmp/vh-curl-XXXXXX)
    printf 'header = "Authorization: Bearer %s"\n' "$AUTH_TOKEN" > "$CURL_AUTH_FILE"
    chmod 600 "$CURL_AUTH_FILE"
  fi
fi

# Build JSON payload once
PAYLOAD=$(jq -n \
  --arg event "stop" \
  --arg session_id "$SESSION_ID" \
  --arg project "$PROJECT_NAME" \
  --arg project_dir "$PROJECT_DIR" \
  --arg assistant "$ASSISTANT" \
  --arg summary "$SUMMARY" \
  --arg stop_reason "$STOP_REASON" \
  --arg transcript_path "$TRANSCRIPT_PATH" \
  --arg timestamp "$TIMESTAMP" \
  '{event: $event, session_id: $session_id, project: $project, project_dir: $project_dir, assistant: $assistant, summary: $summary, stop_reason: $stop_reason, transcript_path: $transcript_path, timestamp: $timestamp}'
)

# POST to bridge server
if [ -n "$CURL_AUTH_FILE" ]; then
  RESPONSE=$(curl -s --max-time 5 -X POST "http://localhost:7777/api/events" \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    --config "$CURL_AUTH_FILE" \
    -d "$PAYLOAD" 2>&1)
  rm -f "$CURL_AUTH_FILE"
else
  RESPONSE=$(curl -s --max-time 5 -X POST "http://localhost:7777/api/events" \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    -d "$PAYLOAD" 2>&1)
fi

# Log errors if curl failed
CURL_EXIT=$?
if [ "$CURL_EXIT" -ne 0 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] on-stop.sh: curl failed (exit $CURL_EXIT): $RESPONSE" >> "$HOME/.voxherd/logs/hook-errors.log"
fi
