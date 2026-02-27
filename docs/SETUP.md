# Platform Setup Guide

Detailed setup instructions for each supported platform. For a quick overview, see the main [README.md](../README.md).

## Prerequisites (All Platforms)

- **Python 3.11+** (`python3 --version` to check)
- At least one AI coding assistant CLI:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
  - [Codex](https://developers.openai.com/codex)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli)
- **jq** (hook scripts parse JSON with it)
- **curl** (hooks POST to the bridge)

## macOS

### One-Command Setup

```bash
bash scripts/dev-setup.sh
```

This checks prerequisites, creates a Python venv, installs dependencies, deploys hooks, starts the bridge, and verifies it responds on port 7777.

### Manual Setup

All commands below run from the repository root.

```bash
# 1. Create venv and install deps
cd bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..

# 2. Install hooks
cd hooks
HOOK_AGENTS=claude bash install.sh
cd ..

# 3. Start the bridge (must be in tmux for microphone access)
tmux new-session -s bridge
source bridge/.venv/bin/activate
python -m bridge run --tts
```

> **Why tmux?** macOS TCC (Transparency, Consent, and Control) requires a real TTY for
> microphone permission. Running the bridge inside tmux satisfies this requirement.
> If you do not need `--listen` (speech-to-text), you can skip tmux and run in a regular terminal.

### macOS Menu Bar App

The menu bar app manages the bridge process, shows session status, and provides a QR code for mobile pairing.

**Debug build (no signing required -- recommended for development):**
```bash
bash macos/build-app.sh --debug
open macos/dist/VoxHerdBridge.app
```

**Release build (requires an Apple Developer ID certificate):**

> You must replace the placeholder signing values first.
> See the [Replacing Placeholder Values](../CONTRIBUTING.md#replacing-placeholder-values) section in CONTRIBUTING.md.

```bash
export VOXHERD_SIGN_IDENTITY="Developer ID Application: Your Name (YOUR_TEAM_ID)"
bash macos/build-app.sh
```

**Full release pipeline (build + DMG + notarize):**
```bash
# First, store notarization credentials in your keychain:
xcrun notarytool store-credentials "VoxHerd" \
    --apple-id YOUR_APPLE_ID --team-id YOUR_TEAM_ID

# Then run the pipeline:
bash scripts/release-macos.sh
```

### macOS-Specific Features

- **TTS**: Uses the native `say` command with a configurable voice
- **STT**: Uses `SFSpeechRecognizer` via a compiled Swift binary (`bridge/stt/voxherd-listen`)
- **Wake word**: Optional keyword detection before STT activates
- **Bonjour**: Auto-advertises the bridge for automatic mobile app discovery on the local network

### Building the STT Binary (Optional)

The speech-to-text binary is a standalone Swift CLI used for `--listen` mode. You only need this if you want voice input on macOS.

```bash
bash scripts/build-stt.sh
# Output: bridge/stt/voxherd-listen
```

Requires Xcode command line tools (`xcode-select --install`).

## Linux

### One-Command Install

```bash
bash scripts/install-linux.sh
```

This handles everything: distro detection, package installation, venv setup, hook deployment, auth token generation, systemd service installation, and startup.

Supported distros: Ubuntu/Debian (apt), Fedora (dnf), Arch (pacman).

### Manual Setup

All commands below run from the repository root.

```bash
# 1. Install system dependencies
sudo apt install python3-venv jq curl tmux espeak-ng  # Debian/Ubuntu
# sudo dnf install python3 jq curl tmux espeak-ng     # Fedora
# sudo pacman -S python jq curl tmux espeak-ng         # Arch

# 2. Create venv and install deps
cd bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..

# 3. Install hooks
cd hooks
HOOK_AGENTS=claude bash install.sh
cd ..

# 4. Start the bridge
source bridge/.venv/bin/activate
python -m bridge run --tts --headless
```

### systemd Service

The one-command installer creates a systemd user service. Manage it with:

```bash
systemctl --user status voxherd-bridge
systemctl --user restart voxherd-bridge
systemctl --user stop voxherd-bridge
journalctl --user -u voxherd-bridge -f    # View logs
```

### Linux GTK4 Panel App

Optional desktop panel with session cards and QR code display:

```bash
bash linux/install-gui.sh
```

Requires: GTK4, PyGObject, libadwaita.

### Linux Waybar Module

For Waybar users, a custom module shows agent status in the bar:

```bash
# Copy config to ~/.config/waybar/
cp linux/waybar_config.jsonc ~/.config/waybar/config
cp linux/waybar_style.css ~/.config/waybar/style.css
```

### Deploying from Another Machine

Build a self-contained tarball and deploy to a Linux machine:

```bash
# On the build machine:
bash scripts/build-linux-package.sh
scp voxherd-bridge.tar.gz user@host:~/

# On the Linux target:
tar xzf voxherd-bridge.tar.gz
cd voxherd-bridge
bash install.sh
```

### Linux-Specific Notes

- **TTS**: Uses `espeak-ng` subprocess
- **No native STT**: Speech-to-text is currently macOS-only. On Linux, use the iOS app or a browser-based client for voice input.
- **Headless mode**: Use `--headless` for systemd (no interactive terminal)

## Windows

### One-Command Setup

Open PowerShell and run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

This creates a venv, installs bridge and tray app dependencies, deploys hooks, and writes config.

### Manual Setup

All commands below run from the repository root in PowerShell.

> **Note:** On Windows, use `python` (not `python3`) unless you installed Python from python.org
> with the "py launcher." If `python` does not work, try `py -3`.

```powershell
# 1. Create venv and install deps
cd bridge
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cd ..

# 2. Install tray app deps
pip install -r windows\requirements-windows.txt

# 3. Install hooks
powershell -ExecutionPolicy Bypass -File hooks\install-hooks.ps1

# 4. Start the bridge
.venv\Scripts\activate
python -m bridge run --tts
```

### Windows Tray App

The system tray app manages the bridge, shows status, and provides network/QR info:

```powershell
# From the repository root, with the venv activated:
python -m windows.voxherd_tray
```

### Building a Windows Package

Create a standalone `.exe` with PyInstaller:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows-package.ps1
# Output: dist\VoxHerdBridge\VoxHerdBridge.exe

# With Inno Setup installer:
powershell -ExecutionPolicy Bypass -File scripts\build-windows-package.ps1 -Installer
```

### Windows-Specific Notes

- **TTS**: Uses `pyttsx3` (SAPI5 on Windows)
- **No native STT**: Speech-to-text is currently macOS-only. On Windows, use the iOS app or a browser-based client for voice input.
- **Hooks**: PowerShell `.ps1` versions of all hook scripts

## Mobile App Pairing

The iOS app (available separately) connects to the bridge over WebSocket. To pair:

1. Start the bridge on your computer
2. Show the QR code: `python -m bridge qr` (or use the desktop app's QR button)
3. Scan the QR code in the iOS app's Settings

Or enter the connection info manually:
- **Host**: your computer's local IP address
- **Port**: 7777
- **Token**: shown in the bridge terminal on startup, or in `~/.voxherd/auth_token`

The bridge auto-advertises via Bonjour (macOS) for automatic discovery on the local network.

## Verifying Your Setup

After setting up on any platform, confirm everything is working:

```bash
# 1. Check the bridge is running
curl http://localhost:7777/api/sessions
# Expected: {"sessions": {}} (empty is fine if no assistant sessions are active)

# 2. Simulate a hook event
curl -X POST http://localhost:7777/api/events \
  -H "Content-Type: application/json" \
  -d '{"event":"stop","session_id":"test","project":"myproject","summary":"Test event"}'
# Expected: {"ok": true}

# 3. Run the automated end-to-end test
bash scripts/test-flow.sh
```

## Troubleshooting

### Bridge will not start

- Check if port 7777 is already in use: `lsof -i :7777` (macOS/Linux) or `netstat -an | findstr 7777` (Windows)
- Make sure the venv is activated before running (`which python` should point inside `.venv/`)
- Check logs at `~/.voxherd/logs/`

### Hooks not firing

- Verify hooks are installed: `cat ~/.claude/settings.json | jq '.hooks'`
- Check that hook scripts are executable: `ls -la ~/.voxherd/hooks/`
- Look for errors in `~/.voxherd/logs/hook-errors.log`
- Make sure the bridge is running -- hooks POST to `localhost:7777` and fail silently if it is down

### TTS not working

- **macOS**: Test with `say "hello"` in terminal
- **Linux**: Test with `espeak-ng "hello"`. Install if missing: `sudo apt install espeak-ng`
- **Windows**: Test with `python -c "import pyttsx3; e=pyttsx3.init(); e.say('hello'); e.runAndWait()"`

### Auth token mismatch

If hooks return 401 errors, the auth token may be out of sync:

```bash
# Check the token the bridge is using
cat ~/.voxherd/auth_token

# Hooks read from this file automatically on each invocation.
# If you regenerated the token, restart the bridge so it picks up the new one.
```
