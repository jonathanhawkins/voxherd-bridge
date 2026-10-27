#!/bin/bash
# model-profile.sh — resolve the install-time model profile for bash hooks.
#
# Source this, do not execute it:
#
#   . "$(dirname "$0")/model-profile.sh"
#   voxherd_resolve_models
#   voxherd_summary_argv "$PROMPT"
#   tail -50 "$T" | timeout 15 "${VOXHERD_SUMMARY_ARGV[@]}"
#
# Resolution order, highest first: process environment, then ~/.voxherd/models.env
# (written by hooks/install.sh), then the hardcoded Claude Haiku defaults.
#
# This is the bash twin of bridge/model_profile.py — the same rules in two
# languages. Change one, change the other.
#
# Three things matter here:
#   1. NEVER `set -e`, and never fail. A missing, unreadable or corrupt
#      models.env degrades to Claude Haiku. A hook must not block the assistant.
#   2. models.env is NOT sourced. It is parsed line by line with no expansion
#      and no command substitution, so a hostile file cannot run anything.
#   3. The CLI name is matched against an allowlist, never interpolated.
#
# Running this file directly prints the resolved profile — a debug aid, and
# what bridge/tests/test_model_profile.py drives to check bash/Python parity.

VOXHERD_DEFAULT_CLI="claude"
VOXHERD_DEFAULT_MODEL="claude-haiku-4-5-20251001"

# `codex exec` is a full agent run; a one-shot call turns most of it off. Same
# flags, same order, as _CODEX_EXEC_ARGS in bridge/model_profile.py, which says
# why each one is there. In short: --ignore-user-config keeps the user's MCP
# servers and VoxHerd's own Codex notify hook out of the call, --ephemeral keeps
# it out of their session history. A `--` goes ahead of the prompt, or a prompt
# that begins with "-" is read as an option.
_VH_CODEX_EXEC_ARGS=(
  "codex" "exec"
  "--ephemeral"
  "--ignore-user-config"
  "--skip-git-repo-check"
  "-s" "read-only"
  "--color" "never"
  "-c" 'model_reasoning_effort="low"'
)

# --ignore-user-config also drops where the sign-in is kept, so a keyring sign-in
# would look signed out. _vh_codex_auth_store prints the user's own
# cli_auth_credentials_store -- a top-level key only, a quoted value from this
# list only -- and nothing at all otherwise. Twin of codex_auth_store().
_VH_CODEX_AUTH_RE='^[[:space:]]*cli_auth_credentials_store[[:space:]]*=[[:space:]]*("([a-z]+)"|'"'"'([a-z]+)'"'"')[[:space:]]*(#.*)?$'

_vh_codex_auth_store() {
  local file line value
  if [ -n "${CODEX_HOME:-}" ]; then
    file="$CODEX_HOME/config.toml"
  else
    file="$HOME/.codex/config.toml"
  fi
  [ -f "$file" ] || return 0
  [ -r "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(_vh_trim "$line")" in
      \[*) return 0 ;;  # past the top level: [profiles.x] keys are not the user's own
    esac
    if [[ "$line" =~ $_VH_CODEX_AUTH_RE ]]; then
      value="${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
      case "$value" in
        file|keyring|auto) printf '%s' "$value" ;;
      esac
      return 0
    fi
  done < "$file" 2>/dev/null
  return 0
}

# --- validation ------------------------------------------------------------

# Only these CLIs may be spawned. Anything else is ignored and the default wins.
_vh_valid_cli() {
  case "$1" in
    claude|gemini|codex|agy) return 0 ;;
    *) return 1 ;;
  esac
}

# Model ids are letters, digits and . _ - : only — narrow on purpose, so shell
# metacharacters and whitespace can never reach an argv we build.
_vh_valid_model() {
  [ -n "$1" ] || return 1
  [ "${#1}" -le 128 ] || return 1
  case "$1" in
    *[!A-Za-z0-9._:-]*) return 1 ;;
  esac
  case "$1" in
    [A-Za-z0-9]*) return 0 ;;
    *) return 1 ;;
  esac
}

_vh_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

