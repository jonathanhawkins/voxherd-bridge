# VoxHerd Bridge

Voice-first bridge server for multi-agent Claude Code orchestration. Two components in this repo: the Python bridge server and bash/PowerShell hook scripts.

## Architecture

```
Claude Code hooks (Stop/Notification/SessionStart) --> HTTP POST --> Bridge Server (FastAPI :7777) --> WebSocket --> Clients
Clients --> WebSocket --> Bridge Server --> claude --resume SESSION_ID -p "message" --> Claude Code
```

## Project Layout

```
bridge/              Python CLI + FastAPI bridge server (the central nervous system)
  __main__.py        `python -m bridge` entry point
  cli.py             CLI: `voxherd start`, arg parsing, rich live event log
  bridge_server.py   Main server: REST endpoints + WebSocket + command dispatch
  session_manager.py Session state: active/idle/waiting per Claude Code instance
  tests/             pytest test suite
hooks/               Bash/PowerShell scripts installed to ~/.voxherd/hooks/
  on-stop.sh         Generates Haiku summary, POSTs to bridge
  on-session-start.sh  Registers session with bridge
  on-notification.sh   Forwards permission requests
  install.sh         Deploys hooks + patches ~/.claude/settings.json
scripts/             Dev tooling
  dev-setup.sh       One-command local setup
  test-flow.sh       End-to-end mock event test
  build-linux-package.sh  Build self-contained tarball for Linux
  install-linux.sh   Linux installer (venv + hooks + systemd)
docs/                Documentation
  prd.md             Product requirements (source of truth for scope)
  api.md             REST + WebSocket API reference
```

## Tech Stack

**Bridge server:** Python 3.11+, FastAPI, uvicorn, websockets, `rich` for terminal output. CLI-first -- runs in a terminal window with a live color-coded event stream. Async throughout -- `asyncio.create_subprocess_exec` for spawning `claude --resume`. No database; sessions held in memory (single-user system, restarts are fine). No web UI, no browser dashboard.

**Hook scripts:** Bash (macOS/Linux) and PowerShell (Windows). Read JSON from stdin via `jq` (bash) or `ConvertFrom-Json` (PowerShell). Call `claude -p` with `--model claude-haiku-4-5-20251001` for summaries. POST to `http://localhost:7777/api/events` via `curl`.

## Running Locally

**Bridge server (recommended: run in tmux for TCC mic access on macOS):**
```bash
# First time setup:
cd bridge && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Run:
python -m bridge run --tts
```
Flags: `--tts` enables platform TTS, `--listen` enables Mac STT (off by default), `--wake-word` enables wake word.

Starts FastAPI on port 7777 with a live event log. Colored output via `rich`: green for completions, yellow for dispatches, red for errors, cyan for permission requests.

**Install hooks:**
```bash
bash hooks/install.sh
```

**Quick validation:**
```bash
# Watch bridge events
wscat -c ws://localhost:7777/ws/ios

# Simulate a stop event
curl -X POST http://localhost:7777/api/events \
  -H "Content-Type: application/json" \
  -d '{"event":"stop","session_id":"test-123","project":"myproject","summary":"Built the thing","stop_reason":"end_turn"}'

# Check sessions
curl http://localhost:7777/api/sessions
```

## Conventions

**Python (bridge):** Async functions for anything that touches I/O. Type hints on function signatures. No classes for simple data -- use dicts or dataclasses. FastAPI dependency injection for shared state.

**Bash (hooks):** Defensive scripting -- quote variables, check for `jq` availability, fail silently on network errors (the hook must not block Claude Code). Log errors to `~/.voxherd/logs/`.

## Key Patterns

**Session lifecycle:** SessionStart hook registers with bridge -> session status = "active" -> commands dispatch via `claude --resume` -> Stop hook fires -> Haiku summarizes -> bridge updates status to "idle" -> client gets event.

**Command dispatch:** `asyncio.create_subprocess_exec("claude", "--resume", session_id, "-p", message, "--output-format", "stream-json", cwd=project_dir)`. Fire and forget -- the Stop hook will notify when it finishes.

**Project matching:** Fuzzy. "aligned" matches "aligned-tools", "quant" matches "quantcoach". Use Levenshtein distance or substring containment, not exact match.

## Important Constraints

- **No web UI, no browser dashboard.** The bridge is a CLI tool with terminal output.
- **No auth for MVP.** Single-user on a private network. Token auth is optional (auto-generated if desired).
- **Hooks must not block Claude Code.** If the bridge server is down, hooks should fail silently and fast.
- **Haiku for summaries.** Speed matters. Summaries are 1-2 sentences max.
- **In-memory session state only.** No database. Sessions are ephemeral.

## Testing

**Bridge server:** `pytest` with `httpx.AsyncClient` for REST endpoints. Mock `asyncio.create_subprocess_exec` to avoid actually spawning Claude Code in tests.

```bash
cd bridge && source .venv/bin/activate && pytest
```

**Hook scripts:** Test with `echo '{"session_id":"x","cwd":"/tmp","transcript_path":"/tmp/t"}' | bash hooks/on-stop.sh` against a running bridge.

**End-to-end:** `scripts/test-flow.sh` simulates the full cycle: register a fake session, send a stop event, verify the bridge handles it.

## Linux Deployment

Build a self-contained tarball and deploy as a systemd user service:

```bash
bash scripts/build-linux-package.sh
# Produces voxherd-bridge.tar.gz

# On the Linux machine:
tar xzf voxherd-bridge.tar.gz && cd voxherd-bridge && bash install.sh
```

The installer supports Arch (pacman), Ubuntu/Debian (apt), and Fedora (dnf). It sets up a Python venv, installs hooks, generates an auth token, and enables a systemd user service.
