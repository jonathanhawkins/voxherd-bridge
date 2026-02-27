# Voice UX Guide

Voice-first interface for multi-agent supervision. This guide covers interaction patterns, audio cues, intent parsing, and project routing for the VoxHerd system.

---

## Voice Interaction Patterns

VoxHerd supports two modes of voice interaction: **passive monitoring** (agents announce automatically) and **active commands** (user speaks).

### Passive Monitoring

Agents announce completion, permission requests, and errors automatically. Each announcement is preceded by a distinct earcon (audio cue).

| Trigger | Response | Earcon |
|---|---|---|
| Agent completes task | *chime* "Aligned Tools finished. Added pagination and updated the API endpoint." | taskComplete |
| Permission request | *different chime* "QuantCoach is waiting for permission to run database migrations." | permissionRequest |
| Error/failure | *alert chime* "Homeschool encountered an error running tests." | error |
| Bridge connected | *soft chime* | connected |
| Bridge disconnected | *soft chime* | disconnected |

Announcements arrive in real-time as the bridge forwards hook events from Claude Code. The user can immediately react with follow-up commands without waiting for a prompt.

### Active Commands

User speaks a command into glasses or phone. STT converts speech to text, intent parser routes it, and bridge dispatches to the target Claude Code instance.

| Command Pattern | Meaning | Example |
|---|---|---|
| "Status" or "What's running" | List all projects with state | "Status" → "Aligned Tools is active. QuantCoach is idle. Homeschool is waiting for permission." |
| "Tell {project} to {task}" | Explicit project routing | "Tell Aligned Tools to add rate limiting." |
| "{task}" (active project set) | Implicit routing to active project | After "Switch to Homeschool": "Run a security audit." routes to Homeschool. |
| "Switch to {project}" | Set active/default project | "Switch to QuantCoach." |
| "Approve {project}" | Grant permission for pending request | "Approve QuantCoach." |
| "Deny {project}" | Block permission request | "Deny Homeschool." |
| "What did {project} do" | Read last completion summary | "What did Aligned Tools do?" → Reads the latest summary. |
| "Pause {project}" | Send interrupt signal | "Pause QuantCoach." |
| "Resume {project}" | Continue paused session | "Resume QuantCoach." |

---

## Earcon Definitions

Earcons are distinct audio cues that provide immediate feedback without requiring visual attention. They combine system sounds and haptic feedback.

| Earcon | Sound ID | Haptic | Use Case | Meaning |
|---|---|---|---|---|
| **taskComplete** | 1025 | Medium | Agent finishes work | Task done, announcement incoming |
| **permissionRequest** | 1023 | Heavy | Waiting for user approval | Needs decision now |
| **error** | 1073 | Rigid | Something failed | Requires attention |
| **commandSent** | 1004 | Light | Voice command acknowledged | Command received, dispatching |
| **connected** | 1003 | Soft | Bridge or glasses connected | Connection established |
| **disconnected** | 1006 | Soft | Bridge or glasses lost | Connection lost |
| **wakeWordActivated** | 1057 | Light | Wake word detected ("Hey Claude") | Ready to listen |
| **listeningReady** | 1114 | Light | STT engine ready | Speak now |

Haptic feedback reinforces the audio cue when the phone is in hand. When using glasses with open-ear speakers, audio alone is sufficient.

---

## Disambiguation Flow

When no active project is set and the target is ambiguous, the system asks for clarification via TTS.

**Scenario:** User says "Run the tests" but no active project is set and multiple projects exist.

```
User:  "Run the tests"
iOS:   *earcon: listeningReady*
       (speech recognition fires)

Bridge: (project ambiguous, cannot resolve)

iOS:   *earcon: commandSent*
       "Which project? Aligned Tools, QuantCoach, or Homeschool?"
       *waiting for response*

User:  "QuantCoach"

iOS:   *earcon: commandSent*
       "Sending to QuantCoach."

Bridge: (dispatch to QuantCoach: "Run the tests")
```

**Resolution rules (in order):**
1. Explicit name in utterance ("Tell QuantCoach to...") — always wins
2. Active project (set via "Switch to...") — bare commands route here
3. Last announced project — if a project just finished, assume follow-up is for that project
4. Disambiguation — ask user to choose

---

## Conversational Multi-Agent Flow

This is the primary use case. The user walks around with glasses or carries the phone, issues commands, hears completions, and iteratively supervises multiple agents without context-switching.