_vh_trim() {
  local s="$1"
  s="${s%$'\r'}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# --- models.env ------------------------------------------------------------

voxherd_models_env_path() {
  if [ -n "${VOXHERD_MODELS_ENV:-}" ]; then
    printf '%s' "$VOXHERD_MODELS_ENV"
  else
    printf '%s' "$HOME/.voxherd/models.env"
  fi
}

# _vh_read_models_env — parse models.env into the _VH_FILE_* shell variables.
# Unknown keys, junk lines and values that fail validation are all dropped
# silently; the caller then falls through to the default.
_vh_read_models_env() {
  _VH_FILE_SUMMARY_CLI=""
  _VH_FILE_SUMMARY_MODEL=""
  _VH_FILE_NLU_CLI=""
  _VH_FILE_NLU_MODEL=""

  local file
  file="$(voxherd_models_env_path)"
  [ -f "$file" ] || return 0
  [ -r "$file" ] || return 0

  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(_vh_trim "$line")"
    [ -n "$line" ] || continue
    case "$line" in
      \#*) continue ;;
      "export "*) line="$(_vh_trim "${line#export }")" ;;
    esac
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac
    key="$(_vh_trim "${line%%=*}")"
    value="$(_vh_trim "${line#*=}")"
    # Strip one layer of matching quotes, the way an .env file is written.
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    value="$(_vh_trim "$value")"
    [ -n "$value" ] || continue

    case "$key" in
      VOXHERD_SUMMARY_CLI)
        value="$(_vh_lower "$value")"
        _vh_valid_cli "$value" && _VH_FILE_SUMMARY_CLI="$value"
        ;;
      VOXHERD_NLU_CLI)
        value="$(_vh_lower "$value")"
        _vh_valid_cli "$value" && _VH_FILE_NLU_CLI="$value"
        ;;
      VOXHERD_SUMMARY_MODEL)
        _vh_valid_model "$value" && _VH_FILE_SUMMARY_MODEL="$value"
        ;;
      VOXHERD_NLU_MODEL)
        _vh_valid_model "$value" && _VH_FILE_NLU_MODEL="$value"
        ;;
    esac
  done < "$file" 2>/dev/null

  return 0
}

# --- resolution ------------------------------------------------------------

# voxherd_resolve_models — sets (and exports):
#   VOXHERD_SUMMARY_CLI  VOXHERD_SUMMARY_MODEL
#   VOXHERD_NLU_CLI      VOXHERD_NLU_MODEL
# plus VOXHERD_SUMMARY_CLI_SOURCE / VOXHERD_NLU_CLI_SOURCE = default|file|env.
voxherd_resolve_models() {
  _vh_read_models_env

  local env_cli env_model

  # summary CLI
  env_cli="$(_vh_lower "${VOXHERD_SUMMARY_CLI:-}")"
  if _vh_valid_cli "$env_cli"; then
    VOXHERD_SUMMARY_CLI="$env_cli"
    VOXHERD_SUMMARY_CLI_SOURCE="env"
  elif [ -n "$_VH_FILE_SUMMARY_CLI" ]; then
    VOXHERD_SUMMARY_CLI="$_VH_FILE_SUMMARY_CLI"
    VOXHERD_SUMMARY_CLI_SOURCE="file"
  else
    VOXHERD_SUMMARY_CLI="$VOXHERD_DEFAULT_CLI"
    VOXHERD_SUMMARY_CLI_SOURCE="default"
  fi

  # summary model
  env_model="$(_vh_trim "${VOXHERD_SUMMARY_MODEL:-}")"
  if _vh_valid_model "$env_model"; then
    VOXHERD_SUMMARY_MODEL="$env_model"
  elif [ -n "$_VH_FILE_SUMMARY_MODEL" ]; then
    VOXHERD_SUMMARY_MODEL="$_VH_FILE_SUMMARY_MODEL"
  else
    VOXHERD_SUMMARY_MODEL="$VOXHERD_DEFAULT_MODEL"
  fi

  # NLU CLI
  env_cli="$(_vh_lower "${VOXHERD_NLU_CLI:-}")"
  if _vh_valid_cli "$env_cli"; then
    VOXHERD_NLU_CLI="$env_cli"
    VOXHERD_NLU_CLI_SOURCE="env"
  elif [ -n "$_VH_FILE_NLU_CLI" ]; then
    VOXHERD_NLU_CLI="$_VH_FILE_NLU_CLI"
    VOXHERD_NLU_CLI_SOURCE="file"
  else
    VOXHERD_NLU_CLI="$VOXHERD_DEFAULT_CLI"
    VOXHERD_NLU_CLI_SOURCE="default"
  fi

  # NLU model
  env_model="$(_vh_trim "${VOXHERD_NLU_MODEL:-}")"
  if _vh_valid_model "$env_model"; then
    VOXHERD_NLU_MODEL="$env_model"
  elif [ -n "$_VH_FILE_NLU_MODEL" ]; then
    VOXHERD_NLU_MODEL="$_VH_FILE_NLU_MODEL"
  else
    VOXHERD_NLU_MODEL="$VOXHERD_DEFAULT_MODEL"
  fi

  export VOXHERD_SUMMARY_CLI VOXHERD_SUMMARY_MODEL VOXHERD_NLU_CLI VOXHERD_NLU_MODEL
  return 0
}

