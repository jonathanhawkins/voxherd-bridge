#!/usr/bin/env bash
# install.sh - Install VoxHerd hooks for supported assistant CLIs.
#
# By default, auto-detects which CLIs (Claude, Codex, Gemini) are installed
# and configures hooks for all of them. Falls back to Claude if none detected.
#
# Usage:
#   bash hooks/install.sh                          # auto-detect all installed CLIs
#   HOOK_AGENTS=codex bash hooks/install.sh        # install only for Codex
#   HOOK_AGENTS=claude,codex bash hooks/install.sh # install for specific CLIs
#
# Notes:
# - Claude + Gemini use lifecycle hooks (JSON via stdin) in ~/.claude/ and ~/.gemini/.
# - Codex uses a `notify` handler (JSON via argv) configured in ~/.codex/config.toml.

set -e

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not installed."
  echo "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SCRIPTS=(
  "on-stop.py"
  "on-stop.sh"
  "on-session-start.sh"
  "on-notification.sh"
  "on-subagent-start.sh"
  "on-subagent-stop.sh"
  "codex-notify.py"
)

VOXHERD_DIR="$HOME/.voxherd"
HOOKS_DEST="$VOXHERD_DIR/hooks"
LOGS_DIR="$VOXHERD_DIR/logs"

RAW_HOOK_AGENTS="${HOOK_AGENTS:-}"

normalize_agents() {
  local raw="$1"
  raw="${raw//,/ }"
  local out=()
  local seen=" "
  local item
  for item in $raw; do
    local agent
    agent="$(printf "%s" "$item" | tr '[:upper:]' '[:lower:]')"
    [ -z "$agent" ] && continue
    case " $seen " in
      *" $agent "*) continue ;;
    esac
    out+=("$agent")
    seen="$seen$agent "
  done
  echo "${out[@]}"
}

# If HOOK_AGENTS was explicitly set, use that list. Otherwise auto-detect
# every supported CLI that is installed on this machine.
if [ -n "$RAW_HOOK_AGENTS" ]; then
  read -r -a TARGET_AGENTS <<< "$(normalize_agents "$RAW_HOOK_AGENTS")"
else
  TARGET_AGENTS=()
  command -v claude &>/dev/null && TARGET_AGENTS+=("claude")
  command -v codex  &>/dev/null && TARGET_AGENTS+=("codex")
  command -v gemini &>/dev/null && TARGET_AGENTS+=("gemini")
  # If none detected, default to claude (it may be installed later)
  if [ "${#TARGET_AGENTS[@]}" -eq 0 ]; then
    TARGET_AGENTS=("claude")
  fi
fi

echo "Creating directories..."
mkdir -p "$HOOKS_DEST" "$LOGS_DIR"

echo "Copying hook scripts to $HOOKS_DEST..."
for script in "${HOOK_SCRIPTS[@]}"; do
  src="$SCRIPT_DIR/$script"
  if [ ! -f "$src" ]; then
    echo "Error: expected script not found: $src"
    exit 1
  fi
  cp "$src" "$HOOKS_DEST/$script"
  chmod +x "$HOOKS_DEST/$script"
  echo "  Installed: $script"
done

