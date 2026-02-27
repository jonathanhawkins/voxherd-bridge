# VoxHerd Bridge Server API Reference

## Overview

The bridge server is a FastAPI application that acts as the central nervous system between assistant CLIs (via hooks/tmux), the iOS app (via WebSocket), and Siri Shortcuts (via REST). It runs on port 7777 by default.

**Base URL:** `http://localhost:7777`
**WebSocket:** `ws://localhost:7777/ws/ios`

The server is CLI-first -- it runs in a terminal window (tmux required for TCC access) with a live `rich` event log. No web UI, no browser dashboard.

## Authentication

Auth is controlled by the `VOXHERD_AUTH_TOKEN` env var or `~/.voxherd/auth_token` file. If neither exists, the bridge auto-generates a token on first startup and writes it to the file with mode `0600`.

When a token is set, **all** REST and WebSocket endpoints require authentication.

**REST endpoints** -- Bearer token in the `Authorization` header:

```
Authorization: Bearer <token>
```

**WebSocket** -- two methods (WebSocket clients cannot easily set headers):

1. Query parameter: `ws://localhost:7777/ws/ios?token=<token>`
2. `Authorization: Bearer <token>` header (if the client supports it)

When no token is configured (env var unset, file missing), auth is disabled entirely.

Unauthenticated requests return:

```json
{ "error": "Unauthorized" }
```

with HTTP 401 (REST) or WebSocket close code `4001` (WS).

---

## REST Endpoints

### Session Management

#### `POST /api/sessions/register`

Register a new assistant session. Called by the `on-session-start` hook.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Assistant session ID (alphanumeric + hyphens, max 128 chars) |
| `project` | string | yes | Project name (derived from `basename` of cwd) |
| `project_dir` | string | yes | Absolute path to the project directory |
| `tmux_target` | string | no | Tmux pane target (e.g. `"voxherd:0.0"`) |
| `assistant` | string | no | Assistant id (`claude`, `codex`, `gemini`). Default: `claude` |

**Response:**

```json
{ "ok": true }
```

**Notes:**
- Auto-sets the newly registered project as the active project.
- If a session with the same `tmux_target` already exists, the old one is replaced (deduplication).
- Broadcasts `session_registered` + `state_sync` to all iOS clients.
- Triggers TTS announcement: `"{project} registered."`.

**Errors:**

```json
{ "error": "invalid session_id format (must be alphanumeric/hyphens, max 128 chars)" }
{ "error": "project_dir is required" }
```

---

#### `GET /api/sessions`

List all registered sessions with their current status.

**Response:**

```json
{
  "<session_id>": {
    "session_id": "abc-123",
    "assistant": "claude",
    "project": "voxherd",
    "project_dir": "/home/user/projects/voxherd",
    "status": "active",
    "last_summary": "Built the WebSocket reconnection logic.",
    "registered_at": "2026-02-16T10:30:00",
    "last_activity": "2026-02-16T10:45:00",
    "tmux_target": "voxherd:0.0",
    "activity_snippet": "Running pytest...",
    "terminal_preview": "Running pytest...\n3 passed, 0 failed",
    "activity_type": "testing",
    "stop_reason": "",
    "agent_number": 1,
    "sub_agent_count": 2,
    "sub_agent_tasks": []
  }
}
```

**Session fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Unique assistant session identifier |
| `assistant` | string | Assistant id (`claude`, `codex`, `gemini`) |
| `project` | string | Project name |
| `project_dir` | string | Absolute path to the project directory |
| `status` | string | `"active"`, `"idle"`, or `"waiting"` |
| `last_summary` | string | Last stop-hook summary (1-2 sentences) |
| `registered_at` | string | ISO timestamp of registration |
| `last_activity` | string | ISO timestamp of last status change |
| `tmux_target` | string or null | Tmux pane identifier |
| `activity_snippet` | string | Most recent meaningful terminal line |
| `terminal_preview` | string | Last 6-8 non-chrome terminal lines (newline-separated) |
| `activity_type` | string | Current activity (see table below) |
| `stop_reason` | string | Why session stopped: `end_turn`, `error`, `interrupted`, `sigint`, `bridge_restart` |
| `agent_number` | int | Ordinal within project (1, 2, 3...) for multi-agent |
| `sub_agent_count` | int | Number of active sub-agents (Task tool) |
| `sub_agent_tasks` | array | Active sub-agent task summaries `[{id, subject, status}]` |

