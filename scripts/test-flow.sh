#!/usr/bin/env bash
# test-flow.sh - End-to-end test for the VoxHerd bridge server.
# Validates REST endpoints work without needing the iOS app.
# Requires the bridge to already be running on port 7777 (use dev-setup.sh).
# Skips WebSocket testing since wscat isn't guaranteed to be installed.

set -e

# ---------- Paths ----------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------- Check dependencies ----------

if ! command -v curl &>/dev/null; then
  echo "Error: curl is required but not installed."
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not installed."
  echo "Install it with: brew install jq (macOS) or apt-get install jq (Linux)"
  exit 1
fi

# ---------- Load auth token ----------

AUTH_TOKEN_FILE="$HOME/.voxherd/auth_token"
AUTH_HEADER=""

if [ -f "$AUTH_TOKEN_FILE" ]; then
  AUTH_TOKEN=$(cat "$AUTH_TOKEN_FILE" | tr -d '[:space:]')
  AUTH_HEADER="-H \"Authorization: Bearer $AUTH_TOKEN\""
  echo "Loaded auth token from $AUTH_TOKEN_FILE"
else
  echo "Warning: No auth token found at $AUTH_TOKEN_FILE"
  echo "If the bridge requires auth, tests will fail with 401."
fi

# Helper: curl with auth
authed_curl() {
  if [ -n "$AUTH_TOKEN" ]; then
    curl -s -H "Authorization: Bearer $AUTH_TOKEN" "$@"
  else
    curl -s "$@"
  fi
}

# ---------- Check bridge is running ----------

if ! authed_curl http://localhost:7777/api/sessions &>/dev/null; then
  echo "Error: Bridge server is not running on port 7777 (or auth failed)."
  echo "Start it first with: scripts/dev-setup.sh"
  exit 1
fi

# ---------- State ----------

TESTS_PASSED=0
TESTS_TOTAL=5
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ---------- Cleanup trap ----------

cleanup() {
  # Nothing to clean up currently, but this is here for future use
  # (e.g., killing background processes or removing temp files).
  :
}
trap cleanup EXIT

# ---------- Helpers ----------

pass_test() {
  local name="$1"
  TESTS_PASSED=$((TESTS_PASSED + 1))
  echo -e "\033[32mPASS: $name\033[0m"
}

fail_test() {
  local name="$1"
  local reason="$2"
  echo -e "\033[31mFAIL: $name: $reason\033[0m"
  exit 1
}

# ---------- Test 1: Register a fake session ----------

echo ""
echo "--- Test 1: Register a fake session ---"

RESPONSE=$(authed_curl -X POST http://localhost:7777/api/sessions/register \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg session_id "test-flow-001" \
    --arg project "test-project" \
    --arg project_dir "/tmp/test-project" \
    --arg status "active" \
    --arg timestamp "$TIMESTAMP" \
    '{session_id: $session_id, project: $project, project_dir: $project_dir, status: $status, timestamp: $timestamp}'
  )")

if echo "$RESPONSE" | jq -e '.ok' &>/dev/null; then
  pass_test "Register session"
else
  fail_test "Register session" "Expected response to contain 'ok', got: $RESPONSE"
fi

# ---------- Test 2: List sessions ----------

echo "--- Test 2: List sessions ---"

RESPONSE=$(authed_curl http://localhost:7777/api/sessions)

if echo "$RESPONSE" | jq -e '.' &>/dev/null && echo "$RESPONSE" | grep -q "test-flow-001"; then
  pass_test "List sessions"
else
  fail_test "List sessions" "Expected response to contain 'test-flow-001', got: $RESPONSE"
fi

# ---------- Test 3: Send a stop event ----------

echo "--- Test 3: Send a stop event ---"

RESPONSE=$(authed_curl -X POST http://localhost:7777/api/events \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg event "stop" \
    --arg session_id "test-flow-001" \
    --arg project "test-project" \
    --arg summary "Completed the test task successfully" \
    --arg stop_reason "completed" \
    --arg timestamp "$TIMESTAMP" \
    '{event: $event, session_id: $session_id, project: $project, summary: $summary, stop_reason: $stop_reason, timestamp: $timestamp}'
  )")

if echo "$RESPONSE" | jq -e '.ok' &>/dev/null; then
  pass_test "Send stop event"
else
  fail_test "Send stop event" "Expected response to contain 'ok', got: $RESPONSE"
fi

# ---------- Test 4: Get project summary ----------

echo "--- Test 4: Get project summary ---"

RESPONSE=$(authed_curl http://localhost:7777/api/sessions/test-project/summary)

if echo "$RESPONSE" | grep -q "Completed the test task successfully"; then
  pass_test "Get project summary"
else
  fail_test "Get project summary" "Expected summary to contain 'Completed the test task successfully', got: $RESPONSE"
fi

# ---------- Test 5: Send a notification event ----------

echo "--- Test 5: Send a notification event ---"

RESPONSE=$(authed_curl -X POST http://localhost:7777/api/events \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg event "notification" \
    --arg session_id "test-flow-001" \
    --arg project "test-project" \
    --arg message "Permission requested" \
    --arg timestamp "$TIMESTAMP" \
    '{event: $event, session_id: $session_id, project: $project, message: $message, timestamp: $timestamp}'
  )")

if echo "$RESPONSE" | jq -e '.ok' &>/dev/null; then
  pass_test "Send notification event"
else
  fail_test "Send notification event" "Expected response to contain 'ok', got: $RESPONSE"
fi

# ---------- Summary ----------

echo ""
echo "=============================="
echo "$TESTS_PASSED/$TESTS_TOTAL tests passed"
echo "=============================="
echo ""
