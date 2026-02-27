# VoxHerd Bridge

Voice-first bridge server for multi-agent Claude Code orchestration. Route voice commands to the right Claude Code session and get spoken status updates when agents finish work.

VoxHerd Bridge is the central nervous system that connects Claude Code hook events to mobile clients over WebSocket. It maintains a live registry of all running Claude Code sessions and dispatches commands via `claude --resume`.

**Full product:** [voxherd.com](https://voxherd.com)

```
Claude Code hooks (Stop/Notification/SessionStart)
    |
    | HTTP POST
    v
Bridge Server (FastAPI :7777) ---- WebSocket ----> Mobile / CLI clients
    |
    | claude --resume SESSION_ID -p "message"
    v
Claude Code instances (#1..#N)
```

## What's in this repo

| Directory | Contents |
|-----------|----------|
| `bridge/` | Python FastAPI bridge server -- REST API, WebSocket, session management, command dispatch |
| `hooks/` | Bash/PowerShell scripts installed into Claude Code's hook lifecycle |
| `docs/` | API reference and product requirements |
| `scripts/` | Dev setup, test automation, Linux deployment |

## Quick start

### Prerequisites

- Python 3.11+
- `jq` (for hook scripts)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (or any supported assistant CLI)
- `tmux` (required on macOS for TCC mic access; recommended everywhere)

### Install and run

```bash
# Clone
git clone https://github.com/jonathanhawkins/voxherd-bridge.git
cd voxherd-bridge

# One-command setup (creates venv, installs deps, deploys hooks, starts bridge)
bash scripts/dev-setup.sh

# Or manually:
cd bridge && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m bridge run --tts
```

The bridge starts on `http://localhost:7777` with a live color-coded event log in the terminal.

### Install hooks

```bash
bash hooks/install.sh
```

This copies hook scripts to `~/.voxherd/hooks/` and registers them in `~/.claude/settings.json`. Every Claude Code session will then report lifecycle events (start, stop, notifications) to the bridge.

### Validate without a mobile app

```bash
# Watch bridge events via WebSocket
wscat -c ws://localhost:7777/ws/ios

# Register a fake session
curl -X POST http://localhost:7777/api/sessions/register \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test-123","project":"myproject","project_dir":"/tmp/myproject"}'

# Simulate a stop event
curl -X POST http://localhost:7777/api/events \
  -H "Content-Type: application/json" \
  -d '{"event":"stop","session_id":"test-123","project":"myproject","summary":"Built the thing","stop_reason":"end_turn"}'

# Check sessions
curl http://localhost:7777/api/sessions

# Run the automated test suite
bash scripts/test-flow.sh
```

## Bridge server API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/sessions/register` | Hook registers new Claude Code instance |
| GET | `/api/sessions` | List all sessions with status |
| POST | `/api/events` | Hook events (stop, notification, subagent) |
| POST | `/api/command` | Dispatch a command to a session (REST) |
| POST | `/api/intent/parse` | Parse voice transcription to structured intent |
| WS | `/ws/ios` | Persistent client connection (events + commands) |

See [`docs/api.md`](docs/api.md) for the complete API reference including WebSocket message types.

## How hooks work

Claude Code supports lifecycle hooks that run shell scripts at key moments:

- **on-session-start.sh** -- Registers the session with the bridge on startup
- **on-stop.sh** -- Generates a 1-2 sentence summary (via Claude Haiku) and reports completion
- **on-notification.sh** -- Forwards permission requests to connected clients
- **on-subagent-start.sh / on-subagent-stop.sh** -- Tracks sub-agent lifecycle

All hooks read JSON from stdin, extract fields with `jq`, and POST to the bridge. They are designed to fail silently if the bridge is down -- a hook must never block Claude Code.

## Session lifecycle

```
SessionStart hook fires
    -> Bridge registers session (status: idle)
    -> Client gets state_sync

Voice command arrives (WebSocket or REST)
    -> Bridge dispatches: claude --resume SESSION_ID -p "message"
    -> Session status: active

Claude Code finishes
    -> Stop hook fires
    -> Haiku generates summary
    -> Bridge updates status: idle
    -> Client gets agent_event with summary
    -> TTS announces completion
```

## Linux deployment

Build a self-contained tarball and deploy as a systemd service:

```bash
# Build on dev machine
bash scripts/build-linux-package.sh

# Deploy on Linux
scp voxherd-bridge.tar.gz user@host:~/
ssh user@host 'tar xzf voxherd-bridge.tar.gz && cd voxherd-bridge && bash install.sh'
```

The installer handles Python venv setup, hook deployment, auth token generation, and systemd service installation. Supports Arch, Ubuntu/Debian, and Fedora.

## Docker

```bash
cd bridge
docker build -t voxherd-bridge .
docker run -p 7777:7777 voxherd-bridge
```

## Platform support

| Platform | TTS | STT | Status |
|----------|-----|-----|--------|
| macOS | `say` (built-in) | Custom Swift binary | Full support |
| Linux | `espeak-ng` | -- | Bridge + hooks |
| Windows | `SAPI` (COM) | -- | Bridge + hooks (PowerShell) |
| Docker | -- | -- | Bridge only |

## Multi-assistant support

VoxHerd Bridge works with multiple AI coding assistants:

- **Claude Code** -- Full support (hooks + resume)
- **Codex CLI** -- Auto-registered by bridge, command dispatch via tmux
- **Gemini CLI** -- Hook support (same hook format as Claude)

## Testing

```bash
# Python tests
cd bridge && source .venv/bin/activate
pytest

# End-to-end (requires running bridge)
bash scripts/test-flow.sh
```

## License

MIT -- see [LICENSE](LICENSE).
