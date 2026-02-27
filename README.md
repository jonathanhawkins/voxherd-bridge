# VoxHerd

**Herd your AI coding agents with your voice.**

VoxHerd is a voice-first controller for AI coding assistants. It lets you manage multiple [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://developers.openai.com/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) sessions from Meta Ray-Ban glasses, your phone, or just your laptop mic. Agents announce when they finish work via text-to-speech. You issue follow-up commands by talking. No browser tab required -- walk around, supervise, and redirect your agents by voice.

```
Voice input -> STT -> intent parse -> WebSocket -> Bridge Server (FastAPI :7777) -> assistant dispatch
Assistant hooks (lifecycle events) -> HTTP POST -> Bridge Server -> WebSocket -> TTS -> speaker
```

## Why VoxHerd?

If you run multiple AI coding sessions across different projects, you know the pain: tabbing between terminals, reading through output, manually checking what finished. VoxHerd replaces that with a voice loop. Your agents report in when they are done ("Aligned Tools completed: added retry logic to the API client"). You respond naturally ("Good, now add tests for the retry paths"). VoxHerd routes your command to the right session and dispatches it. You never leave your conversation flow.

## Platforms

| Platform | Component | Status |
|----------|-----------|--------|
| **All** | Bridge server (Python) | Stable |
| **All** | Hook scripts (bash/PS/Python) | Stable |
| **macOS** | Menu bar app (SwiftUI) | Stable |
| **macOS** | Native TTS + STT | Stable |
| **Linux** | systemd service | Stable |
| **Linux** | GTK4 panel app + Waybar | Beta |
| **Linux** | espeak-ng TTS | Stable |
| **Windows** | System tray app (pystray) | Beta |
| **Windows** | pyttsx3 TTS | Beta |
| **iOS** | Voice remote app | Available separately |

## Quick Start

### 1. Set up the bridge server

```bash
cd bridge
python3 -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

### 2. Install hooks

Hooks are small scripts that run inside your AI assistant's lifecycle (session start, task completion, notifications) and report events to the bridge.

```bash
cd hooks
HOOK_AGENTS=claude bash install.sh              # macOS/Linux (Claude Code only)
HOOK_AGENTS=claude,gemini bash install.sh       # macOS/Linux (multiple assistants)
```

On Windows:
```powershell
powershell -ExecutionPolicy Bypass -File hooks\install-hooks.ps1
```

Verify hooks are installed:
```bash
cat ~/.claude/settings.json | jq '.hooks'
```

### 3. Start the bridge

```bash
source bridge/.venv/bin/activate
python -m bridge run --tts
```

The bridge starts on port 7777 and prints a live, color-coded event log to the terminal. On macOS, use `python -m bridge start --tts` to auto-create a tmux session (required for microphone access).

| Flag | Purpose |
|------|---------|
| `--tts` | Enable text-to-speech (macOS: `say`, Linux: `espeak-ng`, Windows: `pyttsx3`) |
| `--listen` | Enable speech-to-text (macOS only) |
| `--wake-word` | Enable wake word detection (macOS only) |
| `--headless` | No interactive terminal (for systemd/services) |
| `--port N` | Custom port (default: 7777) |

### 4. Platform-specific setup

See [docs/SETUP.md](docs/SETUP.md) for detailed per-platform instructions including the macOS menu bar app, Linux systemd service, and Windows tray app. Or use the one-command installers:

```bash
# macOS (dev setup)
bash scripts/dev-setup.sh

# Linux (full install with systemd service)
bash scripts/install-linux.sh

# Windows
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

## How It Works

1. AI assistant hooks fire on session lifecycle events (start, stop/after-turn, notification)
2. Hook scripts POST events to the bridge server at `localhost:7777`
3. Bridge forwards events to connected clients over WebSocket
4. Connected app (iOS, macOS menu bar, Windows tray, Linux panel) announces results via TTS
5. Voice commands are parsed, routed to the right project, and dispatched to the configured assistant via `--resume`

The bridge is the central hub. Desktop apps manage the bridge process, show session status, and provide QR codes for mobile pairing. AI assistant CLIs keep their own project context -- VoxHerd just routes voice commands to the right session and announces results.

## Testing

```bash
# Run bridge tests
source bridge/.venv/bin/activate
python -m pytest bridge/tests/ -v

# End-to-end test (no mobile app needed)
bash scripts/test-flow.sh

# Manual testing with curl
curl http://localhost:7777/api/sessions
wscat -c ws://localhost:7777/ws/ios
```

## Bridge Server API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/sessions/register` | Register new assistant session |
| GET | `/api/sessions` | List all sessions with status |
| DELETE | `/api/sessions/{session_id}` | Remove a session |
| POST | `/api/events` | Hook events (stop, notification, subagent) |
| POST | `/api/command` | Dispatch command to a project (REST) |
| POST | `/api/intent/parse` | Parse voice transcription via Haiku |
| POST | `/api/tts` | Speak text via platform TTS |
| GET | `/api/projects` | List known projects |
| GET | `/api/connection-info` | Pairing info (URL, QR) |
| WS | `/ws/ios` | Persistent client WebSocket connection |

See [docs/api.md](docs/api.md) for the full reference with request/response schemas.

## Project Layout

```
bridge/              Python bridge server (FastAPI + CLI, cross-platform)
  cli.py             CLI entry point: start, stop, status, qr
  bridge_server.py   FastAPI app: REST + WebSocket + dispatch
  session_manager.py Session state management
  mac_tts.py         macOS TTS (say command)
  linux_tts.py       Linux TTS (espeak-ng)
  win_tts.py         Windows TTS (pyttsx3)
hooks/               Assistant lifecycle hooks
  on-stop.sh         Post-turn summary + bridge notification (bash)
  on-stop.py         Same, Python version (cross-platform)
  on-stop.ps1        Same, PowerShell version (Windows)
  on-session-start.sh  Register session with bridge
  on-notification.sh   Forward permission requests
  install.sh         Deploy hooks + patch settings.json
macos/               macOS menu bar app
  VoxHerdBridge/     SwiftUI app (Xcode project)
  build-app.sh       Build + sign the app bundle
  create-dmg.sh      Package as DMG
windows/             Windows system tray app
  voxherd_tray/      Python tray app (pystray + tkinter)
linux/               Linux desktop integration
  voxherd_panel/     GTK4 panel app
  waybar_module.py   Waybar status module
scripts/             Dev tooling and installers
  dev-setup.sh       One-command macOS dev setup
  install-linux.sh   One-command Linux install
  install-windows.ps1  One-command Windows install
  test-flow.sh       End-to-end test script
docs/                Documentation
  SETUP.md           Detailed platform setup guide
  api.md             Full API reference
  prd.md             Product requirements
  voice-ux.md        Voice interaction patterns
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style guidelines, and how to submit a pull request.

## License

MIT