### Full Walkthrough

```
-- Walking to get coffee, three agents running --

*chime: taskComplete + announcement*
"Aligned Tools finished. Refactored the auth middleware
 and updated 12 test files."

User: "Okay, now I want it to also add rate limiting
       to the auth middleware."

iOS:  *earcon: commandSent*
      "Sending to Aligned Tools."

Bridge: (dispatch via claude --resume to Aligned Tools)

-- 2 minutes later, still walking --

*chime: error + announcement*
"QuantCoach hit a test failure. Three tests in the
 backtesting module are failing with a date parsing error."

User: "Tell it to try fixing the date parsing by using
       ISO format instead of the custom parser."

iOS:  *earcon: commandSent*
      "Sending to QuantCoach."

-- Pivot to a different project --

User: "Switch to Homeschool."

iOS:  *earcon: commandSent*
      "Homeschool is now your active project."

User: "Run a security audit on the student data endpoints."

iOS:  *earcon: commandSent*
      "Sending to Homeschool."

-- Aligned Tools finishes while Homeschool is active --

*chime: taskComplete + announcement*
"Aligned Tools finished. Added rate limiting with
 a 100 requests per minute default."

User: "Tell Aligned Tools to make that configurable
       per endpoint, not a global default."

iOS:  *earcon: commandSent*
      "Sending to Aligned Tools."
      (explicit project name overrides active project)

-- QuantCoach finishes --

*chime: taskComplete + announcement*
"QuantCoach finished. Fixed date parsing in the
 backtesting module. All tests passing."

User: "Nice. Tell QuantCoach to add edge case tests
       for leap years and timezone boundaries."

iOS:  *earcon: commandSent*
      "Sending to QuantCoach."

-- Back to checking on Homeschool --

User: "What's Homeschool doing?"

iOS:  *earcon: listeningReady*
      "Homeschool is still running. Started 3 minutes ago."

-- Homeschool finishes later --

*chime: taskComplete + announcement*
"Homeschool finished. Found two SQL injection risks
 in the student search endpoint. Applied parameterized
 queries and added input validation."

User: "Switch to Homeschool. Now add integration tests
       for those endpoints."

iOS:  *earcon: commandSent*
      "Homeschool is now active. Sending."
      (compound command: switch + task in one utterance)
```

### Key Behaviors

- **React immediately:** Agent finishes → user gives next task without context-switching
- **Iterative guidance:** "Try fixing it like this" — directing approach, not just outcomes
- **Cross-project pivoting:** Explicit "Tell X to..." overrides active project mid-flow
- **Implicit routing:** After "Switch to Y", bare commands go to Y
- **Status checks:** "What's X doing?" while other agents report in
- **Compound commands:** "Switch to X. Now do Y" — parse as two intents, apply in sequence

---

## Intent Parsing

Intent parsing is **two-tier**: fast-path regex first (handles 80% of commands), Haiku NLU fallback for ambiguous cases.

### Tier 1: Fast-Path Regex

Keyword matching on common patterns. Returns immediately without network latency.

| Pattern | Regex | Extracts |
|---|---|---|
| Status | `/(status\|what.*running)/i` | action: "status" |
| Approve | `/approve\s+(\w+)/i` | action: "approve", project: $1 |
| Deny | `/deny\s+(\w+)/i` | action: "deny", project: $1 |
| Switch | `/switch\s+to\s+(\w+)/i` | action: "switch", project: $1 |
| Tell | `/tell\s+(\w+)\s+to\s+(.+)/i` | action: "command", project: $1, message: $2 |
| What did | `/what\s+(?:did\|has)\s+(\w+)\s+(?:do\|done)/i` | action: "summary", project: $1 |
| Pause | `/pause\s+(\w+)/i` | action: "pause", project: $1 |
| Resume | `/resume\s+(\w+)/i` | action: "resume", project: $1 |

Example: "Tell QuantCoach to run the backtesting suite" → `{action: "command", project: "QuantCoach", message: "run the backtesting suite"}`

### Tier 2: Haiku NLU Fallback

If fast-path doesn't match, send transcription + known project list to Haiku. Haiku returns structured JSON.

```
Input to Haiku:
{
  "transcript": "can you check if the login flow still works after the refactor",
  "known_projects": ["Aligned Tools", "QuantCoach", "Homeschool"],
  "active_project": null
}

Haiku response:
{
  "action": "command",
  "project": null,  // ambiguous, ask user
  "message": "can you check if the login flow still works after the refactor"
}
```

