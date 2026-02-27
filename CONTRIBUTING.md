# Contributing to VoxHerd

Thanks for considering a contribution to VoxHerd. Whether you are fixing a bug, adding platform support, improving docs, or just cleaning up code style, we appreciate the help.

This guide covers how to set up the project, run tests, and submit changes. If something in here is unclear or wrong, that is itself a good first contribution -- open an issue or PR.

## Repository Structure

```
bridge/          Python bridge server (FastAPI, cross-platform)
hooks/           Assistant lifecycle hook scripts (bash, PowerShell, Python)
macos/           macOS menu bar app (SwiftUI) + build/packaging scripts
windows/         Windows system tray app (Python + pystray)
linux/           Linux GTK4 panel app + Waybar module
scripts/         Dev tooling, installers, packaging
docs/            API reference, PRD, voice UX docs
```

## Quick Setup

### All Platforms (Bridge Server)

```bash
cd bridge
python3 -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows
pip install -r requirements.txt
python -m bridge run
```

The bridge starts on port 7777. See [docs/SETUP.md](docs/SETUP.md) for detailed per-platform instructions.

### Install Hooks

```bash
cd hooks
HOOK_AGENTS=claude bash install.sh     # Linux/macOS
# Or for multiple assistants:
HOOK_AGENTS=claude,gemini bash install.sh
```

On Windows, use the PowerShell installer:
```powershell
powershell -ExecutionPolicy Bypass -File hooks\install-hooks.ps1
```

## Replacing Placeholder Values

> **IMPORTANT:** Several files contain placeholder values like `YOUR_TEAM_ID` and `YOUR_APPLE_ID`.
> You **must** replace these with your own credentials before building platform-specific apps.
> They are intentionally generic to keep the repo free of personal data.

### macOS Code Signing

The following files reference placeholder signing identities:

| File | Placeholder | Replace With |
|------|-------------|--------------|
| `macos/VoxHerdBridge/project.yml` | `${DEVELOPMENT_TEAM}` | Your Apple Developer Team ID (e.g. `A1B2C3D4E5`) |
| `macos/VoxHerdBridge/VoxHerdBridge.xcodeproj/project.pbxproj` | `YOUR_TEAM_ID` | Your Apple Developer Team ID |
| `macos/build-app.sh` | `VOXHERD_SIGN_IDENTITY` env var | Your "Developer ID Application" identity string |
| `macos/notarize.sh` | `YOUR_APPLE_ID`, `YOUR_TEAM_ID` | Your Apple ID email and Team ID |
| `scripts/release-macos.sh` | `YOUR_APPLE_ID`, `YOUR_TEAM_ID` | Your Apple ID email and Team ID |

To set your signing identity for macOS release builds:

```bash
export VOXHERD_SIGN_IDENTITY="Developer ID Application: Your Name (A1B2C3D4E5)"
bash macos/build-app.sh
```

For debug builds that skip code signing entirely (recommended for development):
```bash
bash macos/build-app.sh --debug
```

### XcodeGen

If you use XcodeGen to regenerate the Xcode project:
```bash
export DEVELOPMENT_TEAM="A1B2C3D4E5"   # <-- your Team ID
cd macos/VoxHerdBridge && xcodegen generate
```

Or edit `macos/VoxHerdBridge/project.yml` directly and replace `${DEVELOPMENT_TEAM}` with your Team ID.

### Example Paths in Docs

The `docs/api.md` file uses `/home/user/projects/...` as example paths. These are documentation examples only and do not need to be changed.

## Development Workflow

### Running the Bridge

On macOS, the bridge needs to run in `tmux` for TCC microphone access. On Linux and Windows, a regular terminal works fine.

```bash
# macOS (in tmux for mic access)
tmux new-session -s bridge
source bridge/.venv/bin/activate
python -m bridge run --tts

# Linux
source bridge/.venv/bin/activate
python -m bridge run --tts

# Windows (PowerShell or cmd)
bridge\.venv\Scripts\activate
python -m bridge run --tts
```

### Running Tests

From the repository root:

```bash
source bridge/.venv/bin/activate
python -m pytest bridge/tests/ -v
```

Tests mock all subprocess calls, so you do not need AI assistant CLIs installed to run them.

### Validating Hook Scripts

Before committing hook changes, check syntax:

```bash
# Bash hooks
bash -n hooks/on-stop.sh
bash -n hooks/on-session-start.sh
bash -n hooks/on-notification.sh

# Python hooks
python3 -c "import ast; ast.parse(open('hooks/on-stop.py').read())"

# PowerShell hooks (Windows)
pwsh -Command "Get-Content hooks\on-stop.ps1 | Out-Null"
```

### Building Platform Apps

**macOS menu bar app:**
```bash
# Debug (no code signing required)
bash macos/build-app.sh --debug

# Release (requires Developer ID certificate -- see placeholder section above)
export VOXHERD_SIGN_IDENTITY="Developer ID Application: Your Name (TEAM_ID)"
bash macos/build-app.sh
```

**Windows package:**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows-package.ps1
```

**Linux tarball:**
```bash
bash scripts/build-linux-package.sh
# Output: voxherd-bridge.tar.gz
```

### Testing Without Every Platform

You do not need macOS, Linux, and Windows machines to contribute. Focus on the platform you have:

- **Bridge server changes**: Test on any platform. The core server is cross-platform Python.
- **Hook scripts**: Bash hooks can be tested on macOS or Linux. PowerShell hooks require Windows. Python hooks (`on-stop.py`) work everywhere.
- **Platform apps**: Only testable on the target platform. If you are adding a feature to the macOS menu bar app, you need a Mac. That is expected.
- **Docs and tests**: Testable anywhere.

## Code Style

### Python (bridge/)
- Async functions for all I/O operations
- Type hints on every function signature
- Use `dict` and `list` builtins, not `typing.Dict`/`typing.List`
- Dataclasses or plain dicts for data structures
- No database; sessions are in-memory

### Bash (hooks/)
- No `set -e` in hook scripts (a failed `curl` must not block the AI assistant)
- Quote all variables: `"$VAR"`, not `$VAR`
- Timeouts on all network calls: `curl -s --max-time 5`
- Log errors to `~/.voxherd/logs/`

### Swift (macos/)
- SwiftUI, macOS 14+ minimum
- `@Observable` macro for state (not `ObservableObject`)
- Structured concurrency (`async/await`, `Task`)

## Submitting a Pull Request

1. Fork the repository and create a feature branch from `main`
2. Make your changes
3. Run the relevant tests (see above)
4. Submit a pull request with a clear description of what you changed and why

### Branch Naming

Use descriptive branch names: `fix/hook-timeout-handling`, `feature/waybar-module`, `docs/setup-windows-section`.

### PR Checklist

Before submitting, confirm:

- [ ] `python -m pytest bridge/tests/ -v` passes
- [ ] `bash -n` passes on all modified shell scripts
- [ ] `python3 -c "import ast; ast.parse(...)"` passes on all modified Python files
- [ ] No hardcoded paths, team IDs, or personal email addresses in your changes
- [ ] Changes work on the target platform(s)

## Reporting Issues

Please include:
- Your platform (macOS version, Linux distro, or Windows version)
- Python version (`python3 --version`)
- Bridge server version or commit hash (`git rev-parse --short HEAD`)
- Relevant log output from `~/.voxherd/logs/` or the bridge terminal
- Steps to reproduce

## License

This project is licensed under the MIT License. By contributing, you agree that your contributions will be licensed under the same terms.
