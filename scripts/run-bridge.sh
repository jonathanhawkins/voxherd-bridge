#!/usr/bin/env bash
# Launch the bridge server directly (used inside tmux sessions).
# For managed tmux lifecycle, use: python -m bridge start
cd "$(dirname "$(dirname "$(readlink -f "$0")")")"
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$HOME/.claude/local:$PATH"
exec bridge/.venv/bin/python -m bridge run --tts --listen --listen-timeout 15
