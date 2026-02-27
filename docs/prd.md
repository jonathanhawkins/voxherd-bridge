# VoxHerd PRD

**Voice-First Multi-Instance Claude Code Controller**

Control multiple Claude Code instances from Meta Ray-Ban glasses via voice. Get spoken status updates when agents complete work. Issue follow-up commands while walking, driving, or doing anything else.

---

## Problem

Running multiple Claude Code instances across projects requires constant terminal context-switching, visual monitoring, and keyboard input. This locks the developer to their desk. There is no way to:

- Get notified when an agent finishes a task without watching the terminal
- Issue follow-up commands to a specific project without switching windows
- Monitor cross-project status at a glance
- Approve permission requests without being at the keyboard

## Target User

Solo developer running 2-6 concurrent Claude Code sessions across different projects, using Meta Ray-Ban glasses as a hands-free interface. Primary use: mobile development supervision while away from the desk.

## Core Insight

Claude Code already has hooks (Stop, Notification, SessionStart) and session resume (`claude --resume`). A thin bridge server can expose these as a real-time event stream, and a mobile app can provide voice I/O on top. No need to replicate MCP tools or codebase context on the phone -- just route voice commands to the right existing session.

---

## System Architecture

```
Meta Ray-Ban Glasses (mic/speakers)
        |
        | Bluetooth audio
        v
iOS App "VoxHerd" (STT, TTS, intent routing)
        |
        | WebSocket (LAN / Tailscale)
        v
Mac Bridge Server (FastAPI, port 7777)
        |
        | Hook events in, `claude --resume` out
        v
Claude Code instances (#1..#N, each with their own MCP tools)
```

Three layers, each with a single responsibility:

1. **Claude Code hooks** push lifecycle events (stop, notification, session start) to the bridge server via HTTP POST
2. **Bridge server** maintains session registry, forwards events to iOS over WebSocket, dispatches voice commands back to Claude Code via `claude --resume SESSION_ID -p "message"`
3. **iOS app** handles voice I/O (STT/TTS), intent parsing, project routing, and displays a dashboard of all active sessions

---

## Functional Requirements

### FR-1: Hook-to-Bridge Pipeline (Mac-side)

**FR-1.1** Global Claude Code hook scripts installed at `~/.voxherd/hooks/` that fire on Stop, Notification, and SessionStart events.

**FR-1.2** `on-stop.sh` reads hook JSON from stdin, extracts session_id/cwd/transcript_path, generates a 1-2 sentence summary via Haiku, and POSTs the event to the bridge server.

**FR-1.3** `on-session-start.sh` registers the new session (session_id, project name, project directory) with the bridge server.

**FR-1.4** `on-notification.sh` forwards permission requests and notifications to the bridge server.

**FR-1.5** An `install.sh` script copies hooks to `~/.voxherd/hooks/`, makes them executable, and updates `~/.claude/settings.json` with the hook configuration.

### FR-2: Bridge Server (Mac-side, CLI + FastAPI)

The bridge server is a CLI tool launched in a terminal window. It runs FastAPI in the foreground and prints a live event stream to stdout so the developer can see what's happening. No web UI, no browser dashboard.

**FR-2.1** CLI entry point: `voxherd start` (or `python -m bridge` during development). Starts the FastAPI server on port 7777 and prints a formatted live log to stdout showing session registrations, hook events, iOS connections, and command dispatches.

**FR-2.2** Live terminal output using `rich` (Python). On startup, prints a banner with the listen address and registered sessions. Each event prints a timestamped, color-coded line:
- Green: session registered, task completed
- Yellow: command dispatched, iOS connected
- Red: errors, stale sessions
- Cyan: permission requests / waiting

**FR-2.3** `POST /api/events` -- Ingests hook events from Claude Code, updates session state, forwards to all connected iOS clients via WebSocket.

**FR-2.4** `POST /api/sessions/register` -- Registers new Claude Code sessions, notifies iOS clients.

**FR-2.5** `GET /api/sessions` -- Returns all active sessions with status (active/idle/waiting), last summary, and project metadata.

**FR-2.6** `GET /api/sessions/{project}/summary` -- Returns the latest summary for a named project.

**FR-2.7** `WebSocket /ws/ios` -- Persistent connection for iOS clients. On connect, sends full state sync. Receives voice commands and status requests. Pushes agent events in real-time.