**Activity types:**

| Value | Meaning |
|-------|---------|
| `thinking` | Assistant is reasoning (spinner detected) |
| `writing` | Edit/Write/MultiEdit tool active |
| `searching` | Read/Grep/Glob/LS tool active |
| `running` | Bash tool active (generic) |
| `testing` | Bash tool running tests |
| `building` | Bash tool compiling/building |
| `working` | Task tool or generic active state |
| `completed` | Session stopped normally |
| `sleeping` | Session registered but not yet active |
| `errored` | Session stopped with an error |
| `stopped` | Session interrupted (Ctrl-C / SIGINT) |
| `approval` | Session waiting for user permission |

---

#### `DELETE /api/sessions/{session_id}`

Remove a single session by ID.

**Response:**

```json
{ "ok": true }
```

```json
{ "error": "Session not found" }
```

**Notes:** Broadcasts `session_removed` + `state_sync` to iOS. Cancels any active terminal subscriptions for the session.

---

#### `DELETE /api/sessions`

Remove all sessions.

**Response:**

```json
{ "ok": true, "cleared": 5 }
```

---

#### `GET /api/sessions/{project}/summary`

Get the latest summary for a project. Project name is fuzzy-matched.

**Response:**

```json
{ "project": "voxherd", "summary": "Built the WebSocket reconnection logic." }
```

```json
{ "error": "Project not found" }
```

---

#### `GET /api/sessions/{project}/subagents`

Get sub-agent info for all sessions belonging to a project.

**Response:**

```json
{
  "sessions": [
    {
      "session_id": "abc-123",
      "project": "voxherd",
      "agent_number": 1,
      "sub_agent_count": 2,
      "sub_agent_tasks": [
        { "id": "sa-1", "subject": "Fix auth flow", "status": "running" }
      ]
    }
  ]
}
```

---

### Events

#### `POST /api/events`

Receive hook events from assistant CLIs. This is the primary ingest endpoint -- all hooks POST here.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event` | string | yes | Event type: `"stop"`, `"notification"`, `"subagent_start"`, `"subagent_stop"` |
| `session_id` | string | yes | Assistant session ID |
| `project` | string | no | Project name (default: `"unknown"`) |
| `project_dir` | string | no | Project directory (used for auto-registration) |
| `tmux_target` | string | no | Tmux pane target |
| `assistant` | string | no | Assistant id (`claude`, `codex`, `gemini`) |

**Additional fields by event type:**

**`stop` event:**

| Field | Type | Description |
|-------|------|-------------|
| `summary` | string | 1-2 sentence summary of what was done |
| `stop_reason` | string | `"end_turn"`, `"error"`, `"interrupted"`, `"sigint"` |

**`notification` event:**

| Field | Type | Description |
|-------|------|-------------|
| `message` | string | Notification message (e.g. permission request) |

**`subagent_start` event:**

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | string | Sub-agent identifier |
| `agent_type` | string | Type of sub-agent (e.g. `"task"`) |
| `timestamp` | string | When the sub-agent started |

**`subagent_stop` event:**

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | string | Sub-agent identifier |
| `agent_type` | string | Type of sub-agent |

**Response:**

```json
{ "ok": true }
```

**Notes:**
- Unknown sessions are auto-registered if `project_dir` is provided. This self-heals after bridge restarts.
- `stop` events update session status to `"idle"` and trigger TTS announcement.
- `notification` events with `"waiting for your input"` are suppressed (noise in voice mode).
- All events are enriched with session state and broadcast to iOS as `agent_event`.
- Max payload size: 50KB.

---

### Command Dispatch

#### `POST /api/command`

Dispatch a command to an assistant session via REST. Designed for Siri Shortcuts integration.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project` | string | yes | Target project name (fuzzy-matched) |
| `message` | string | yes | Command text to send to the assistant |
| `session_id` | string | no | Target specific session (highest priority) |
| `agent_number` | int | no | Target specific agent number within project |
| `assistant` | string | no | Assistant filter (`claude`, `codex`, `gemini`) |