Haiku is only called when:
- No regex match found
- User said something conversational ("try fixing it by...") that needs context
- Project name is unclear

This keeps latency low for the common case.

---

## Project Resolution Order

When routing a command, the system resolves the target project in this order:

1. **Explicit project name in utterance**
   - "Tell QuantCoach to..." — always wins, even if Homeschool is active
   - Fast-path regex extracts the project, no ambiguity

2. **Active project (set via "Switch to")**
   - After "Switch to Homeschool", bare commands like "run the tests" route to Homeschool
   - Cleared on app restart or manual switch
   - No longer active once user says "Switch to X" (a new X becomes active)

3. **Last announced project (reactive routing)**
   - QuantCoach finishes → announces completion
   - User immediately says "add edge case tests" without naming the project
   - System infers the target from the most recent announcement
   - Only applies if no active project is set

4. **Disambiguation (ask user)**
   - No explicit name, no active project, no recent announcement
   - TTS asks "Which project?" and lists options
   - User responds with a project name, command is routed

### Resolution Examples

| State | User Says | Target |
|---|---|---|
| Active: Homeschool | "Run the tests" | Homeschool (active project) |
| Active: Homeschool | "Tell Aligned Tools to add rate limiting" | Aligned Tools (explicit name overrides) |
| Active: None, QuantCoach just announced | "Add edge case tests" | QuantCoach (last announced) |
| Active: None, no recent announcement | "Run the tests" | Ask "Which project?" → user responds |
| Active: Homeschool, QuantCoach just announced | "Check the results" | Homeschool (explicit active > implicit last announced) |

---

## Audio Session Management

Audio routing depends on the device and connectivity.

### Phone Only

**Audio Session Category:** `.playAndRecord` with `.defaultToSpeaker`

- Microphone input from iPhone speaker-microphone array
- Speaker output to iPhone built-in speaker
- Used when glasses are not paired
- Example: standing at desk, phone in hand, speaking to iOS

### Glasses (Bluetooth Audio)

**Audio Session Category:** `.voiceChat` (when Bluetooth HFP is active)

- Microphone input from Ray-Ban glasses 5-mic array (via Bluetooth HFP)
- Speaker output to Ray-Ban glasses open-ear speakers (via Bluetooth A2DP)
- Lower latency, hands-free, optimal for mobile use
- Example: walking to get coffee, glasses connected

### Switching Between Modes

The iOS app monitors Bluetooth connection state and switches automatically:

1. Glasses connect → route to `.voiceChat`, glasses mic/speaker
2. Glasses disconnect → fallback to `.playAndRecord`, phone mic/speaker
3. User manually disables glasses in settings → phone mode

The transition is transparent to the user. Audio continues uninterrupted.

### VAD (Voice Activity Detection)

iOS `SFSpeechRecognizer` provides built-in VAD. When the user stops speaking, the recognizer:

1. Waits for 1-2 seconds of silence
2. Returns a final result
3. Recognizer stops listening

No custom VAD implementation needed. The system respects natural pauses in speech and doesn't require explicit "done" signals (like tap-to-talk release).

---

## Permission Request Flow

When Claude Code hits a permission request (notification hook), the flow is:

1. **Bridge receives notification event** from `on-notification.sh`
2. **Bridge forwards to iOS** via WebSocket with event type `"notification"`
3. **iOS plays earcon** `permissionRequest` (heavy haptic + chime)
4. **iOS speaks announcement:** "QuantCoach is waiting for permission to..."
5. **User responds:** "Approve QuantCoach" or "Deny QuantCoach"
6. **iOS sends approval/denial** back to bridge
7. **Bridge forwards** to Claude Code (via `claude --resume` with approval context)

Example:

```
Bridge: {
  "type": "notification",
  "session_id": "quant-123",
  "project": "QuantCoach",
  "message": "Waiting for permission to run database migrations"
}

iOS:   *earcon: permissionRequest*
       "QuantCoach is waiting for permission to run database migrations."

User:  "Approve QuantCoach."

iOS:   *earcon: commandSent*
       "Approved."

Bridge: (dispatch: claude --resume quant-123 -p "The user approved the migration.")
```

---

## Settings & Preferences

Users can customize voice UX via settings:

| Setting | Values | Default | Effect |
|---|---|---|---|
| TTS Voice | System voices (Siri, Alex, etc.) | Siri | Which voice reads announcements |
| Wake Word | "Hey Claude" enable/disable | Disabled | If enabled, glasses/phone listens continuously for "Hey Claude" |
| Earcon Volume | System sound volume | Device volume | Earcons respect system volume setting |
| Earcon Enabled | True/False | True | Toggle all earcons on/off |
| Bridge URL | LAN mDNS, Tailscale IP, Cloudflare | http://macbook.local:7777 | Bridge server address |
| Project Aliases | Custom name maps | None | "Say 'Aligned' instead of 'Aligned Tools'" |

Preferences are stored in `UserDefaults` and persist across app restarts.

---

## Edge Cases & Handling

### Bridge Disconnected

If WebSocket to bridge is lost:

- **Immediately:** TTS announces "Bridge disconnected", earcon `disconnected`
- **Active command:** If user was in the middle of speaking, STT may still finalize. The command is queued. Once bridge reconnects, bridge sends `state_sync` and command is dispatched.
- **Reconnection:** Exponential backoff (1s, 2s, 4s, 8s, max 30s). On reconnect, full state sync sent, iOS updates dashboard.

### Stale Session

If `claude --resume SESSION_ID` fails (session was killed):

- **Bridge detects error** from subprocess
- **Bridge sends error event** to iOS: `{"type": "error", "message": "QuantCoach session not found"}`
- **iOS plays earcon** `error` (rigid haptic + alert chime)
- **iOS speaks:** "QuantCoach session not found. Check the terminal."
- **Dashboard marks** QuantCoach as disconnected/dead

### Ambiguous Project (Fuzzy Match)

If user says "Tell Quant to..." and both "QuantCoach" and "Quantum" exist:

- **Fast-path regex:** Project name is "Quant" (unresolved)
- **Fuzzy matcher:** Levenshtein distance or substring containment
- If closest match is unambiguous (e.g., "Quant" → "QuantCoach" is 1 edit away, "Quantum" is 2) → route to QuantCoach
- If tied (unlikely), escalate to Haiku or ask user "Did you mean QuantCoach or Quantum?"

### Empty or Silent Recording

If user taps mic but says nothing:

- **STT finalizes** after silence timeout (built-in to `SFSpeechRecognizer`)
- **Final result is empty** (no text)
- **iOS ignores** (no intent to parse, no dispatch)
- **Readiness earcon plays** for next attempt: `listeningReady`

### Compound Utterances

"Switch to Homeschool. Now add integration tests."

- **Sentence boundary detection:** Split on ". " or "? " or "! "
- **First sentence:** "Switch to Homeschool" → apply switch, set active project
- **Subsequent sentence:** "Now add integration tests" → parse with new active project context
- **Result:** Both intents executed in sequence, second routed to newly active project

---

## Testing Voice UX

### Manual Testing Flow

1. **Start bridge:** `tmux send-keys -t bridge "source bridge/.venv/bin/activate && python -m bridge run --tts" Enter`
2. **Run iOS app** on device or simulator
3. **Verify STT:** Tap mic button, speak "status" → should hear "Aligned Tools is idle..."
4. **Verify intent parsing:** Speak "Tell Aligned Tools to run the tests" → check bridge dispatch log
5. **Verify earcons:** Each action should play a distinct sound
6. **Verify project routing:** Set active project, speak bare command, verify routing

### Automated Testing

- Test fast-path regex patterns with mock utterances
- Test Haiku fallback with complex utterances
- Test project fuzzy matching with ambiguous names
- Test WebSocket state sync (bridge sends, iOS updates dashboard)
- Mock voice synthesis for earcon timing

---

## Troubleshooting

| Issue | Likely Cause | Fix |
|---|---|---|
| No sound from announcements | TTS disabled or volume muted | Check settings, device volume |
| Command not dispatching | Bridge not running or WebSocket disconnected | Start bridge, check bridge URL in settings |
| Project not recognized | Fuzzy match failed, or project name typo | Check active projects in dashboard, try explicit "Tell X to..." |
| Earcon silent but haptic fires | isEnabled = false in EarconPlayer | Check settings |
| STT not listening | Audio session wrong category, or mic permission denied | Check settings, grant mic permission, verify `.voiceChat` set for glasses |
| Disambiguation loop (keeps asking "Which project?") | Multiple projects with same fuzzy match | Use explicit "Tell X to..." or alias shorter names in settings |

