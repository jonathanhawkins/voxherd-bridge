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

# Extract fields from stdin JSON (Claude snake_case + Grok camelCase)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // .sessionId // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // .workspaceRoot // .workspace_root // empty')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // .transcriptPath // empty')
STOP_REASON=$(echo "$INPUT" | jq -r '.stop_reason // .stopReason // "completed"')

PROJECT_DIR="$CWD"
PROJECT_NAME=$(basename "$PROJECT_DIR")
ASSISTANT=$(echo "${VOXHERD_HOOK_ASSISTANT:-claude}" | tr '[:upper:]' '[:lower:]')

# Which model summarises this turn is an install-time decision (models.env,
# written by hooks/install.sh). Resolve it through the shared resolver; if that
# file is missing for any reason, fall back to the hardcoded Claude Haiku
# default so the hook behaves exactly as it always has.
_VH_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -r "$_VH_HOOK_DIR/model-profile.sh" ]; then
  # shellcheck source=model-profile.sh
  . "$_VH_HOOK_DIR/model-profile.sh" 2>/dev/null
fi
if ! type voxherd_summary_argv >/dev/null 2>&1; then
  voxherd_summary_argv() {
    VOXHERD_SUMMARY_CLI="claude"
    VOXHERD_SUMMARY_MODEL="claude-haiku-4-5-20251001"
    VOXHERD_SUMMARY_ARGV=("claude" "-p" "$1" "--model" "claude-haiku-4-5-20251001")
  }
fi

# VOXHERD_QUIET: this session never speaks. Skip the (slow, paid) summary
# entirely and tell the bridge to stay silent via skip_tts. The
# session still POSTs its stop event so it stays visible on the dashboard.
QUIET=""
case "$(printf '%s' "${VOXHERD_QUIET:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) QUIET=1 ;;
esac

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

# Generate summary via the profile's model (skipped entirely for quiet sessions)
SUMMARY=""
if [ -n "$QUIET" ]; then
  echo "  VOXHERD_QUIET set — skipping AI summary" >> "$DEBUG_LOG"
elif [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  voxherd_summary_argv "Summarize what was just accomplished in 1-2 sentences for a voice announcement. Be concise."
  echo "  SUMMARY_CLI=$VOXHERD_SUMMARY_CLI SUMMARY_MODEL=$VOXHERD_SUMMARY_MODEL" >> "$DEBUG_LOG"
  echo "  Transcript exists, attempting summary..." >> "$DEBUG_LOG"
  echo "  Transcript tail (last 5 lines):" >> "$DEBUG_LOG"
  tail -5 "$TRANSCRIPT_PATH" >> "$DEBUG_LOG" 2>&1
  SUMMARY=$(tail -50 "$TRANSCRIPT_PATH" | timeout 15 "${VOXHERD_SUMMARY_ARGV[@]}" 2>>"$DEBUG_LOG")
  echo "  Summary exit code: $?" >> "$DEBUG_LOG"
  echo "  SUMMARY=$SUMMARY" >> "$DEBUG_LOG"
else
  echo "  SKIPPED: transcript_path empty or file not found" >> "$DEBUG_LOG"
fi

# Fallback if the summary CLI failed or returned empty
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
  --argjson skip_tts "$([ -n "$QUIET" ] && echo true || echo false)" \
  '{event: $event, session_id: $session_id, project: $project, project_dir: $project_dir, assistant: $assistant, summary: $summary, stop_reason: $stop_reason, transcript_path: $transcript_path, timestamp: $timestamp, skip_tts: $skip_tts}'
)

# POST to bridge server
if [ -n "$CURL_AUTH_FILE" ]; then
  RESPONSE=$(curl -s --max-time 5 -X POST "http://localhost:7777/api/events" \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    --config "$CURL_AUTH_FILE" \
    -d "$PAYLOAD" 2>&1)
  CURL_EXIT=$?
  rm -f "$CURL_AUTH_FILE"
else
  RESPONSE=$(curl -s --max-time 5 -X POST "http://localhost:7777/api/events" \
    -H "Content-Type: application/json" \
    -H "X-VoxHerd: 1" \
    -d "$PAYLOAD" 2>&1)
  CURL_EXIT=$?
fi

# Log errors if curl failed
if [ "$CURL_EXIT" -ne 0 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] on-stop.sh: curl failed (exit $CURL_EXIT): $RESPONSE" >> "$HOME/.voxherd/logs/hook-errors.log"
fi