**FR-2.8** Command dispatch: receives a voice command with target project, resolves to a session_id, spawns `claude --resume SESSION_ID -p "message" --output-format stream-json` in the correct working directory.

**FR-2.9** Session status tracking: updates sessions to "active" when commands are dispatched, "idle" on stop events, "waiting" on permission requests.

**FR-2.10** `Ctrl-C` gracefully shuts down: closes iOS WebSocket connections, logs final session state, exits clean.

### FR-3: iOS App -- Voice Interface

**FR-3.1** STT via iOS `SFSpeechRecognizer` (on-device, free). Supports tap-to-talk and optional wake word ("Hey Claude").

**FR-3.2** TTS via `AVSpeechSynthesizer` (built-in) for MVP. Future: ElevenLabs for cloned voice.

**FR-3.3** VAD (Voice Activity Detection) to handle natural pauses in speech.

**FR-3.4** Audio session management: `.voiceChat` category when glasses are connected (Bluetooth), `.default` when using phone speakers.

### FR-4: iOS App -- Intent Parsing & Routing

**FR-4.1** Fast-path keyword matching for common intents: "status", "approve {project}", "deny {project}", "switch to {project}".

**FR-4.2** Haiku-powered NLU fallback for ambiguous commands. Sends transcription + known project list to Haiku, receives structured JSON intent.

**FR-4.3** Project fuzzy matching: "aligned" resolves to "aligned-tools", "quant" resolves to "quantcoach".

**FR-4.4** Active project context: after "switch to {project}", subsequent commands without an explicit project target route to the active project.

**FR-4.5** Disambiguation: when no active project is set and the target is ambiguous, ask the user "Which project?" via TTS.

### FR-5: iOS App -- Dashboard UI

**FR-5.1** Dashboard view showing session cards for all registered Claude Code instances.

**FR-5.2** Each session card displays: project name, status (active/idle/waiting), last summary, time since last activity.

**FR-5.3** Transcript view: scrollable log of voice interactions (commands sent, responses received).

**FR-5.4** Settings view: bridge server URL, TTS voice selection, wake word toggle, project aliases.

### FR-6: iOS App -- Glasses Integration

**FR-6.1** Meta DAT SDK integration via SPM for glasses connectivity.

**FR-6.2** Route glasses 5-mic array input through the STT pipeline.

**FR-6.3** Route TTS output to glasses open-ear speakers.

**FR-6.4** Earcon audio cues: distinct chimes for task completion, permission requests, errors, and command acknowledgment.

---

## Voice Interaction Patterns

### Passive Monitoring (glasses announce automatically)

| Trigger | Announcement |
|---|---|
| Agent completes task | *chime* "Aligned Tools finished. Added pagination and updated the API endpoint." |
| Permission request | *different chime* "QuantCoach is waiting for permission to run database migrations." |
| Error/failure | *alert chime* "Homeschool encountered an error running tests." |

### Active Commands (user speaks)

| Command | Response |
|---|---|
| "Status" / "What's running" | Lists all projects with their current state |
| "Tell {project} to {task}" | Routes task to named project |
| "{task}" (with active project set) | Routes to active/default project |
| "Switch to {project}" | Sets default project context |
| "Approve {project}" | Sends approval for pending permission |
| "Deny {project}" | Blocks pending permission |
| "What did {project} do last?" | Reads the last stop summary |
| "Pause {project}" | Sends interrupt signal |
| "Resume {project}" | Continues paused session |

### Disambiguation

When no active project is set and the command is ambiguous:
- TTS: "Which project? Aligned Tools, QuantCoach, or Homeschool?"
- User responds with project name
- Command is routed

### Conversational Multi-Agent Flow (Primary Use Case)

The core experience is an ongoing conversation with multiple agents while mobile. Announcements arrive, the user reacts with follow-ups, pivots to another project, gives guidance, and circles back. This is not single-shot commands -- it's iterative supervision across sessions.