**Response:**

```json
{ "ok": true }
```

```json
{ "error": "project and message are required" }
{ "error": "No session found for project 'foo'" }
```

**Notes:**
- Marks session as active immediately.
- Dispatches via `tmux send-keys` (preferred) or assistant-specific resume command (fallback when available).
- Fire-and-forget -- the stop hook will notify when the task completes.
- Broadcasts `command_accepted` to iOS.

---

### Intent Parsing

#### `POST /api/intent/parse`

Parse a voice transcription into a structured intent using Haiku. Called by the iOS app when regex-based parsing fails.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `transcription` | string | yes | Raw voice transcription text |
| `known_projects` | array | no | List of known project name strings |
| `active_project` | string | no | Currently active project name |

**Response (success):**

```json
{ "action": "tell", "project": "voxherd", "message": "run the tests" }
```

**Valid actions:**

| Action | Fields | Description |
|--------|--------|-------------|
| `status` | -- | Request status of all sessions |
| `tell` | `project`, `message` | Send command to a project |
| `switch` | `project` | Switch active project |
| `approve` | `project` | Approve pending permission |
| `deny` | `project` | Deny pending permission |
| `what_did` | `project` | Ask what a project did last |
| `pause` | `project` | Pause a project |
| `resume` | `project` | Resume a project |

**Response (error):**

```json
{ "error": "Intent parsing timed out" }
{ "error": "Intent parsing failed" }
```

**Notes:** Uses `claude -p` with `claude-haiku-4-5-20251001` model. 5-second timeout.

---

### TTS

#### `POST /api/tts`

Speak text through the bridge's macOS TTS queue.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Text to speak |
| `project` | string | no | Project context for logging (default: `"tts"`) |
| `listen_after` | bool | no | Whether to start Mac STT after speaking (default: based on `--listen` flag) |

**Response:**

```json
{ "ok": true }
```

---

### Project Management

#### `GET /api/projects`

List available projects that can be spawned. Merges configured projects (`~/.voxherd/projects.json`) with auto-discovered tmux sessions.

**Response:**

```json
{
  "projects": [
    { "name": "voxherd", "dir": "/home/user/projects/voxherd", "source": "configured" },
    { "name": "my-app", "dir": "/home/user/projects/my-app", "source": "discovered" }
  ]
}
```

`source` is either `"configured"` (from projects.json) or `"discovered"` (from tmux).

---

#### `POST /api/projects/add`

Add a project to `~/.voxherd/projects.json`.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Project name |
| `dir` | string | yes | Absolute path to project directory |

**Response:**

```json
{ "ok": true }
```

**Errors:**

```json
{ "error": "name and dir are required" }
{ "error": "Invalid project directory: directory does not exist: /foo" }
{ "error": "Project 'voxherd' already exists" }
```

**Notes:**
- Directory is canonicalized via `os.path.realpath`.
- Must be under user home (`~` or `/Users/`).
- Forbidden directories: `/`, `/etc`, `/var`, `/tmp`, `/usr`, `/bin`, `/sbin`, `/System`, `/Library`.

---

#### `GET /api/tmux/sessions`

Unified view of all tmux sessions cross-referenced with registered sessions and configured projects.

**Response:**

```json
{
  "projects": [
    {
      "name": "voxherd",
      "dir": "/home/user/projects/voxherd",
      "tmux_session": "voxherd",
      "has_live_process": true,
      "is_registered": true,
      "registered_session_id": "abc-123",
      "assistant": "claude",
      "status": "active",
      "activity_type": "writing",
      "last_summary": "Built the thing.",
      "is_configured": true,
      "agent_number": 1
    },
    {
      "name": "my-app",
      "dir": "/home/user/projects/my-app",
      "tmux_session": null,
      "has_live_process": false,
      "is_registered": false,
      "registered_session_id": null,
      "assistant": null,
      "status": null,
      "activity_type": null,
      "last_summary": null,
      "is_configured": true,
      "agent_number": null
    }
  ],
  "stats": {
    "total": 5,
    "running": 2,
    "registered": 2,
    "configured": 3
  }
}
```