merge_hook() {
  local settings="$1"
  local hook_type="$2"
  local vc_entry="$3"
  local marker="$4"

  settings="$(echo "$settings" | jq 'if .hooks == null then .hooks = {} else . end')"
  local key_exists
  key_exists="$(echo "$settings" | jq --arg key "$hook_type" '.hooks | has($key)')"

  if [ "$key_exists" = "false" ]; then
    echo "$settings" | jq --arg key "$hook_type" --argjson entry "[$vc_entry]" '.hooks[$key] = $entry'
  else
    local already_present
    already_present="$(echo "$settings" | jq --arg key "$hook_type" --arg marker "$marker" '
      .hooks[$key] | any(.hooks[]? | .command // "" | contains($marker))
    ')"
    if [ "$already_present" = "true" ]; then
      echo "$settings"
    else
      echo "$settings" | jq --arg key "$hook_type" --argjson entry "$vc_entry" '.hooks[$key] += [$entry]'
    fi
  fi
}

read_settings() {
  local path="$1"
  mkdir -p "$(dirname "$path")"
  if [ ! -f "$path" ]; then
    echo '{}' > "$path"
  fi
  local current
  current="$(cat "$path")"
  if ! echo "$current" | jq empty 2>/dev/null; then
    echo "Error: $path contains invalid JSON. Please fix it and re-run."
    exit 1
  fi
  echo "$current"
}

write_settings() {
  local path="$1"
  local content="$2"
  local tmp
  tmp="$(mktemp "${path}.XXXXXX")"
  if echo "$content" | jq '.' > "$tmp"; then
    mv "$tmp" "$path"
  else
    rm -f "$tmp"
    echo "Error: failed to write updated settings: $path"
    exit 1
  fi
}

install_claude_hooks() {
  local settings_path="$HOME/.claude/settings.json"
  local current updated
  current="$(read_settings "$settings_path")"

  local VC_STOP VC_NOTIFICATION VC_SESSION_START VC_SUBAGENT_START VC_SUBAGENT_STOP
  VC_STOP='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=claude python3 $HOME/.voxherd/hooks/on-stop.py"}]}'
  VC_NOTIFICATION='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=claude $HOME/.voxherd/hooks/on-notification.sh"}]}'
  VC_SESSION_START='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=claude $HOME/.voxherd/hooks/on-session-start.sh"}]}'
  VC_SUBAGENT_START='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=claude $HOME/.voxherd/hooks/on-subagent-start.sh"}]}'
  VC_SUBAGENT_STOP='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=claude $HOME/.voxherd/hooks/on-subagent-stop.sh"}]}'

  updated="$(echo "$current" | jq 'del(.["hooks.Stop"], .["hooks.Notification"], .["hooks.SessionStart"], .["hooks.SubagentStart"], .["hooks.SubagentStop"])')"
  updated="$(merge_hook "$updated" "Stop" "$VC_STOP" "on-stop.py")"
  updated="$(merge_hook "$updated" "Notification" "$VC_NOTIFICATION" "on-notification.sh")"
  updated="$(merge_hook "$updated" "SessionStart" "$VC_SESSION_START" "on-session-start.sh")"
  updated="$(merge_hook "$updated" "SubagentStart" "$VC_SUBAGENT_START" "on-subagent-start.sh")"
  updated="$(merge_hook "$updated" "SubagentStop" "$VC_SUBAGENT_STOP" "on-subagent-stop.sh")"
  write_settings "$settings_path" "$updated"
  echo "$settings_path"
}

install_gemini_hooks() {
  local settings_path="$HOME/.gemini/settings.json"
  local current updated
  current="$(read_settings "$settings_path")"

  # Gemini hook lifecycle differs from Claude:
  # - SessionStart: session bootstrap
  # - AfterAgent: post-turn completion (maps to VoxHerd "stop")
  # - Notification: approval/attention alerts
  local VC_AFTER_AGENT VC_NOTIFICATION VC_SESSION_START
  VC_AFTER_AGENT='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=gemini python3 $HOME/.voxherd/hooks/on-stop.py"}]}'
  VC_NOTIFICATION='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=gemini $HOME/.voxherd/hooks/on-notification.sh"}]}'
  VC_SESSION_START='{"matcher": "", "hooks": [{"type": "command", "command": "VOXHERD_HOOK_ASSISTANT=gemini $HOME/.voxherd/hooks/on-session-start.sh"}]}'

  updated="$(echo "$current" | jq 'del(.["hooks.AfterAgent"], .["hooks.Notification"], .["hooks.SessionStart"])')"
  updated="$(merge_hook "$updated" "AfterAgent" "$VC_AFTER_AGENT" "on-stop.py")"
  updated="$(merge_hook "$updated" "Notification" "$VC_NOTIFICATION" "on-notification.sh")"
  updated="$(merge_hook "$updated" "SessionStart" "$VC_SESSION_START" "on-session-start.sh")"
  write_settings "$settings_path" "$updated"
  echo "$settings_path"
}

install_codex_hooks() {
  # Codex uses a TOML config at ~/.codex/config.toml with a `notify` key.
  # The notify script receives a JSON argument via sys.argv[1] (not stdin).
  local config_dir="$HOME/.codex"
  local config_path="$config_dir/config.toml"
  mkdir -p "$config_dir"

  local notify_cmd="python3 $HOME/.voxherd/hooks/codex-notify.py"

  if [ -f "$config_path" ]; then
    # Check if notify is already configured
    if grep -q "codex-notify.py" "$config_path" 2>/dev/null; then
      echo "$config_path (already configured)"
      return
    fi

    # Check if there's an existing notify line — replace it or append
    if grep -q '^notify\s*=' "$config_path" 2>/dev/null; then
      # Back up and replace the existing notify line
      cp "$config_path" "${config_path}.bak"
      sed -i.tmp "s|^notify\s*=.*|notify = [\"python3\", \"$HOME/.voxherd/hooks/codex-notify.py\"]|" "$config_path"
      rm -f "${config_path}.tmp"
    else
      # Append notify config
      printf '\n# VoxHerd: notify bridge on agent-turn-complete\nnotify = ["python3", "%s/.voxherd/hooks/codex-notify.py"]\n' "$HOME" >> "$config_path"
    fi
  else
    # Create config with notify setting
    cat > "$config_path" <<TOML
# Codex CLI configuration
# See: https://developers.openai.com/codex/config-reference

# VoxHerd: notify bridge on agent-turn-complete
notify = ["python3", "$HOME/.voxherd/hooks/codex-notify.py"]
TOML
  fi

  echo "$config_path"
}

INSTALLED=()
SKIPPED=()

for agent in "${TARGET_AGENTS[@]}"; do
  case "$agent" in
    claude)
      echo "Updating Claude settings..."
      INSTALLED+=("claude:$(install_claude_hooks)")
      ;;
    gemini)
      echo "Updating Gemini settings..."
      INSTALLED+=("gemini:$(install_gemini_hooks)")
      ;;
    codex)
      echo "Updating Codex settings..."
      INSTALLED+=("codex:$(install_codex_hooks)")
      ;;
    *)
      SKIPPED+=("$agent (unsupported)")
      ;;
  esac
done

echo
echo "VoxHerd hooks installed successfully."
echo
echo "  Hook scripts:  $HOOKS_DEST/"
for script in "${HOOK_SCRIPTS[@]}"; do
  echo "    - $script"
done
echo "  Log directory: $LOGS_DIR/"
echo

if [ "${#INSTALLED[@]}" -gt 0 ]; then
  echo "Configured assistant settings:"
  for item in "${INSTALLED[@]}"; do
    echo "  - $item"
  done
fi

if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo
  echo "Skipped:"
  for item in "${SKIPPED[@]}"; do
    echo "  - $item"
  done
fi

echo
echo "To uninstall, remove VoxHerd entries from the configured settings files"
echo "and delete $VOXHERD_DIR/"