```
  -- Walking to get coffee, three agents running --

  *chime*
  "Aligned Tools finished. Refactored the auth middleware
   and updated 12 test files."

  "Okay, now I want it to also add rate limiting
   to the auth middleware."
  "Sending to Aligned Tools."

  -- 2 minutes later, still walking --

  *chime*
  "QuantCoach hit a test failure. Three tests in the
   backtesting module are failing with a date parsing error."

  "Tell it to try fixing the date parsing by using
   ISO format instead of the custom parser."
  "Sending to QuantCoach."

  -- Pivot to a different project --

  "Switch to Homeschool."
  "Homeschool is now your active project."

  "Run a security audit on the student data endpoints."
  "Sending to Homeschool."

  -- Aligned Tools finishes while Homeschool is active --

  *chime*
  "Aligned Tools finished. Added rate limiting with
   a 100 requests per minute default."

  "Tell Aligned Tools to make that configurable
   per endpoint, not a global default."
  "Sending to Aligned Tools."

  -- QuantCoach finishes --

  *chime*
  "QuantCoach finished. Fixed date parsing in the
   backtesting module. All tests passing."

  "Nice. Tell QuantCoach to add edge case tests
   for leap years and timezone boundaries."
  "Sending to QuantCoach."

  -- Back to checking on Homeschool --

  "What's Homeschool doing?"
  "Homeschool is still running. Started 3 minutes ago."

  -- Homeschool finishes later --

  *chime*
  "Homeschool finished. Found two SQL injection risks
   in the student search endpoint. Applied parameterized
   queries and added input validation."

  "Switch to Homeschool. Now add integration tests
   for those endpoints."
  "Homeschool is now active. Sending."
```

Key behaviors demonstrated:
- **React to announcements:** Agent finishes, user immediately gives the next task without switching context
- **Iterative guidance:** "Try fixing it like this" -- directing an agent's approach, not just what to do
- **Cross-project pivoting:** Switch active project, issue commands, hear results from other projects in between
- **Implicit routing:** After "switch to Homeschool", bare commands go to Homeschool without naming it
- **Explicit routing overrides implicit:** "Tell Aligned Tools to..." works even when Homeschool is active
- **Status checks mid-flow:** "What's Homeschool doing?" while other agents are reporting in
- **Compound commands:** "Switch to Homeschool. Now add integration tests" -- switch + command in one utterance

---

## Connectivity

| Mode | Transport | Use Case |
|---|---|---|
| Local (same Wi-Fi) | `http://macbook.local:7777` via Bonjour/mDNS | Lowest latency, home/office |
| Remote (Tailscale) | `http://100.x.x.x:7777` over Tailnet | Recommended for mobile, zero-config, encrypted |
| Remote (Cloudflare Tunnel) | `https://voxherd.yourdomain.com` | Alternative, requires `cloudflared` on Mac |

---

## Non-Requirements

- **No web UI.** No shadcn, no Tailwind, no dashboard in the browser, no login page. The iOS app is the UI. The bridge is headless plumbing.
- **No auth (Clerk or otherwise).** Single-user system on a private LAN/Tailnet. No login, no sessions, no tokens.
- **No MCP replication on iOS.** Claude Code instances already have their MCP servers (Notion, GitHub, etc.). VoxHerd only routes voice commands; the instances handle tool execution.
- **No codebase indexing on iOS.** The phone never needs to understand or store code. It is a voice remote control.
- **No Gemini integration.** Unlike VisionClaw, this uses Claude end-to-end. Haiku for fast intent parsing, existing Claude Code sessions for actual work.

---

## Repo Structure

```
voxherd/
  bridge/
    __main__.py               # `python -m bridge` entry point
    cli.py                    # CLI: `voxherd start`, arg parsing, rich live log
    bridge_server.py          # FastAPI bridge server
    session_manager.py        # Session state management
    requirements.txt
    Dockerfile                # Optional containerized deployment
  hooks/
    on-stop.sh                # Stop event: Haiku summary + POST to bridge
    on-session-start.sh       # Session registration
    on-notification.sh        # Permission request forwarding
    install.sh                # Installs hooks + updates Claude settings
  ios/
    VoxHerd/
      VoxHerd.xcodeproj
      VoxHerd/
        App/                  # Entry point, global state
        Voice/                # STT, TTS, VAD, wake word, audio session
        Bridge/               # WebSocket client, session registry, event handler
        Router/               # Intent parser, project matcher, command builder
        Glasses/              # DAT SDK, Bluetooth audio bridge
        UI/                   # Dashboard, session cards, transcript, settings
        Config/               # Bridge host, API keys, project aliases
  docs/
    prd.md                    # This document
    voice-ux.md               # Detailed voice interaction patterns
    api.md                    # Bridge server API reference
  scripts/
    dev-setup.sh              # One-command local dev setup
    test-flow.sh              # End-to-end test with mock events
```

---

## Implementation Phases

### Phase 1: Bridge Server + Hooks