**Notes:** Includes three categories of entries:
1. Tmux sessions with registered assistant instances (one entry per agent).
2. Tmux sessions without registered sessions (pane exists but no hook has fired).
3. Configured projects with no tmux session (available to spawn).

---

### Tasks

Task lists are stored as JSON files under `~/.claude/tasks/`. Each project maps to a directory by name.

#### `GET /api/tasks/{project}`

List all tasks for a project.

**Response:**

```json
{
  "tasks": [
    {
      "id": "1",
      "subject": "Add WebSocket reconnection",
      "description": "Implement exponential backoff...",
      "activeForm": "Add WebSocket reconnection",
      "status": "pending",
      "blocks": [],
      "blockedBy": []
    }
  ],
  "project": "voxherd"
}
```

---

#### `POST /api/tasks/{project}`

Create a new task.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | yes | Task title |
| `description` | string | no | Detailed description |
| `activeForm` | string | no | Active verb form of the subject (defaults to subject) |

**Response:**

```json
{ "ok": true, "task": { "id": "2", "subject": "...", "status": "pending", ... } }
```

---

#### `PATCH /api/tasks/{project}/{task_id}`

Update a task's fields.

**Request body** (all optional):

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | New status (e.g. `"done"`, `"pending"`) |
| `subject` | string | Updated title |
| `description` | string | Updated description |
| `activeForm` | string | Updated active form |

**Response:**

```json
{ "ok": true, "task": { "id": "2", "subject": "...", "status": "done", ... } }
```

```json
{ "error": "No task list for project 'foo'" }
{ "error": "Task '99' not found" }
```

---

### Infrastructure

#### `GET /api/ports`

Scan common dev server ports and return which are listening.

**Response:**

```json
{
  "ports": [
    { "port": 3000, "url": "http://localhost:3000" },
    { "port": 8080, "url": "http://localhost:8080" }
  ]
}
```

**Scanned ports:** 3000, 3001, 4000, 4200, 5000, 5173, 5174, 8000, 8080, 8888, 9000.

---

#### `GET /api/connection-info`

Return local and Tailscale connection info for iOS auto-discovery.

**Response:**

```json
{
  "local": "ws://macbook.local:7777/ws/ios",
  "tailscale": {
    "ip": "100.64.1.5",
    "hostname": "macbook"
  }
}
```

`tailscale` is `null` if Tailscale is not detected.

---

## WebSocket: `/ws/ios`

Persistent bidirectional connection for the iOS app. Supports up to 10 concurrent connections.

### Connection

```
ws://localhost:7777/ws/ios
ws://localhost:7777/ws/ios?token=<auth_token>
```

On connect, the server immediately sends a `state_sync` message with all current session data.

**Close codes:**
- `4001` -- Unauthorized (bad or missing token).
- `4002` -- Connection limit reached (max 10).

### Messages from Server

All messages have a `"type"` field.

#### `state_sync`

Full state snapshot. Sent on connect, reconnect, and after bulk changes.

```json
{
  "type": "state_sync",
  "sessions": {
    "<session_id>": { ... session fields ... }
  },
  "active_project": "voxherd",
  "last_announced_session_id": "abc-123"
}
```

---

#### `agent_event`

Hook event forwarded from an assistant CLI. Contains all fields from the original `POST /api/events` payload, enriched with session state.

```json
{
  "type": "agent_event",
  "event": "stop",
  "session_id": "abc-123",
  "project": "voxherd",
  "summary": "Built the reconnection logic.",
  "stop_reason": "end_turn",
  "activity_type": "completed",
  "agent_number": 1,
  "sub_agent_count": 0,
  "last_summary": "Built the reconnection logic."
}
```

---

#### `session_registered`

A new session was registered. Contains all session fields plus metadata.

```json
{
  "type": "session_registered",
  "session_id": "abc-123",
  "project": "voxherd",
  "project_dir": "/home/user/projects/voxherd",
  "assistant": "claude",
  "status": "idle",
  "agent_number": 1,
  "replaces": ["old-session-id"],
  "set_active_project": "voxherd"
}
```

`replaces` lists session IDs that were removed due to tmux_target deduplication.

---

#### `session_removed`

