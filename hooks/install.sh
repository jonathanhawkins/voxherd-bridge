#!/usr/bin/env bash
# install.sh - Install VoxHerd hooks for supported assistant CLIs.
#
# By default, auto-detects which CLIs (Claude, Codex, Gemini, Grok, Antigravity) are installed
# and configures hooks for all of them. Falls back to Claude if none detected.
#
# Usage:
#   bash hooks/install.sh                          # auto-detect all installed CLIs
#   HOOK_AGENTS=codex bash hooks/install.sh        # install only for Codex
#   HOOK_AGENTS=claude,codex bash hooks/install.sh # install for specific CLIs
#   HOOK_AGENTS=grok bash hooks/install.sh         # Grok Build (Grok 4.5 + Composer)
#   HOOK_AGENTS=antigravity bash hooks/install.sh  # Antigravity CLI (`agy`)
#
#   MODEL_PROFILE=google bash hooks/install.sh     # Gemini 3.8 Flash via agy for summaries + NLU
#   MODEL_PROFILE=anthropic bash hooks/install.sh  # Claude Haiku (the default)
#
# Notes:
# - Claude + Gemini use lifecycle hooks (JSON via stdin) in ~/.claude/ and ~/.gemini/.
# - Codex uses a `notify` handler (JSON via argv) configured in ~/.codex/config.toml.
# - Grok Build uses lifecycle hooks as JSON files under ~/.grok/hooks/*.json.
# - Antigravity (`agy`) reads named hooks from ~/.gemini/config/hooks.json. It has
#   a Stop event only -- no SessionStart -- so the bridge registers from the first Stop.
# - MODEL_PROFILE picks the small model the bridge itself uses for stop-hook
#   summaries and intent parsing. It is an install-time choice, not a per-request
#   one, and it is recorded in ~/.voxherd/models.env. Omit it and nothing is
#   written, which means the hardcoded Claude Haiku default.

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
  "antigravity-hook.py"
  "agy_transcript.py"
  "model-profile.sh"
  "ModelProfile.ps1"
)

VOXHERD_DIR="$HOME/.voxherd"
HOOKS_DEST="$VOXHERD_DIR/hooks"
LOGS_DIR="$VOXHERD_DIR/logs"
MODELS_ENV="$VOXHERD_DIR/models.env"

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
  command -v agy    &>/dev/null && TARGET_AGENTS+=("antigravity")
  # Grok Build CLI may live only under ~/.grok/bin (not always on PATH).
  if command -v grok &>/dev/null || [ -x "$HOME/.grok/bin/grok" ]; then
    TARGET_AGENTS+=("grok")
  fi
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

# The Python hook shares bridge/model_profile.py rather than carrying its own
# copy of the resolver. Drop it next to on-stop.py so the installed hook can
# import it. Best-effort: if it is not on disk (e.g. running from a hooks-only
# distribution), on-stop.py falls back to the hardcoded Claude Haiku default.
for candidate in \
  "$SCRIPT_DIR/../bridge/model_profile.py" \
  "$SCRIPT_DIR/model_profile.py"
do
  if [ -f "$candidate" ]; then
    cp "$candidate" "$HOOKS_DEST/model_profile.py"
    echo "  Installed: model_profile.py"
    break
  fi
done

# ---------------------------------------------------------------------------
# Model profile — which small model does summaries and intent parsing
# ---------------------------------------------------------------------------
#
# Mirrors PROFILES in bridge/model_profile.py. Keep the two in step.
# Only written when MODEL_PROFILE is set: a plain install leaves no models.env,
# which resolves to the hardcoded Claude Haiku default and is byte-for-byte
# today's behaviour.

write_models_env() {
  local profile="$1"
  local summary_cli summary_model nlu_cli nlu_model

  case "$profile" in
    google)
      # agy, not gemini: Gemini CLI no longer signs in with a personal Google
      # account (retired 2026-06-18). See PROFILES in bridge/model_profile.py.
      summary_cli="agy";     summary_model="gemini-3.8-flash-low"
      nlu_cli="agy";         nlu_model="gemini-3.8-flash-low"
      ;;
    anthropic)
      summary_cli="claude";  summary_model="claude-haiku-4-5-20251001"
      nlu_cli="claude";      nlu_model="claude-haiku-4-5-20251001"
      ;;
    *)
      echo "  Warning: unknown MODEL_PROFILE '$profile' — using 'anthropic'."
      profile="anthropic"
      summary_cli="claude";  summary_model="claude-haiku-4-5-20251001"
      nlu_cli="claude";      nlu_model="claude-haiku-4-5-20251001"
      ;;
  esac

  cat > "$MODELS_ENV" <<ENVFILE
# VoxHerd model profile — written by hooks/install.sh (MODEL_PROFILE=$profile).
# The small model used for stop-hook summaries and intent parsing. Resolved at
# install time, never per request. Environment variables of the same name
# override these for a single session.
VOXHERD_SUMMARY_CLI=$summary_cli
VOXHERD_SUMMARY_MODEL=$summary_model
VOXHERD_NLU_CLI=$nlu_cli
VOXHERD_NLU_MODEL=$nlu_model
ENVFILE
  chmod 600 "$MODELS_ENV"
  echo "  Model profile: $profile -> $MODELS_ENV"
}

if [ -n "${MODEL_PROFILE:-}" ]; then
  echo "Writing model profile..."
  write_models_env "$(printf "%s" "$MODEL_PROFILE" | tr '[:upper:]' '[:lower:]')"
