# Repository Guidelines

## Project Structure & Module Organization
- `bridge/`: FastAPI bridge server, CLI entrypoint, and Python service modules (`bridge/*.py`).
- `bridge/tests/`: Pytest suite for API flows and HMAC/auth behavior.
- `ios/VoxHerd/VoxHerd/`: iOS SwiftUI app (voice UI, bridge connectivity, onboarding).
- `ios/VoxHerd/VoxHerdTests/`: XCTest coverage for routing and crypto-related logic.
- `macos/VoxHerdBridge/` and `linux/voxherd_panel/`: desktop bridge companions.
- `hooks/`: Claude Code lifecycle hooks installed into `~/.voxherd/hooks/`.
- `scripts/`: setup, packaging, release, and end-to-end test helpers.

## Build, Test, and Development Commands
- `bash scripts/dev-setup.sh`: create Python venv, install bridge deps, deploy hooks, and start local bridge.
- `python3 -m bridge start --tts`: run bridge in tmux with spoken status updates.
- `cd bridge && pytest`: run Python unit/integration tests.
- `bash scripts/test-flow.sh`: end-to-end API smoke test against a running bridge.
- `xcodebuild -scheme VoxHerd -sdk iphoneos -configuration Debug -derivedDataPath /tmp/vh-build`: build iOS app from CLI.
- `bash scripts/build-linux-package.sh`: produce `voxherd-bridge.tar.gz` for Linux deployment.

## Coding Style & Naming Conventions
- Python: 4-space indentation, explicit type hints where practical, `snake_case` for functions/variables, `PascalCase` for classes.
- Follow Ruff settings in `bridge/pyproject.toml` (`line-length = 100`, `target-version = py311`).
- Swift: `UpperCamelCase` types, `lowerCamelCase` members, feature grouping by folder (`Bridge/`, `Voice/`, `UI/`).
- Keep functions focused; add comments only where control flow or side effects are not obvious.

## Testing Guidelines
- Python tests use `pytest` with `pytest-asyncio`; place tests in `bridge/tests/test_*.py`.
- iOS tests use XCTest in `ios/VoxHerd/VoxHerdTests/*Tests.swift`.
- Before opening a PR, run at least `cd bridge && pytest`; for iOS changes, also run a relevant `xcodebuild test` target locally.

## Commit & Pull Request Guidelines
- Commit style in history is imperative, concise, and capitalized (example: `Add narration engine, fix phantom vibrations, and improve voice UX`).
- Keep commits scoped to one logical change; include migration/config updates in the same commit when required.
- PRs should include: summary, impacted areas (`bridge`, `ios`, `hooks`, etc.), test evidence, and screenshots/video for UI-visible changes.

## Security & Configuration Tips
- Never commit secrets; use templates such as `ios/VoxHerd/Secrets.xcconfig.template`.
- Treat `~/.voxherd/auth_token` and bridge auth headers as sensitive.
- Review hook scripts before release since they send local session metadata to the bridge API.
