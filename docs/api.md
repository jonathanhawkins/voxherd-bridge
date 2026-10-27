# VoxHerd Bridge Server API Reference

## Overview

The bridge server is a FastAPI application that acts as the central nervous system between assistant CLIs (via hooks/tmux), the iOS app (via WebSocket), and Siri Shortcuts (via REST). It runs on port 7777 by default.

**Base URL:** `http://localhost:7777`
**WebSocket:** `ws://localhost:7777/ws/ios`

The server is CLI-first -- it runs in a terminal window (tmux required for TCC access) with a live `rich` event log. No web UI, no browser dashboard.

## Authentication

Auth is controlled by the `VOXHERD_AUTH_TOKEN` env var or `~/.voxherd/auth_token` file. If neither exists, the bridge auto-generates a token on first startup and writes it to the file with mode `0600`.

When a token is set, **all** REST and WebSocket endpoints require authentication — except
`/health`, the brand PNGs under `/api/assets/`, and the two pairing endpoints, which are how a
headset gets the token ([Pairing a headset](#pairing-a-headset)).

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

### Host, CSRF and body limits

Three things besides the token can refuse a REST request. A client that treats them as a generic
network error will look like it is hanging, so handle them explicitly.

| Status | Body | Cause |
|---|---|---|
| `421` | `{"error": "Unrecognized Host"}` | The `Host` header is not a name or address this machine answers to. Accepted: `localhost`, `127.0.0.1`, `::1`, any bare IP literal, this machine's `gethostname()` and (on macOS) its Bonjour `LocalHostName`, each with and without `.local`, its Tailscale name, and anything listed in `VOXHERD_ALLOWED_HOSTS` (comma-separated — use it for a reverse proxy, an `/etc/hosts` alias or a `.home.arpa` name). A *suffix* is not enough: `evil.local` is refused, because an mDNS responder on the LAN controls both halves of a name it invents, which is exactly the rebinding this check exists to stop. |
| `403` | `{"error": "Missing X-VoxHerd header"}` | A state-changing method (`POST`/`PUT`/`PATCH`/`DELETE`) without the `X-VoxHerd` header, or a pairing call without it. |
| `413` | `{"error": "Payload too large"}` | The request body exceeds 1 MiB. Enforced before auth and before parsing, so it applies to the token-free endpoints too. |

The `Host` check is not decoration: the `X-VoxHerd` header defends against CSRF only because a
browser cannot set a custom header cross-origin, and DNS rebinding makes a request *same-origin*
from the browser's point of view, which restores that ability. Pinning `Host` is what rebinding
cannot forge.

`/api/intent/parse` returns only the eight documented `action` values, and only the
`action`/`project`/`message` fields — the model's reply is untrusted (the transcription is
interpolated into its prompt), so anything else is dropped rather than passed through.

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
| `model` | string | no | Model the assistant is running (`"claude-opus-5-5"`), when it says. Claude Code's SessionStart does. A malformed value is dropped, never refused |

**Response:**

```json
{ "ok": true }
```

**Notes:**
- Auto-sets the newly registered project as the active project.
- If a session with the same `tmux_target` already exists, the old one is replaced (deduplication).
- A registration with no `model` keeps the one already known for that session id, or for the
  session it replaces on the same pane when the assistant is the same (`/clear` starts a new
  session id in the same process).
- Broadcasts `session_registered` to all clients — **not** `state_sync`. A client that builds its
  session list only from `state_sync` re-asks with `status_request` (the headset client does).
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
    "model": "claude-opus-5-5",
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
| `model` | string | Model the assistant is running, e.g. `claude-opus-5-5`. A separate axis from `assistant` — one assistant runs many models. `""` until known. Set at registration when the hook sends it, then re-read from the transcript's newest main-thread assistant turn at every Stop, so a `/model` switch shows after the next turn. Claude Code transcripts only |
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
| `stop_reason` | string | `"end_turn"`, `"error"`, `"interrupted"`, `"sigint"`, `"max_steps"` |
| `model` | string | Optional. The model that ran the turn, when the hook knows (Antigravity's hooks carry `modelName`). Used only when the bridge cannot read the model from the transcript itself; a malformed value is ignored |

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
| `message` | string | yes | Command text to send to the assistant. Rewritten before it is typed — see **Notes** |
| `session_id` | string | no | Target specific session (highest priority) |
| `agent_number` | int | no | Target specific agent number within project |
| `assistant` | string | no | Assistant filter (`claude`, `codex`, `gemini`) |
| `reference` | object | no | Code the user pointed at in a reader — see [Pointing](#pointing-reference) |

**Response:**

```json
{ "ok": true }
```

```json
{ "error": "project and message are required" }
{ "error": "No session found for project 'foo'" }
{ "error": "reference: path is outside the project" }
```

**What happens to `message` before it is typed.** The response is `{"ok": true}`
either way, so none of this is visible to the caller:

- It is capped at **5 000 characters**; anything past that is dropped silently.
- Control characters are stripped, and **CR, LF and TAB are collapsed to
  spaces**. The dispatch path types the text with `tmux send-keys -l` and then
  sends Enter as a separate key, so an embedded newline would submit the message
  early and leave the remainder to land in whatever the assistant showed next.
  A multi-line message therefore arrives as one line.
- A bare ESC is removed for the same reason, and a sharper one: with a select
  widget on screen, `ESC [ B` is Down and the following Enter confirms whatever
  the cursor moved to. A reply once approved the command it meant to deny.
- If nothing survives, the dispatch is refused rather than sending a bare Enter,
  the session's status is reset to idle, and an `error` goes out over the
  WebSocket.

**Notes:**
- Marks session as active immediately.
- Dispatches via `tmux send-keys` (preferred) or assistant-specific resume command (fallback when available).
- Fire-and-forget -- the stop hook will notify when the task completes.
- Broadcasts `command_accepted` to iOS.

---

### Intent Parsing

#### `POST /api/intent/parse`

Parse a voice transcription into a structured intent using the install profile's fast model. Called by the iOS app when regex-based parsing fails.

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

**Notes:** 15-second timeout. Every CLI spends most of it starting up, not answering: measured, `claude -p` took 6.3–11.4 s end to end around a ~1.5 s model call, and `agy` 4–9 s. At the old 5 s nearly every parse timed out. The CLI and model come from the **install-time model profile**, not from the request — a caller cannot pick the model:

| Profile | Command |
|---|---|
| `anthropic` (default) | `claude -p "<prompt>" --model claude-haiku-4-5-20251001 --output-format json` |
| `google` | `agy -p "<prompt>" --model gemini-3.8-flash-low --disable-slash-commands --output-format json` |

The `google` profile runs Antigravity's `agy`: Google retired Gemini CLI's personal-account sign-in on 2026-06-18. Gemini CLI still works when set by hand (`VOXHERD_NLU_CLI=gemini`) with a `GEMINI_API_KEY`. The iOS app waits 17 s, a little longer than the bridge, so it hears the bridge's answer or its timeout rather than giving up first; headset clients parse speech on-device and never call it. The CLI runs with `VOXHERD_HOOK_RUNNING=1`, so its own Stop hook does not report the parse to the bridge as a finished session.

The profile is written to `~/.voxherd/models.env` by `MODEL_PROFILE=google bash hooks/install.sh` and read once per process (`bridge/model_profile.py`). Set `VOXHERD_NLU_CLI` / `VOXHERD_NLU_MODEL` in the bridge's environment to override for one session. A missing or corrupt `models.env` falls back to the Anthropic default.

Each CLI wraps the reply differently and the endpoint unwraps each: `claude` returns `{"type":"result","result":"<text>"}`, `gemini` returns `{"response":"<text>","stats":{…}}`, `agy` returns `{"status":"SUCCESS","response":"<text>","usage":{…}}`. A CLI with no JSON envelope has the action object scraped out of raw stdout. A CLI reporting an error — for `agy`, any status but `SUCCESS` — yields `{"error": "Intent parsing failed"}`.

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

```json
{ "error": "text is required" }
```

`text` is capped at **2 000 characters** and silently truncated past that;
control characters other than newline and tab are stripped. `{"ok": true}` means
queued, not spoken — the queue drains one utterance at a time, and each gets a
length-scaled budget after which the speech process is killed so one utterance
cannot silence every later one. The budget is
`min(300, max(60, len(text) / 7.5 + 30))` seconds: a 60 s floor, 7.5 characters
per second plus 30 s of slack, and a 300 s ceiling. At the 2 000-character cap
that is 297 s, so the ceiling costs a legitimate `/api/tts` call nothing — it
bounds the internal `narration` path, which batches up to five project summaries
into one utterance and could otherwise budget 23 minutes.

An empty or non-string `text` is the only refusal; everything else is accepted.

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

### Command Catalog

Everything a focused session can be told to do — slash commands, agents, skills, MCP servers, plugins — for a client's command palette. See `bridge/catalog.py`.

Three tiers are merged into one response:

| Tier | Source | Cost | Notes |
|------|--------|------|-------|
| 1 | Static manifest in `bridge/catalog.py` | free | Every built-in slash command each CLI's terminal UI offers, measured from one build (`builtin_version`, below), plus Claude Code's built-in agents. Compiled into the CLIs, so **refresh on CLI version bumps**. |
| 2 | Filesystem scan | ~10 ms | The primary tier. `~/.claude/commands`, `agents`, `skills`, installed plugins, `~/.codex/agents`, `~/.grok/bundled`, agy's `~/.gemini/config/skills` and a workspace's `.agents/skills`, each assistant's MCP config (below), etc. Every path is optional; a missing directory is normal. |
| 3 | CLI probe | 0.2–9 s | Never runs during a request: a request that finds it stale starts a refresh in the background and answers from cache, so the palette's next open has it. Two probes: `codex mcp list --json`, and Claude Code's answer to the SDK's `initialize` request (below). A probe adds only what Tiers 1 and 2 do not already list. |

Tier 2 is cached on an mtime fingerprint of every matched file, so repeat calls skip parsing. The palette always renders — a stale entry beats a spinner in a headset.

Reads are bounded and sandboxed: realpath + allowed-root validation, `O_NOFOLLOW`, a 1 MB per-file cap, and only the first 4 KB of each file (frontmatter) is read. A symlink escaping an assistant config root yields no entry. The one file read outside the roots is `~/.claude.json` (exactly that file, parsed whole, up to 16 MB), where Claude Code keeps its user- and local-scope MCP servers.

#### `GET /api/catalog/{project}`

**Query parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `assistant` | string | no | `claude`, `codex`, `gemini`, `grok`, `composer`, `antigravity`. Defaults to the matched session's assistant, then `claude`. |

`{project}` is fuzzy-matched against registered sessions, then against `~/.voxherd/projects.json`. An unmatched project is **not** an error — the user-scope catalog still renders, with `project_matched: false`.

**Entry fields:**

| Field | Type | Description |
|-------|------|-------------|
| `kind` | string | `command` \| `agent` \| `skill` \| `mcp` \| `plugin` |
| `name` | string | Invocation. Commands are slash-prefixed and namespaced (`/git:sync`, `/code-review:go`); agents and skills use their frontmatter `name`. |
| `description` | string | From YAML/TOML frontmatter, whitespace-collapsed, max 300 chars. May be `""`. |
| `source` | string | `builtin` \| `user` \| `project` \| `plugin` |
| `assistant` | string | The assistant this entry belongs to |
| `path` | string \| null | File it came from; `null` for Tier 1 and Tier 3 entries |
| `invoke` | string | *Optional.* What to send to use the entry. Absent when it cannot be invoked on its own (a `plugin`). |
| `needs_args` | bool | *Optional, with `invoke`.* True when the entry means nothing without the user's words: a sentence opener, or a built-in its CLI rejects bare (Grok's `/btw <question>`, agy's `/codesearch <query>`). |
| `argument_hint` | string | *Optional.* A command's declared arguments, e.g. `[version]`; built-ins carry their CLI's own (`[model]`, `<question>`). A prompt to show while listening — whether words are required is `needs_args`. |

**Using an entry.** Send `invoke`, then a space and whatever the user dictated, as a
`voice_command` to the session — never as raw keys, so busy sessions queue and dead
panes report. The bridge owns the idiom so no client has to know it:

| Entry | `invoke` | `needs_args` |
|---|---|---|
| command | its name: `/git:sync` | false — true for a built-in its CLI rejects bare |
| Claude, Grok or Antigravity skill | `/<name>`: `/br`, `/code-review`, `/automation` | false |
| agent | `Use the <name> agent to` | true |
| Codex or Gemini skill | `Use the <name> skill to` | true |
| MCP server | `Use the <name> MCP server to` | true |

**Built-in commands** (Tier 1) are measured per build, since every CLI compiles them in
and none lists its interactive ones from outside:

| Assistant | Build | Measured from |
|---|---|---|
| claude | 2.1.283 | the command objects in the binary — each one's interactive variant — and its `initialize` answer |
| codex | 0.154.0 | `codex-rs/tui/src/slash_command.rs` at `rust-v0.154.0`, as built for macOS |
| gemini | 0.61.0 | `BuiltinCommandLoader` in the npm bundle, with default settings |
| grok, composer | 1.0.30 | the TUI's command builders and the shell's command table in the binary |
| antigravity | 1.2.12 | the command registry in the binary (its docs page is older: it still lists the removed `/fast` and `/planning`). agy updates itself in place |

Hidden, debug, other-platform and default-off commands and every alias are left out: a
missing command costs a keystroke, one that is not there puts "Unknown command" in the
terminal. A `/model` that also becomes the default for every session started later says
so in its description — Claude Code's, Codex's (and its `/personality`) and agy's do;
Gemini's is session-only unless `--persist`ed, and Grok keeps a model per session.

**Claude Code's `initialize` probe** asks the installed `claude` for its built-in
commands, bundled skills and agents, the way the Agent SDK does: `claude -p` with
`--input-format stream-json`, one `initialize` control request on stdin, then EOF. The
answer comes back in ~0.2 s, before and without any model call. It runs signed out, in
an empty temporary `HOME` and working directory, with `--setting-sources project`,
`--strict-mcp-config` and an empty MCP config, and without `CLAUDE_*`, `ANTHROPIC_*` or
`VOXHERD_*` in its environment — so it loads none of the user's settings, hooks, plugins
or MCP servers, and writes nothing of theirs. Its commands are a headless session's: Tier 1
keeps the terminal's wording for the ones it has, and the probe contributes what a newer
build added (`/brand-new`) plus the built-in agents.

**MCP servers are the ones each assistant loads** — measured 2026-09-27 by having each CLI
write its own config in a throwaway `HOME` and asking it (`<cli> mcp list`) what it loads:

| Assistant | Where | Off |
|---|---|---|
| claude | `~/.claude.json`: `mcpServers` (user) and `projects[<realpath>].mcpServers` (local); the account's claude.ai connectors; enabled plugins' `.mcp.json` / `plugin.json`; the project's `.mcp.json` **once approved** | `disabledMcpjsonServers` anywhere; the project's `disabledMcpServers` (`/mcp` → disable), any scope — a plugin's server as `plugin:<plugin>:<server>` |
| codex | `~/.codex/config.toml` `[mcp_servers.*]`, plus what its bundled plugins add (Tier 3) | `enabled = false` |
| grok, composer | `~/.grok/config.toml` and `./.grok/config.toml` `[mcp_servers.*]` | `enabled = false`, `disabled_mcp_servers` |
| antigravity | `~/.gemini/config/mcp_config.json` | `"disabled": true` |
| gemini | `~/.gemini/settings.json`, `./.gemini/settings.json`, extensions — **only in a folder it trusts** (`~/.gemini/trustedFolders.json`) | |

Only Claude reads `.mcp.json`, and only a server that is approved: in user settings
(`enabledMcpjsonServers`, `enableAllProjectMcpServers`) at any time, or in the project's
settings, `settings.local.json` or its `~/.claude.json` record once the folder is trusted.
Otherwise Claude Code lists it as "Pending approval" and never loads it. A server named in
several scopes is one row. An MCP entry's `description` is the server's own description or
its executable's name — never its arguments, environment, URL or headers, which is where
keys are kept.

**claude.ai connectors** (Gmail, Google Drive, …) come with the signed-in account, not a
config file: Claude Code fetches them from claude.ai at startup. Its only local record is
`claudeAiMcpEverConnected` in `~/.claude.json` — every connector that has ever connected,
by display name (`claude.ai Gmail`) — so one disconnected since is still listed. Their
`description` is `claude.ai connector`.

**Claude plugins are what Claude Code actually loads**, not what a marketplace offers:
`~/.claude/plugins/installed_plugins.json` filtered by `enabledPlugins` (user settings,
then the project's `.claude/settings.json`, then `settings.local.json`), with `project`
and `local` installs applying only in their `projectPath`. A plugin's commands, agents
and skills are namespaced by the plugin's name — `/code-review:go`, `codex:rescue`.

**Response:**

```json
{
  "project": "voxherd",
  "project_matched": true,
  "project_dir": "/Users/you/dev/voxherd",
  "assistant": "claude",
  "entries": [
    {
      "kind": "command",
      "name": "/clear",
      "description": "Start a new session with empty context; previous session stays on disk (resumable with /resume)",
      "source": "builtin",
      "assistant": "claude",
      "path": null,
      "argument_hint": "[name]",
      "invoke": "/clear",
      "needs_args": false
    },
    {
      "kind": "skill",
      "name": "br",
      "description": "Beads Rust issue tracker. Use for creating/managing/querying issues...",
      "source": "user",
      "assistant": "claude",
      "path": "/Users/you/.claude/skills/br/SKILL.md",
      "invoke": "/br",
      "needs_args": false
    }
  ],
  "counts": { "command": 99, "agent": 14, "skill": 38, "mcp": 16 },
  "total": 167,
  "cached": false,
  "probe_stale": true,
  "builtin_version": "2.1.283"
}
```

`cached` reports whether Tier 2 was served from the mtime cache. `probe_stale` is `true` when Tier 3 has never run or has aged past its 15-minute TTL. `builtin_version` is the CLI build Tier 1 was measured against — when the installed CLI is newer, its built-ins may have moved on.

```json
{ "error": "invalid project name (alphanumeric/hyphens/underscores/dots/spaces, max 100 chars)" }
```

---

### The Reader

Read a registered project's code, and point at it: the file highlighted, the regions a
hand can target, and the working tree's diff. The client renders; everything that needs
a grammar, a repository or a filesystem runs here. See `bridge/reader.py`.

Every endpoint takes `session_id` (preferred) or `project`, matched **exactly** (case
aside), never by the fuzzy match other endpoints use: a nearest match would open another
project's files. The root is that session's registered `project_dir`, never a path from
the request.

**This reads arbitrary project files**, so:

- `path` is relative to the project, resolved with `realpath`, and must stay under it —
  a symlink out of the project is refused, not followed;
- the final open is `O_NOFOLLOW | O_NONBLOCK` and must find a regular file: a symlink
  swapped in after the check fails, and a FIFO cannot hang the request;
- a file over 2 MB is refused; past 20,000 lines it is served truncated; a line past
  2,000 characters is cut;
- a line holding a secret is replaced by `[redacted — contains credential]`, keeping its
  number so every line number stays true. The test is `transcript_render`'s own,
  imported rather than copied. A PEM block is redacted from BEGIN to END, because a
  key's body is lines no pattern recognises;
- files that are secrets by nature are refused outright (403):
  - `.env` and `.env.*`, except `.example`/`.sample`/`.template`/`.dist`, and `.envrc`;
  - key material: `.pem`, `.key`, `.p12`, `.pfx`, `.p8`, `.jks`, `.keystore`, `.ppk`,
    `.kdbx`, and SSH private keys;
  - credential files: `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.htpasswd`,
    `credentials(.json)`, `secrets.*` config (`Secrets.xcconfig`, `secrets.yaml`, …)
    and service-account JSON;
  - anything under `.git`, `.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube` or `.docker`;
  - agent and MCP configuration: `.mcp.json`, `mcp.json`, `mcp_config.json`,
    `.codex/config.toml`, `.codex/auth.json`, `.gemini/settings.json` and
    `.claude/settings.local.json`. An MCP server's credentials sit in headers and env
    under names no redaction pattern knows (`X-Api-Key`).

Errors carry an HTTP status and `{"error": "..."}`: 400 a bad request, 403 refused, 404
no such session or file, 409 not a git repository, 413 too large, 415 binary.

#### `GET /api/file`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Relative to the project |
| `session_id` / `project` | string | one | Whose project |
| `start`, `end` | int | no | A page, 1-based and inclusive; at most 4,000 lines a response |

```json
{
  "path": "bridge/model_profile.py",
  "language": "Python",
  "line_count": 433,
  "start": 170, "end": 171,
  "lines": [
    [["def", "k"], [" ", ""], ["parse_models_env", "nf"], ["(", "p"], ["text", "n"], [":", "p"]],
    [["    ", ""], ["\"\"\"Parse ``models.env`` text into a dict", "sd"]]
  ],
  "redacted": [],
  "truncated": false,
  "size": 14210, "mtime": 1790000000.0,
  "session_id": "49ace562-…", "project": "voxherd"
}
```

Each line is runs of `[text, class]`; joined, they are the line exactly (as redacted).
The class is Pygments' short CSS name: theme by its first letter — `k` keyword, `s`
string, `c` comment, `n` name (`nf` function, `nc` class, `nb` builtin, `nd`
decorator), `m` number, `o` operator, `p` punctuation, `g` generic, `err` — and `""` is
plain. The whole file is lexed even for a page, so a page that starts inside a string
is coloured as one. `redacted` lists the line numbers that were.

#### `GET /api/regions`

Same `path` and `session_id`/`project`.

```json
{
  "path": "src/geo/Bearings.kt",
  "language": "kotlin",
  "source": "tree-sitter",
  "line_count": 82,
  "regions": [
    {"kind": "block", "name": "", "start": 1, "end": 1, "depth": 0},
    {"kind": "class", "name": "Bearings", "start": 5, "end": 82, "depth": 0},
    {"kind": "method", "name": "of", "start": 21, "end": 31, "depth": 1}
  ],
  "truncated": false
}
```

Regions come in document order with a `depth`; children follow their parent. At depth 0
they tile the file — definitions, and a `block` for the code between them, a blank line
apart. A definition starts at the doc comment, decorator or attribute directly above
it. `kind` is one of `function`, `method`, `class`, `struct`, `enum`, `interface`,
`protocol`, `extension`, `object`, `trait`, `impl`, `module`, `record`, `type`,
`constructor`, `variable` (a many-line value), `field`, `macro` or `block`.

`source` is `tree-sitter` for Python, JavaScript, TypeScript/TSX, Kotlin, Swift, Go,
Rust and Java. Anything else — or a grammar that fails to load — is `blocks`: top-level
paragraphs, where indented lines and closing brackets stay with the block they close. A
file is parsed as written, but only kinds, names and line numbers leave.

#### `GET /api/diff`

`session_id`/`project` only. The working tree against `HEAD` (the empty tree before the
first commit), then untracked files that are not ignored, as whole-file additions. Paths
are relative to the project directory, which need not be the repository's top.

```json
{
  "files": [
    {"path": "app.py", "old_path": null, "status": "modified", "binary": false,
     "withheld": false, "omitted": false,
     "hunks": [{"old_start": 1, "old_lines": 3, "new_start": 1, "new_lines": 3, "heading": "",
                "lines": [[" ", "a = 1"], ["-", "b = 2"], ["+", "b = 20"], [" ", "c = 3"]]}]}
  ],
  "truncated": false
}
```

- `status` is `modified`, `added`, `deleted`, `renamed` (with `old_path`) or
  `untracked`.
- `withheld` is a file that is never served: listed, without hunks.
- `omitted` is an untracked file too big to show — 256 KB each, 4 MB of diff in all.
- `binary` files are listed without hunks.
- Hunk lines are redacted like any file.

git runs with `GIT_OPTIONAL_LOCKS=0`, so it never takes `index.lock` from under the
agent. It also runs with `core.fsmonitor=false`, no external diff or textconv, colour
off and the `a/`/`b/` prefixes spelled out, so the user's git config cannot change what
is parsed. Each call times out at 10 s. The endpoint answers 409 when the project is not
a git repository.

#### `GET /api/files`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` / `project` | string | one | Whose project |
| `q` | string | no | Ranked: the name itself, then names starting with it, names containing it, paths containing it, then its letters in order |
| `limit` | int | no | 1–500, default 50 |

```json
{"files": ["bridge/reader.py", "bridge/tests/test_reader.py"], "matches": 45, "source": "git", "truncated": true}
```

With no `q`, newest first: what an agent just touched is the likeliest thing to read.
Files come from `git ls-files`, tracked and untracked-but-not-ignored. Outside a
repository the tree is walked instead (`source: "walk"`), skipping dependency and dot
directories. A refused file is never listed.

#### `GET /api/focus`

`session_id`/`project` only. What the session's agent last edited in its project — Follow
mode's starting point. Each new edit after that is pushed as
[`agent_focus`](#agent_focus).

```json
{
  "session_id": "49ace562-…", "project": "homeschool", "assistant": "claude",
  "focus": {
    "path": "src/scheduler/week.ts",
    "ranges": [[142, 168]],
    "verb": "edited",
    "id": "8f1c…",
    "at": "2026-09-28T10:00:00.000Z",
    "withheld": false
  }
}
```

- `focus` is `null` until the agent edits a file inside its project.
- `ranges` are where the edit changed the file as it is now, 1-based and inclusive, one per
  hunk and at most 20. They cover the added lines, not the context around them. A hunk that
  only deletes is the line just above where its lines were.
- `verb` is `created` for a new file, whose one range is the whole file, and `edited` for
  anything else.
- `id` changes with every edit, even one to the same lines.
- `withheld` is a file the Reader never serves (`.env`, keys, agent configuration). It is
  named so the wearer knows where the agent is, and never opened.
- `at` is the transcript's own timestamp, or `""`.

Where the edits come from is the agent's transcript, read under `transcript_render`'s
posture (see `bridge/focus.py`):

- **Claude Code** records each tool's result after the call. An Edit's result carries the
  file and a structured patch; a Write that created a file carries its content. Sub-agents
  keep their own transcripts, so their edits are not followed.
- **Codex** records a `FileChange` for each patch it applies. An edit made with a shell
  command is not a file change to Codex, so it is not followed.
- Anything else answers **422** with `{"error": "following works with Claude Code and Codex
  so far"}`.

An edit counts only when its file resolves inside the project, so an agent writing
elsewhere does not move the focus. A session registered under a temporary id (discovered in
tmux, before its first hook) has no transcript to read yet, and its focus stays `null` until
the hook gives it its real id.

---

### Pairing a headset

How a client that cannot scan the Mac's QR code — a headset with no camera —
gets the token. It asks, the bridge shows a six-digit code on the Mac, and the right code
returns the token. See `bridge/pairing.py`.

These two answer **without** the token, so they are limited:

- they still need the `X-VoxHerd` header (403 without it), so a web page cannot drive them;
- a code is six random digits, good for 2 minutes and 5 tries, compared in constant time;
- at most 3 requests wait at once (429 beyond that);
- 20 wrong codes in 15 minutes lock pairing for 15 minutes (429, with `retry_after` seconds);
- the code is shown on the Mac only: a macOS notification and the bridge's log. The device
  name reaches the notification as an `osascript` argument, never as script text, after
  being cut to `[A-Za-z0-9 ._-]`, 40 characters. The token is never logged.

#### `POST /api/pair/start`

```json
{"device": "Living room headset"}
```
→
```json
{"request_id": "9Qm…", "expires_in": 120, "digits": 6}
```

#### `POST /api/pair/finish`

```json
{"request_id": "9Qm…", "code": "471902"}
```
→ 200, the token and where to reach the bridge (the shape of
[`GET /api/connection-info`](#get-apiconnection-info)):
```json
{"token": "…", "local": "ws://macbook.local:7777/ws/ios", "tailscale": null}
```

| Status | Body | Meaning |
|---|---|---|
| 403 | `{"error": "wrong code", "attempts_left": 3}` | Try again; at 0 the request is gone |
| 410 | `{"error": "that pairing request has expired; start again"}` | Expired, used, or never existed |
| 429 | `{"error": "…", "retry_after": 900}` | Locked after too many wrong codes |

A headset keeps **every** address it knows for the bridge, not just the one it reached: the
addresses the Bonjour advert carried, the `local` name and the `tailscale` name above. A Mac has
several and they all change — the LAN address is a DHCP lease, the tailnet address works only from
the tailnet, a `.local` name needs a resolver that answers — so a client that keeps one is a client
that reports the bridge down the first time it guesses wrong. The Android client ranks them
(`BridgeRoutes`), walks them while pairing (`Pairing`), and rotates over them on each failed
reconnect, remembering whichever one worked.

An address that cannot be reached is not a refusal. A bridge that answers with 403/410/429 has been
reached, so its other addresses would answer the same way: only a connection that never opens is
worth retrying elsewhere. And once a code is up, a `request_id` may only be presented to addresses
of the bridge that issued it — it means nothing in another process.

A token the bridge later refuses — the file was replaced — shows on the headset as "not paired",
and pairing opens again.

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

`local` names the host by its **Bonjour** name (macOS `scutil --get LocalHostName`), not
`gethostname()`, which on macOS is the DHCP name — they differ (`SomeMac-MBP` against
`SomeMac-MacBook-Pro`), and only the Bonjour one resolves. A client handed the DHCP name
reports that it cannot reach the bridge, which reads as the bridge being down. Both names
are in the `Host` allowlist, so either is accepted on the way back in.

An mDNS `.local` lookup is not always available to a client — a headset on a network that
blocks multicast, or whose resolver has stopped answering for the name, gets nothing from
either name. `tailscale.hostname` is the address that does not depend on mDNS; the IP works
until the DHCP lease changes.

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

#### `client_setting`

Sent to every client when a display setting changes.

```json
{ "type": "client_setting", "setting": "theme", "value": "light" }
```

| setting | values | effect |
|---|---|---|
| `theme` | `light`, `dark` | Panel palette. Light is a daylight tool, not a preference — see below. |
| `deck` | `auto`, `focus`, `pair`, `war-room`, `survey` | Slot preset: how many panels share the arc, and at what cell size. |

`deck` defaults to **`auto`**, which is not the same as pinning `pair`: an
unpinned Deck follows the session count, so one session fills the arc and a
second splits it. Pinned, a lone panel would sit at half width with the other
half of the field of view empty. A preset whose panels are too small to read
(`war-room`, `survey`) is glanceable — for spotting which agent needs you, not
for reading what it said.

**`state_sync` carries the current values in `client_settings` and is the authority.** The broadcast
is only the fast path: a client that was disconnected when the setting changed converges on
reconnect rather than staying wrong. An **absent** `client_settings` means an older bridge, not "no
settings" — clients must leave their current value alone rather than resetting to a default.

A light theme is a daylight tool on an additive display, not a preference. A dark terminal emits
almost nothing across most of the panel, so in a bright room it is a transparency with glyphs
floating on it; a light one floods the area and gives the eye a surface.

#### `speech_floor` (client → bridge)

A headset takes, renews or releases the **speech floor**.

```json
{ "type": "speech_floor", "claim": true }
```

Narration is otherwise spoken on the Mac *and* broadcast as `narration` to every client, which
speaks it — so a headset speaking too would make one summary three voices. While a headset holds
the floor:

- the narration engine neither speaks on the Mac nor broadcasts `narration`, and the fallback
  paths and the "`<project>` registered." announcement stay quiet too;
- the headset says it all itself, through Earshot, from the `agent_event` / `choice_prompt` /
  `activity_update` messages every client already gets;
- `POST /api/tts` still speaks on the Mac: it is an explicit request, not narration.

The floor is a **lease** of 60 s. The headset renews it every 20 s; unrenewed, it lapses and the
Mac and phones speak again. It is also released at once when its holder's socket closes — but a
force-stopped client can leave a half-open socket that never closes, so the lapse is what
guarantees it. A floor held for a headset nobody is wearing would be silence that looks exactly
like nothing happening.

A change is announced by a `state_sync` to every client; a renewal changes nothing and sends
nothing. `claim` must be a JSON boolean; anything else is refused with an `error`.

#### `narration`

What the narration engine says, for clients to speak — batched and cooled down on the bridge.
Not sent while a headset holds the speech floor.

```json
{
  "type": "narration",
  "event_type": "completed",
  "project": "voxherd",
  "session_id": "abc-123",
  "summary": "Added retry logic.",
  "tts_text": "Added retry logic.",
  "agent_number": 1,
  "batch_count": 1,
  "listen_after": false
}
```

`event_type` is `completed`, `errored`, `stalled`, `approval` or `batch_completed`. Speak
`tts_text`; `summary` is the raw hook summary. Which of them are sent depends on the
verbosity (`set_verbosity`, below): `stalled` only ever comes at `verbose`.

#### `set_verbosity` (client → bridge)

How much the narration engine says — on the Mac and in `narration` to every client.

```json
{ "type": "set_verbosity", "level": "verbose" }
```

| level | narrated |
|---|---|
| `quiet` | errors and approvals only |
| `normal` | completions too, gathered over 5 s into one announcement; a lone completion within 20 s of its project's last narration is skipped. The default. |
| `verbose` | stalls too (below) |

Errors and approvals are never held back by the 20 s cooldown. There is one level for the
whole bridge, kept only in memory: a restart goes back to `normal`, and no message reports the
current level. An unknown `level` is ignored without a reply. The phone sends this for
"go quiet", "only errors", "tell me everything", "verbose mode", "normal mode" and similar.

**Stalls.** At `verbose`, a working session that has shown no new activity for 60 s is
announced once, as a `narration` with `event_type: "stalled"`:

```json
{ "type": "narration", "event_type": "stalled", "project": "voxherd", "session_id": "abc-123",
  "summary": "No activity detected", "tts_text": "voxherd appears stalled.", "agent_number": 1,
  "batch_count": 1, "listen_after": false }
```

(With more than one agent on the project, `tts_text` names it: "agent 2 on voxherd appears
stalled.") Activity is what the activity poll sees change in the session's tmux pane — the
kind of work on screen (thinking, writing, running, testing…), not the spinner's
elapsed-time counter. So one command that runs, or one stretch of thinking that lasts,
longer than a minute is a stall too. Once announced, a session is not announced again until
it shows new activity or starts a new turn. It is announced only while it is:

- `active` — not `waiting` on the user, and not idle;
- in a tmux pane — nothing else is polled, so a session without one has no activity to go by;
- not quiet (`VOXHERD_QUIET`);
- outside its project's 20 s cooldown, which delays a stall rather than dropping it.

The clocks run at every level, so switching to `verbose` announces at once a session that has
been stuck for a while. Like all narration, a stall is neither spoken nor broadcast while a
headset holds the speech floor, and the headset does not announce stalls itself.

#### `set_client_setting` (client → bridge)

```json
{ "type": "set_client_setting", "setting": "theme", "value": "light" }
```

Unknown names or values are rejected with an `error` rather than ignored — these are broadcast
verbatim to every client, so an arbitrary value here would be an arbitrary value on four devices.
The Mac voice loop raises this from the phrases `go light`, `dark mode`, `switch to dark` and
similar; the command is heard by a phone but is *about* the glasses, which is why the bridge holds
the setting rather than the device that heard it.

#### `state_sync`

Full state snapshot. Sent on connect, reconnect, and after bulk changes.

```json
{
  "type": "state_sync",
  "sessions": {
    "<session_id>": { ... session fields ... }
  },
  "active_project": "voxherd",
  "last_announced_session_id": "abc-123",
  "client_settings": { "theme": "light", "deck": "auto" },
  "voice_state": { "listening": true, "heard": "war room", "wake": true, "at": 1758830000.0 },
  "active_choice_sessions": ["abc-123"],
  "active_form_sessions": [],
  "speech_floor": { "held": true, "expires_in": 41.5 }
}
```

`speech_floor.held` says whether a headset holds the speech floor (see `speech_floor` below).
A headset speaks only when this says `true`. An **absent** `speech_floor` means a bridge that
cannot hold one, so the headset stays quiet rather than doubling every summary.

`client_settings` and `voice_state` are **authoritative here**, not only in their
broadcasts: a client that was disconnected when either changed would otherwise come back
on a stale value and stay there. Absent means an older bridge — leave the current value
alone rather than resetting to a default.

`active_choice_sessions` / `active_form_sessions` are the sessions with a live
`choice_prompt` / `question_form` on their terminal **right now**. A client drops any
decision card whose session is not in the matching set. This is how a card that missed
its `*_cancelled` broadcast — the client was disconnected while the user answered at the
Mac — gets cleared. Absent means an older bridge: prune nothing.

---

#### `choice_prompt`

An assistant is blocked on a numbered choice. Broadcast when the prompt appears or is
replaced by a different one; **replayed on connect** for every prompt still on screen.

```json
{
  "type": "choice_prompt",
  "session_id": "abc-123",
  "project": "voxherd",
  "title": "Do you want to proceed?",
  "options": ["Yes", "Yes, and always allow access to /Users/you/dev from this project", "No"],
  "signature": "3f2a9c0d1e4b5a67",
  "commit_key": "2e548b1fc97f413b1d3f32cbf2261d48",
  "input_mode": "tui_select",
  "body": ["Bash command", "touch probe-file.txt", "Create probe-file.txt"],
  "kind": "permission"
}
```

| Field | Description |
|---|---|
| `options` | Labels in on-screen order, without their `N.` numbers. A label the pane wrapped is rejoined. |
| `signature` | Identity of this prompt's *shape* — title + options only. Send it back with a commit. It deliberately does not move while the body streams in, which is why it cannot be the integrity check: on a permission dialog the title and options are boilerplate, so two prompts for **different commands** share a signature. |
| `commit_key` | Identity of this prompt's *content*, including the command — so two permission dialogs for different commands differ here, where `signature` does not. **Treat it as opaque and do not recompute it from this message.** It is derived from the body as the detector saw it in a ~30-row capture, while `body` above may be a deeper capture taken afterwards so a long plan reads in full; for those prompts the two do not correspond, deliberately. The bridge compares the pane against the prompt it last broadcast before it commits anything, so a client needs no change to be protected. **Optional**: absent means an older bridge. |
| `input_mode` | `tui_select` (a select widget, committed with arrows + Enter) or `number` (an inline list above an ordinary input line). Clients do not need to branch on it — the bridge synthesises the keys. |
| `body` | Text above the options, for a "read" view. For a permission dialog, just the dialog: the tool, the command, the file preview. |
| `kind` | `permission` for Claude Code's tool-permission dialog — option 0 is always "Yes" and one option is labelled "No". `choice` for everything else. **Optional**: absent means an older bridge, treat as `choice`. |

Commit with `terminal_send_keys` + `choice_index` (below). Never type an answer: see
`voice_command` for what happens to text sent while a prompt is up.

A commit is refused when the pane no longer holds the prompt that was broadcast — including
when a **different** dialog of the same shape has replaced it, which `signature` alone cannot
detect. The refusal arrives as `{"type": "error", "message": "The prompt on screen changed — nothing
was answered. Look again."}`; show it and re-read the card rather than retrying. For the same
reason, raw `keys` are refused while a decision is open: answer through `choice_index`.

#### `choice_prompt_cancelled`

The prompt left the screen without an answer from this client (answered at the Mac,
or the assistant moved on). Debounced across two poll ticks so a redraw does not
dismiss a card mid-pinch.

```json
{ "type": "choice_prompt_cancelled", "session_id": "abc-123", "project": "voxherd" }
```

Fire-and-forget — a disconnected client never receives it. Reconcile against
`state_sync.active_choice_sessions` as well.

#### `question_form`

One screen of a multi-question form (`AskUserQuestion`). Re-broadcast as a new screen
each time the user answers, with the same `form_id` and a new `signature`.

```json
{
  "type": "question_form",
  "session_id": "abc-123",
  "project": "voxherd",
  "form_id": "a1b2c3d4",
  "signature": "e5f6a7b8",
  "select_mode": "single",
  "title": "Which database?",
  "body": [],
  "questions": [{ "label": "Database", "answered": false }, { "label": "Auth", "answered": false }],
  "answered_count": 0,
  "total_questions": 2,
  "options": [
    { "text": "Postgres", "kind": "choice", "checked": false },
    { "text": "Type something.", "kind": "free_text", "checked": false }
  ]
}
```

`select_mode` is `single`, `multi` or `review`. Each option's `kind` is `choice`,
`free_text`, `next`, `submit` or `cancel`. Drive it with `terminal_send_keys` +
`form_action` (below).

#### `question_form_cancelled`

```json
{ "type": "question_form_cancelled", "session_id": "abc-123", "project": "voxherd" }
```

Same caveat as `choice_prompt_cancelled`; reconcile against `active_form_sessions`.

---

#### `voice_state`

What the **Mac's** microphone is doing. Broadcast on change; `state_sync` carries the truth.

```json
{ "type": "voice_state", "listening": true, "heard": "hey agent war room", "wake": true, "at": 1758830000.0 }
```

| Field | Meaning |
|---|---|
| `listening` | The wake-word loop is running on the Mac (`--wake-word`). |
| `heard` | Last transcript, **even when it was not a wake phrase**. |
| `wake` | Whether `heard` began with a wake phrase. |
| `at` | Unix time, so a client can tell how stale this is. |

`heard` is deliberately published for unaddressed utterances. Clients without a
microphone of their own cannot otherwise distinguish "nothing is listening" from
"listening and hearing nothing" from "heard you and did not recognise it" — all three
present as silence, and all three have shipped at once.

**This describes the Mac microphone only.** Clients with their own — a headset client
may use its own mic array through Android `SpeechRecognizer` — show their local state
instead and ignore this, since the Mac's microphone says nothing about what a headset
across the room can hear.

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
  "last_summary": "Built the reconnection logic.",
  "assistant": "claude",
  "model": "claude-opus-5-5"
}
```

`model` is the session's model after this event: a stop re-reads it from the transcript first.

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
  "model": "claude-opus-5-5",
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

#### `agent_focus`

An agent edited a file in its project (Follow mode). Sent by the activity poll (~1.5 s)
once for each edit; `focus` has the shape [`GET /api/focus`](#get-apifocus) returns. A client
that starts following between edits asks that endpoint first. Only Claude Code and Codex
sessions send it.

```json
{
  "type": "agent_focus",
  "session_id": "49ace562-…",
  "project": "homeschool",
  "focus": {"path": "src/scheduler/week.ts", "ranges": [[142, 168]], "verb": "edited",
            "id": "8f1c…", "at": "2026-09-28T10:00:00.000Z", "withheld": false}
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
  "lines": ["line 1", "line 2", "..."],
  "mode": "filtered"
}
```

Lines include ANSI escape sequences in **both** modes — `capture-pane` runs
with `-e` and nothing strips them. `mode` echoes back the render mode the
subscription is running in, so the client can confirm what it is drawing.

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
| `reference` | object | no | Code the user pointed at — see [Pointing](#pointing-reference) |

**Resolution order:** `session_id` > `project` + `agent_number` (+ optional `assistant`) > `project` (+ optional `assistant`).

**Rate limit:** 10 commands per session per 60 seconds.

##### Pointing (`reference`)

```json
{
  "type": "voice_command", "session_id": "abc-123", "message": "what does this do?",
  "reference": {"path": "src/scheduler/week.ts", "lines": [142, 168], "symbol": "formatISOWeek"}
}
```

The bridge turns the reference into the assistant's own words before anything is typed,
so a client never needs to know which assistant it is talking to. For Claude Code the
message above becomes:

```
what does this do? @src/scheduler/week.ts (lines 142-168, formatISOWeek)
```

— and Claude Code attaches the file itself. Every other assistant gets the plain path
(`what does this do? src/scheduler/week.ts (lines 142-168, formatISOWeek)`), which each of
them can open: an `@` typed into a terminal UI can open an autocomplete popup whose Enter
picks a file instead of sending the question, and only Claude Code has been watched
getting it right. A path with a space is quoted, never mentioned.

- `path` is required and must be a file [the Reader](#the-reader) would serve, in the
  session's project.
- `lines` is optional, `[first, last]` running forward from 1 (`[n, n]` reads "line n").
- `symbol` is optional; one that is not a name is dropped.
- A reference that fails is refused **whole** with an `error` and nothing is sent —
  "what does this do?" without the "this" is a question about nothing.
- It is expanded before the queue, so a busy session queues the words it will receive,
  and `command_accepted` carries them.

**A select widget on screen is never typed into.** Typing into one does not do what the
sender meant: the text is ignored and the Enter after it confirms the highlighted
option, which on a permission dialog is "Yes". So before typing, the bridge captures
the pane, and if a `tui_select` choice or a form is showing:

- A plain approval (`/approve`, `approve`, `yes`, `yes, proceed`, `go ahead`, …) or
  denial (`/deny`, `deny`, `no`, `no, deny`, `stop`, …) of a **permission** dialog
  commits that option, exactly as a `choice_index` commit would.
- Anything else is **not sent**. The session goes back to `waiting` and every client
  gets an `error` ("voxherd is waiting on a permission prompt — answer it first.
  Nothing was sent.") followed by a `state_sync`.

This applies to every dispatch path: this message, `POST /api/command`, a queued
command, and the Mac voice loop. `number`-mode prompts are an ordinary input line and
are still answered by typing.

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
  "session_id": "abc-123",
  "mode": "ansi",
  "cols": 80,
  "rows": 48
}
```

| Field | Required | Description |
|---|---|---|
| `session_id` | yes | Session to stream |
| `mode` | no | `filtered` (default) or `ansi` |
| `cols` | no | `ansi` only — pane width, 20–500 |
| `rows` | no | `ansi` only — pane height, 5–200 |

**Modes**

| Mode | Used by | Behaviour |
|---|---|---|
| `filtered` | phone mirror, glasses lens | Strips assistant TUI chrome — status footer, tip rows, TodoWrite widget, decorative dividers. On a 22-row lens that chrome is a third of the row budget. |
| `ansi` | headset panels | Ships the pane verbatim. ~48 rows per panel, where the point is that the TUI looks like the TUI. |

An unknown `mode` falls back to `filtered` rather than erroring, so a client
newer than the bridge gets a working terminal rather than a dead one.

Clients on the same session may hold different modes. Re-subscribing with the
same mode is a no-op (it keeps the in-flight snapshot); re-subscribing with a
different mode restarts the poll loop, costing one poll cycle.

**Geometry ownership**

An `ansi` subscriber may send `cols`/`rows` to size the tmux window to its
panel, so the assistant TUI reflows to the width it is actually being read at.

- Only `ansi` clients may do this. `filtered` clients render whatever size they
  are handed and never resize a pane out from under the user's own terminal.
- One lease per session; the first `ansi` client to claim it wins, and a second
  client asking for a different size is ignored rather than fighting over the
  pane.
- Out-of-range or non-integer `cols`/`rows` are dropped and the pane keeps its
  current size — no error.
- The original dimensions **and** the tmux `window-size` window option are put
  back on unsubscribe, disconnect, or session removal. (`resize-window` sets
  that option to `manual` as a side effect; leaving it set would stop the
  window tracking the terminal the user has open.)

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
| `keys` | string | one of | Key sequence to send (max 10,000 chars) |
| `key_sequence` | string[] | one of | Validated tmux key names, sent in one call (max 64) |
| `literal` | bool | no | Applies to `keys` only. If `true`, uses `tmux send-keys -l` (character-by-character, no key name interpretation). Default: `false` (key names like `"Enter"`, `"Up"` are interpreted). |

**`key_sequence`** — for physical keyboards (e.g. a Bluetooth keyboard paired to the glasses).
One message carries a whole run of keystrokes and becomes a single
`tmux send-keys` call:

```json
{
  "type": "terminal_send_keys",
  "session_id": "abc-123",
  "key_sequence": ["C-c", "Enter"]
}
```

Members are validated against tmux's key grammar: an optional run of `C-`/`M-`/
`S-` modifiers followed by either a documented key name (`Enter`, `Escape`,
`Tab`, `BSpace`, `Up`/`Down`/`Left`/`Right`, `Home`, `End`, `PageUp`/`PageDown`,
`F1`–`F12`, `Space`, `DC`, `IC`, …) or a single printable character. Names are
matched case-insensitively.

**One invalid member rejects the whole sequence** with an `error` — partial
delivery of a keystroke run is worse than none. `key_sequence` is always
interpreted; `literal` does not apply to it.

**Delivery is confirmed.** `tmux send-keys` exits non-zero *without raising*
when the pane is gone, so the bridge checks the return code and replies with an
`error` ("Keys not delivered (pane may be gone)") instead of silently dropping
the keystrokes. This applies to the `keys` form too.

**Frame shapes the bridge drops.** A dropped frame is logged on the Mac and
counted, but **no reply is sent** — consistent with the `type` allowlist, and
the reason a client that sends one looks ignored rather than refused. Check
these client-side; the bridge will not tell you:

| Dropped when | Why |
|---|---|
| the frame is not a JSON **object** | `["a"]` or `42` raised on `dict(data)` and tore the connection down, subscriptions and geometry lease with it |
| `session_id`, `tmux_session`, `project` or `task_id` is present and **not a string** | handlers read them with `data.get("<field>", "")` and then slice, lowercase or key a dict with them. The `""` default only covers a **missing** key, so a wrong type arrives intact: `{"session_id": []}` raised `unhashable type: 'list'`, `{"session_id": null}` raised `'NoneType' object is not subscriptable`, `{"tmux_session": 5}` raised `expected string or bytes-like object` |
| `type` is not on the known-types allowlist | deliberate: the string is client-supplied, so it must not be able to mint a stats key |

Note the second row includes **`null`**. Omit the key instead — that is what
the iOS and Android clients do for an absent id, and what every handler's
`""` default is written for.

**Send one or the other, never both.** A message carrying `keys` *and*
`key_sequence` is refused with `error` "Send either 'keys' or 'key_sequence',
not both." They are delivered differently — `literal` describes only `keys`, and
`key_sequence` is always interpreted — so a message with both has no single
meaning, and guessing one let a payload be classified as harmless on the field
the gate below read while being delivered from the other.

**While a decision is open, raw keys that could answer it are refused.** When
the session has a live `choice_prompt` or `question_form`, the bridge classifies
the keys and refuses anything that could confirm a highlighted row:

```json
{ "type": "error", "session_id": "abc-123",
  "message": "A question is open in this session — answer it with choice_index, not raw keys. Escape or Ctrl-C still work." }
```

Still delivered, because none of them can answer: `Escape`, `C-c`, `PageUp`,
`PageDown`, `PPage`, `NPage`, `Home`, `End`, `Up`, `Down`, `Left`, `Right`.
Everything else is refused — including `Tab` and `BTab`. `BTab` looks like
navigation and a key bar naturally offers it as "S-Tab", but Claude Code's
ExitPlanMode prompt is anchored on a footer reading "shift+tab to approve with
this feedback", so there it is a *confirmation*. The classification fails
closed: an unrecognised key name counts as answering.

Answer through `choice_index` (or `form_action`) instead. A client that types the
answer instead of picking it has approved what was highlighted, not what it
meant — `"/deny"` typed into a select widget once approved the command.

**Answering a `choice_prompt`** — a semantic commit. The bridge re-captures the pane,
checks the signature, and sends the keys that widget needs:

```json
{
  "type": "terminal_send_keys",
  "session_id": "abc-123",
  "choice_index": 2,
  "choice_signature": "3f2a9c0d1e4b5a67"
}
```

`choice_index` is 0-based into the prompt's `options`. A stale signature is refused
with `error` "Choice prompt no longer live — commit ignored."

**Driving a `question_form`** — one step per message:

```json
{
  "type": "terminal_send_keys",
  "session_id": "abc-123",
  "form_action": "select",
  "form_id": "a1b2c3d4",
  "title": "Which database?",
  "row_index": 0
}
```

| `form_action` | Extra fields | Effect |
|---|---|---|
| `select` | `row_index` | Single-select: pick the row (advances to the next question) |
| `toggle` | `row_index` | Multi-select: toggle the row's box |
| `advance` | — | Multi-select: go to the next question |
| `note` | `text` | Answer with free text (the "Type something" row) |
| `submit` / `cancel` | — | Review screen: finish or abandon the form |

`row_index` is 0-based into the form's `options`. The bridge gates on `form_id` +
`title`, so a card showing an old screen cannot act on a new one.

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
| `dir` | string | no | Explicit directory (overrides projects.json lookup). Must be under the user's home. |
| `assistant` | string | no | `claude`, `codex`, `gemini`, `grok`, `composer`, `antigravity`. Default: `claude` (or `VOXHERD_DEFAULT_ASSISTANT`) |
| `prompt` | string | no | Initial prompt to send after the assistant initializes (~3s delay) |

Server responds with `spawn_accepted` on success or `error` on failure.

**Notes:**
- Creates a tmux session named `vh-{project}-{hex}`.
- Runs the selected assistant CLI in the project directory.
- Claude/Gemini self-register via hooks; Codex is auto-registered by the bridge.
- **Send `dir` whenever you know it.** Without it the project must be in
  `projects.json`, and on a machine without that file every spawn fails — even into a
  project with agents running right now. A registered session's `project_dir` is the
  directory to send.

---

#### `stop_session`

Gracefully stop a session by sending Ctrl-C to its tmux pane.

To interrupt an agent's current turn and leave it running, prefer Escape via
`terminal_send_keys` `{"key_sequence": ["Escape"]}` — the key every supported TUI
documents for it ("esc to interrupt"). Ctrl-C at an idle Claude Code prompt arms
"press Ctrl-C again to exit", so two stops in a row end the session.

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

**Those restrictions allow any *registered* session**, not only the `vh-` sessions the
bridge spawned — including a terminal the developer opened by hand and is working in.
Confirm before sending, and say which tmux session will end; the headset's confirmation
card also flags one VoxHerd did not start.

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
- The stall check runs after every activity poll; it only announces at `verbose`
  (see `set_verbosity`).
- Sub-agent task-file scanning runs every ~4.5 seconds.
- Sessions with 3 consecutive tmux pane-check failures are automatically deregistered.