# voxherd_model_flag <cli> — the flag that CLI spells "--model" with.
# claude and agy want --model; gemini and codex want -m. Getting this wrong
# silently falls back to the CLI's own default model, which is slower and pricier.
voxherd_model_flag() {
  case "$1" in
    gemini|codex) printf '%s' "-m" ;;
    *) printf '%s' "--model" ;;
  esac
}

# _vh_build_argv <cli> <model> <prompt> — fills the _VH_ARGV array.
_vh_build_argv() {
  local cli="$1" model="$2" prompt="$3" flag
  _vh_valid_cli "$cli" || cli="$VOXHERD_DEFAULT_CLI"
  _vh_valid_model "$model" || model="$VOXHERD_DEFAULT_MODEL"
  flag="$(voxherd_model_flag "$cli")"
  if [ "$cli" = "codex" ]; then
    # `codex exec` takes the prompt positionally, after the flags and a `--`.
    local store
    store="$(_vh_codex_auth_store)"
    _VH_ARGV=("${_VH_CODEX_EXEC_ARGS[@]}")
    if [ -n "$store" ]; then
      _VH_ARGV+=("-c" "cli_auth_credentials_store=\"$store\"")
    fi
    _VH_ARGV+=("$flag" "$model" "--" "$prompt")
  else
    _VH_ARGV=("$cli" "-p" "$prompt" "$flag" "$model")
  fi
  # agy is an agent first: a prompt starting with "/" would run as one of its
  # slash commands or skills.
  if [ "$cli" = "agy" ]; then
    _VH_ARGV+=("--disable-slash-commands")
  fi
}

# voxherd_summary_argv <prompt> — fills the VOXHERD_SUMMARY_ARGV array.
voxherd_summary_argv() {
  [ -n "${VOXHERD_SUMMARY_CLI:-}" ] || voxherd_resolve_models
  _vh_build_argv "$VOXHERD_SUMMARY_CLI" "$VOXHERD_SUMMARY_MODEL" "$1"
  VOXHERD_SUMMARY_ARGV=("${_VH_ARGV[@]}")
}

# voxherd_nlu_argv <prompt> — fills the VOXHERD_NLU_ARGV array.
voxherd_nlu_argv() {
  [ -n "${VOXHERD_NLU_CLI:-}" ] || voxherd_resolve_models
  _vh_build_argv "$VOXHERD_NLU_CLI" "$VOXHERD_NLU_MODEL" "$1"
  VOXHERD_NLU_ARGV=("${_VH_ARGV[@]}")
  case "$VOXHERD_NLU_CLI" in
    claude|agy) VOXHERD_NLU_ARGV+=("--output-format" "json") ;;
    gemini) VOXHERD_NLU_ARGV+=("-o" "json") ;;
  esac
}

# --- debug / parity harness ------------------------------------------------

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  voxherd_resolve_models
  voxherd_summary_argv "${1:-PROMPT}"
  voxherd_nlu_argv "${1:-PROMPT}"
  printf 'summary_cli=%s\n' "$VOXHERD_SUMMARY_CLI"
  printf 'summary_model=%s\n' "$VOXHERD_SUMMARY_MODEL"
  printf 'summary_cli_source=%s\n' "$VOXHERD_SUMMARY_CLI_SOURCE"
  printf 'nlu_cli=%s\n' "$VOXHERD_NLU_CLI"
  printf 'nlu_model=%s\n' "$VOXHERD_NLU_MODEL"
  printf 'nlu_cli_source=%s\n' "$VOXHERD_NLU_CLI_SOURCE"
  for _vh_a in "${VOXHERD_SUMMARY_ARGV[@]}"; do printf 'summary_argv=%s\n' "$_vh_a"; done
  for _vh_a in "${VOXHERD_NLU_ARGV[@]}"; do printf 'nlu_argv=%s\n' "$_vh_a"; done
fi
