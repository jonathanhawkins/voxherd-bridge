# VoxHerd Windows System Tray App

Windows system tray application for managing the VoxHerd bridge server. Equivalent to the macOS menu bar app.

## Prerequisites

- Python 3.11+
- Claude Code CLI (`claude`) on PATH
- VoxHerd repo cloned locally

## Install

```powershell
cd windows
pip install -r requirements-windows.txt
```

## Run

```powershell
# From the repo root:
python -m windows.voxherd_tray

# Or from the windows/ directory:
cd windows
python -m voxherd_tray
```

The app creates a system tray icon (green circle when running, red when stopped). Right-click for the context menu.

## Features

- **Start/Stop Bridge**: Manages the bridge server as a subprocess
- **QR Code**: Generates a QR code for iOS app pairing (includes LAN IP, port, auth token, Tailscale IP)
- **Log Viewer**: Color-coded scrollable log of bridge events
- **Settings**: Port, TTS toggle, auto-start, launch at login, hook installation, auth token management
- **Auto-restart**: Exponential backoff restart on unexpected bridge exit
- **Health check**: Polls /api/sessions every 2s, restarts after 5 consecutive failures

## Configuration

Settings are stored in `%APPDATA%\VoxHerd\config.json`. Auth token at `%APPDATA%\VoxHerd\auth_token`.

## Build with PyInstaller

```powershell
pip install pyinstaller
pyinstaller --name VoxHerd --onefile --windowed --icon=icon.ico windows/voxherd_tray/__main__.py
```

## Environment Variables

- `VOXHERD_PROJECT_DIR`: Override the repo root path for bridge discovery
- `VOXHERD_AUTH_TOKEN`: Override the auth token (instead of reading from file)