Ship a working Mac-side pipeline that captures Claude Code lifecycle events and makes them queryable. The bridge runs as a CLI tool in a terminal window with live event output.

- [ ] `on-stop.sh` hook: read stdin JSON, generate Haiku summary, POST to bridge
- [ ] `on-session-start.sh` hook: register session with bridge
- [ ] `on-notification.sh` hook: forward notifications
- [ ] `install.sh`: deploy hooks + update `~/.claude/settings.json`
- [ ] FastAPI bridge server with CLI entry point (`voxherd start`)
- [ ] Live terminal output with `rich`: color-coded event stream, startup banner
- [ ] REST API: `/api/events`, `/api/sessions`, `/ws/ios`
- [ ] `claude --resume` command dispatch from bridge
- [ ] Graceful shutdown on `Ctrl-C`
- [ ] Validate end-to-end with `curl` and `wscat`

**Exit criteria:** Run `voxherd start` in a terminal, see a live event stream as Claude Code sessions start and stop, Haiku summaries print in green, and `claude --resume` successfully sends a follow-up command.

### Phase 2: iOS App MVP

Minimal voice-to-bridge interface running on iPhone.

- [ ] Fork VisionClaw project structure, strip Gemini integration
- [ ] `BridgeConnection.swift`: WebSocket client to Mac bridge
- [ ] `SessionRegistry.swift`: local mirror of active sessions
- [ ] STT via `SFSpeechRecognizer` (on-device)
- [ ] TTS via `AVSpeechSynthesizer`
- [ ] Keyword-based intent parsing (no Haiku yet)
- [ ] Dashboard UI: session cards with status
- [ ] Tap-to-talk button for voice commands
- [ ] Event handler: receive agent events, trigger TTS announcements

**Exit criteria:** Tap button on phone, speak "tell aligned tools to run the tests", hear confirmation, see Claude Code resume and execute.

### Phase 3: Glasses Integration

Route audio through Meta Ray-Ban glasses.

- [ ] Meta DAT SDK via SPM
- [ ] Glasses mic input routed to STT pipeline
- [ ] TTS output routed to glasses speakers
- [ ] Bluetooth audio session management (`.voiceChat`)
- [ ] Full loop test: glasses voice command -> iOS -> bridge -> Claude Code -> summary -> glasses TTS

**Exit criteria:** Issue a voice command from glasses while walking, receive spoken summary when agent completes, without touching the phone.

### Phase 4: Smart Routing + Polish

Upgrade intent parsing and add quality-of-life features.

- [ ] Haiku-powered intent parser for ambiguous commands
- [ ] Project fuzzy matching
- [ ] Earcon audio cues (distinct chimes per event type)
- [ ] Permission request forwarding + voice approval flow
- [ ] Active project context for implicit routing
- [ ] Notification preferences (filter which events get announced)

**Exit criteria:** Say "run the tests" without specifying a project, get asked "which project?", respond, and it routes correctly.

### Phase 5: Advanced Features (Ongoing)

- [ ] ElevenLabs TTS with cloned voice
- [ ] Camera context from glasses for visual debugging
- [ ] iOS Widget / Live Activity showing agent status on lock screen
- [ ] Apple Watch complication
- [ ] Siri Shortcuts integration ("Hey Siri, Claude status")

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| CLI tool in a terminal, not a web UI or daemon | You need to see events flowing during development. A live terminal log is the fastest feedback loop. No shadcn/tailwind/clerk -- the iOS app is the UI. Daemonize later if needed. |
| Bridge server instead of direct iOS-to-Claude Code | Claude Code is a CLI process with no direct API. The bridge translates between WebSocket (iOS) and CLI lifecycle (hooks + `--resume`). |
| On-device STT + text routing instead of streaming audio to Claude | Latency and cost. Apple's `SFSpeechRecognizer` is free and fast. Only hit Haiku for ambiguous intent parsing. |
| No MCP on iOS | Claude Code instances already have their tools. VoxHerd is a voice remote, not a replacement. |
| Hooks (push) over MCP (pull) | Hooks fire when events happen. MCP would require Claude Code to poll, which it won't do unprompted. |
| Haiku for intent parsing, not Sonnet/Opus | Intent extraction is a lightweight classification task. Haiku is fast and cheap enough for real-time voice UX. |
| Tailscale for remote connectivity | Zero-config, encrypted, works through NAT. No port forwarding or DNS setup. |