elif [ -f "$MODELS_ENV" ]; then
  echo "Model profile: keeping existing $MODELS_ENV"
  echo "  (re-run with MODEL_PROFILE=google or MODEL_PROFILE=anthropic to change it)"
else
  echo "Model profile: anthropic (default — claude-haiku-4-5-20251001, no models.env written)"
fi

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

write_grok_hook_file() {
  # Write or merge a single-event hook file under ~/.grok/hooks/.
  # Soft-default VOXHERD_HOOK_ASSISTANT so Composer spawn can override via env.
  local event_name="$1"
  local command="$2"
  local marker="$3"
  local hooks_dir="$HOME/.grok/hooks"
  local path="$hooks_dir/voxherd-${event_name}.json"
  mkdir -p "$hooks_dir"

  local entry
  entry="$(jq -n \
    --arg event "$event_name" \
    --arg cmd "$command" \
    '{hooks: {($event): [{hooks: [{type: "command", command: $cmd, timeout: 30}]}]}}')"

  if [ -f "$path" ] && jq empty "$path" 2>/dev/null; then
    local already
    already="$(jq --arg marker "$marker" '
      [.hooks // {} | to_entries[] | .value[]? | .hooks[]? | .command // ""]
      | any(contains($marker))
    ' "$path")"
    if [ "$already" = "true" ]; then
      return 0
    fi
    # Merge our event key without clobbering other events in the same file
    jq --argjson entry "$entry" --arg event "$event_name" '
      .hooks = (.hooks // {})
      | .hooks[$event] = ((.hooks[$event] // []) + ($entry.hooks[$event] // []))
    ' "$path" > "${path}.tmp" && mv "${path}.tmp" "$path"
  else
    echo "$entry" | jq '.' > "$path"
  fi
}

install_grok_hooks() {
  # Grok Build discovers global hooks from ~/.grok/hooks/*.json
  # (see ~/.grok/docs/user-guide/10-hooks.md). Events match Claude: SessionStart,
  # Stop, Notification, SubagentStart, SubagentStop.
  # Soft-default assistant=grok so VOXHERD_HOOK_ASSISTANT=composer from spawn env
  # is preserved when present.
  local soft_env='export VOXHERD_HOOK_ASSISTANT="${VOXHERD_HOOK_ASSISTANT:-grok}"'
  local stop_cmd notif_cmd sess_cmd sub_start_cmd sub_stop_cmd

  stop_cmd="${soft_env}; python3 \"\$HOME/.voxherd/hooks/on-stop.py\""
  notif_cmd="${soft_env}; \"\$HOME/.voxherd/hooks/on-notification.sh\""
  sess_cmd="${soft_env}; \"\$HOME/.voxherd/hooks/on-session-start.sh\""
  sub_start_cmd="${soft_env}; \"\$HOME/.voxherd/hooks/on-subagent-start.sh\""
  sub_stop_cmd="${soft_env}; \"\$HOME/.voxherd/hooks/on-subagent-stop.sh\""

  write_grok_hook_file "Stop" "$stop_cmd" "on-stop.py"
  write_grok_hook_file "Notification" "$notif_cmd" "on-notification.sh"
  write_grok_hook_file "SessionStart" "$sess_cmd" "on-session-start.sh"
  write_grok_hook_file "SubagentStart" "$sub_start_cmd" "on-subagent-start.sh"
  write_grok_hook_file "SubagentStop" "$sub_stop_cmd" "on-subagent-stop.sh"

  echo "$HOME/.grok/hooks/"
}

install_antigravity_hooks() {
  # agy reads named hooks from ~/.gemini/config/hooks.json -- the file its /hooks
  # command and its backend share (agy 1.2.10 changelog). Each top-level key is a
  # hook name, so ours is "voxherd" and is replaced on reinstall; other hooks in
  # the file are never touched. Stop fires when the agent loop ends a turn. The
  # handler answers `{}` at once and summarises in the background, because agy
  # runs hooks synchronously; the timeout only bounds something going wrong.
  local path="$HOME/.gemini/config/hooks.json"
  local entry current
  entry='{"Stop": [{"type": "command", "command": "python3 $HOME/.voxherd/hooks/antigravity-hook.py", "timeout": 10}]}'
  mkdir -p "$(dirname "$path")"
  current='{}'
  # agy leaves empty config files behind (mcp_config.json is 0 bytes); an
  # empty or blank hooks.json is a file with no hooks yet, not an error.
  if [ -f "$path" ] && [ -n "$(tr -d '[:space:]' < "$path")" ]; then
    current="$(cat "$path")"
    if ! echo "$current" | jq -e 'type == "object"' >/dev/null 2>&1; then
      echo "Error: $path is not a JSON object. Please fix it and re-run." >&2
      exit 1
    fi
  fi
  write_settings "$path" "$(echo "$current" | jq --argjson entry "$entry" '.voxherd = $entry')"
  echo "$path"
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
    grok|composer)
      # Both Grok 4.5 and Composer use the Grok Build CLI + ~/.grok/hooks.
      echo "Updating Grok Build hooks..."
      INSTALLED+=("grok:$(install_grok_hooks)")
      ;;
    antigravity|agy)
      echo "Updating Antigravity hooks..."
      INSTALLED+=("antigravity:$(install_antigravity_hooks)")
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