A session was deregistered (pruned, manually deleted, or pane gone).

```json
{
  "type": "session_removed",
  "session_id": "abc-123",
  "project": "voxherd"
}
```

---

#### `activity_update`

Periodic update from tmux polling (~1.5s interval). Sent when the activity snippet, type, or terminal preview changes.

```json
{
  "type": "activity_update",
  "session_id": "abc-123",
  "project": "voxherd",
  "snippet": "Running pytest...",
  "terminal_preview": "Running pytest...\n3 passed, 0 failed",
  "activity_type": "testing",
  "status": "active",
  "sub_agent_count": 0
}
```

---

#### `sub_agent_update`

Sub-agent count changed (from hooks or task-file polling).

```json
{
  "type": "sub_agent_update",
  "session_id": "abc-123",
  "project": "voxherd",
  "sub_agent_count": 3,
  "sub_agent_tasks": [
    { "id": "sa-1", "subject": "Fix auth", "status": "running" }
  ]
}
```

---

#### `command_accepted`

A voice command was accepted and dispatched.

```json
{
  "type": "command_accepted",
  "session_id": "abc-123",
  "project": "voxherd",
  "assistant": "claude",
  "message": "run the tests",
  "activity_type": "working"
}
```

---

#### `terminal_content`

Streamed terminal output for a subscribed session. Sent ~1s when content changes.

```json
{
  "type": "terminal_content",
  "session_id": "abc-123",
  "lines": ["line 1", "line 2", "..."]
}
```

Lines include ANSI escape sequences (for terminal rendering on iOS).

---

#### `terminal_subscription_ended`

A terminal subscription was cancelled because the session was removed or replaced.

```json
{
  "type": "terminal_subscription_ended",
  "session_id": "abc-123",
  "project": "voxherd"
}
```

---

#### `tasks_sync`

Response to `tasks_request`, or sent after task mutations.

```json
{
  "type": "tasks_sync",
  "project": "voxherd",
  "tasks": [ { "id": "1", "subject": "...", "status": "pending", ... } ]
}
```

---

#### `spawn_accepted`

A new assistant session was successfully spawned in tmux.

```json
{
  "type": "spawn_accepted",
  "project": "voxherd",
  "tmux_session": "vh-voxherd-a1b2",
  "assistant": "claude"
}
```

---

#### `session_stopped`

A session was gracefully stopped (Ctrl-C sent).

```json
{
  "type": "session_stopped",
  "tmux_session": "vh-voxherd-a1b2"
}
```

---

#### `session_killed`

A tmux session was killed entirely.

```json
{
  "type": "session_killed",
  "tmux_session": "vh-voxherd-a1b2"
}
```

---

#### `error`

An error occurred during command processing.

```json
{
  "type": "error",
  "message": "No session found for project 'foo'"
}
```

May optionally include `session_id` if the error relates to a specific session.

---

### Messages from Client

All messages must have a `"type"` field. Max message size: 50KB.

#### `voice_command`

Send a command to an assistant session.

```json
{
  "type": "voice_command",
  "project": "voxherd",
  "message": "run the tests",
  "assistant": "claude",
  "session_id": "abc-123",
  "agent_number": 1
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project` | string | yes | Target project name (fuzzy-matched) |
| `message` | string | yes | Command text |
| `assistant` | string | no | Assistant filter (`claude`, `codex`, `gemini`) |
| `session_id` | string | no | Target specific session (highest priority) |
| `agent_number` | int | no | Target specific agent number within project |

**Resolution order:** `session_id` > `project` + `agent_number` (+ optional `assistant`) > `project` (+ optional `assistant`).

**Rate limit:** 10 commands per session per 60 seconds.

---

#### `status_request`

Request a full state sync.

```json
{ "type": "status_request" }
```

Server responds with a `state_sync` message.

---

#### `tasks_request`

Request all tasks for a project.

```json
{
  "type": "tasks_request",
  "project": "voxherd"
}
```

Server responds with a `tasks_sync` message.

---

#### `task_create`

Create a new task via WebSocket.

```json
{
  "type": "task_create",
  "project": "voxherd",
  "subject": "Add tests for intent parser",
  "description": "Cover all regex patterns...",
  "activeForm": "Add tests for intent parser"
}
```

Server responds with a `tasks_sync` message.

---

#### `task_update`

Update an existing task via WebSocket.

```json
{
  "type": "task_update",
  "project": "voxherd",
  "task_id": "3",
  "status": "done"
}
```

Updatable fields: `status`, `subject`, `description`, `activeForm`. Server responds with a `tasks_sync` message.

---

#### `terminal_subscribe`

Start streaming terminal output for a session.

```json
{
  "type": "terminal_subscribe",
  "session_id": "abc-123"
}
```

Server begins sending `terminal_content` messages ~1s when content changes. If the session has no tmux target, an `error` message is sent followed by a `state_sync`.

---

#### `terminal_unsubscribe`

Stop streaming terminal output for a session.

```json
{
  "type": "terminal_unsubscribe",
  "session_id": "abc-123"
}
```

---

#### `terminal_send_keys`

Send keystrokes to a session's tmux pane.

```json
{
  "type": "terminal_send_keys",
  "session_id": "abc-123",
  "keys": "y",
  "literal": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Target session |
| `keys` | string | yes | Key sequence to send (max 10,000 chars) |
| `literal` | bool | no | If `true`, uses `tmux send-keys -l` (character-by-character, no key name interpretation). Default: `false` (key names like `"Enter"`, `"Up"` are interpreted). |

---

#### `spawn_session`

Spawn a new assistant session in a new tmux window.

```json
{
  "type": "spawn_session",
  "project": "voxherd",
  "dir": "/home/user/projects/voxherd",
  "assistant": "claude",
  "prompt": "Fix the login bug"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project` | string | yes | Project name (looked up in `~/.voxherd/projects.json`) |
| `dir` | string | no | Explicit directory (overrides projects.json lookup) |
| `assistant` | string | no | Assistant to launch (`claude`, `codex`, `gemini`). Default: `claude` |
| `prompt` | string | no | Initial prompt to send after the assistant initializes (~3s delay) |

Server responds with `spawn_accepted` on success or `error` on failure.

**Notes:**
- Creates a tmux session named `vh-{project}-{hex}`.
- Runs the selected assistant CLI in the project directory.
- Claude/Gemini self-register via hooks; Codex is auto-registered by the bridge.

---

#### `stop_session`

Gracefully stop a session by sending Ctrl-C to its tmux pane.

```json
{
  "type": "stop_session",
  "tmux_session": "vh-voxherd-a1b2"
}
```

Server responds with `session_stopped`. Only VoxHerd-managed sessions (prefixed `vh-` or registered with the bridge) can be stopped. Protected sessions (`bridge`, `vh-bridge`) are rejected.

---

#### `kill_session`

Kill a tmux session entirely (removes the tmux window and deregisters all associated sessions).

```json
{
  "type": "kill_session",
  "tmux_session": "vh-voxherd-a1b2"
}
```

Server responds with `session_killed`. Same safety restrictions as `stop_session`.

---

## Error Handling

**REST endpoints** return JSON with either `{"ok": true, ...}` on success or `{"error": "message"}` on failure. HTTP status is 200 for application errors (except 401 for auth failures).

**WebSocket errors** are sent as `{"type": "error", "message": "..."}` messages. The connection is not closed on errors -- only on auth failure or connection limit.

**Input validation:**
- `session_id`: alphanumeric + hyphens, max 128 chars.
- `project`: alphanumeric + hyphens/underscores/dots/spaces, max 100 chars.
- `summary`: max 2,000 chars, control characters stripped.
- `message`: max 5,000 chars, control characters stripped.
- Event payload: max 50KB.
- WebSocket message: max 50KB.
- Key input (`terminal_send_keys`): max 10,000 chars.

**Rate limiting:** Voice commands are rate-limited to 10 dispatches per session per 60-second window. Exceeding the limit returns an error message over WebSocket.

**Background processes:**
- Dead session pruning runs every 30 seconds.
- Activity polling (tmux capture-pane) runs every 1.5 seconds for active sessions.
- Sub-agent task-file scanning runs every ~4.5 seconds.
- Sessions with 3 consecutive tmux pane-check failures are automatically deregistered.
