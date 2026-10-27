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
| **wakeWordActivated** | 1057 | Light | Wake word detected ("Hey agent") | Ready to listen |
| **listeningReady** | 1114 | Light | STT engine ready | Speak now |

Haptic feedback reinforces the audio cue when the phone is in hand. When using glasses with open-ear speakers, audio alone is sufficient.

### The identity axis (Earshot, headset clients)

The earcons above say *what* happened. Headset clients add a second, orthogonal axis:
*who*. Each project has a two-note motif, played from that
session's side just before it speaks, so its words need not begin "Homeschool says…".

| Rule | Why |
|---|---|
| Two notes from C-major pentatonic, C5–A6 | Any pair is consonant; no motif sounds like an alarm |
| Chosen by FNV-1a of the lower-cased project name | The same project sounds the same tomorrow; pinned bit for bit so every client matches |
| 110 ms, 30 ms gap, 110 ms: 250 ms before speech | A motif is a lead-in, not a jingle |
| Unique within the herd | 90 motifs collide often (`homeschool` and `quantcoach` share E5→D6); a newcomer takes its next variant, whoever was first keeps theirs |
| Who, then what | For a question: the motif, then the knock, then the words |
| The Anchor has none | It speaks from the front for several agents at once, and says names |

### What the Anchor says (Earshot, headset clients)

From four agents, or when two finish within the gather window, one front-centre voice
speaks for all of them:

> *"Eight came back. voxherd: refactored the auth middleware. android: hit a merge
> conflict. Six more finished. quantcoach is waiting on you. Two others are still
> running."*

| Rule | Why |
|---|---|
| The count is the true total | Eight did come back; the wearer is told how many, then told about some of them |
| Summaries until the word budget runs out, then counted | Reading all eight was 107 words — over forty seconds of uninterrupted speech. A budget rather than a count adapts: three terse agents fit where two verbose ones do not |
| Never more than four voices in one breath | A swarm saying "done." apiece sails under the budget and is still a list nobody can follow |
| A name costs what it is *said* as | `spoken()` turns every `-` into a space, so "kora-app-commerce" is three words; charging one let a digest that accounted for 24 words say 35 |
| Agents waiting are capped like agents running | Two are named, the rest counted. It was the one clause here with no cap, and it is the loud one |
| Never fewer than one, and never half a summary | A summary is one or two sentences that mean something together; half of one is worse than a count |
| What is left out is recoverable | "What's quantcoach doing?" reads the summary back, so counting loses nothing |
| An ordinal counts only along the names actually said | "The fifth one" must not reach a session the wearer was never told about |
| Who is waiting, then who is running | Only on a real digest: tacked onto every single finish it would be the noise Earshot exists to prevent |

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

## Eyes-free verbs (Earshot, headset clients)

With the display dormant the wearer cannot point, so the headset takes a small set of
verbs, each after the wake phrase like any command:

| Say | Hear |
|---|---|
| "who's waiting?" | "Two are waiting: android and quantcoach." Oldest question first |
| "next" | The front question, read whole: a permission as "android needs you: Bash command: npm test. Approve or deny?", a choice with its options numbered as the card numbers them. Said again, the one after it — and the card moves with it, so "approve" answers what was just read |
| "what's everyone doing?", "status" | A clause each, up to four agents. Past that, who needs you by name and the rest counted |
| "what's android doing?" | "android has been running tests for 3 minutes." |
| "read me the last three lines of android" | The pane's last lines — at most the eight the bridge keeps — without box-drawing, bullets or spinner frames |
| "mute android", "unmute it" | Its words go, not its news: a finish or a failure is a chime, a question is its motif then the knock. The Rail marks it "muted" |
| "switch to android" | Into focus. "Switch to war room" is the Deck's, "switch to dark" the theme's |
| "tell the stuck one to …", "ask it why …" | Sent in the wearer's own words, and "Sending to quantcoach." |
| "say that again" | The last thing said, from where it was said, with its lead-in |

**Who "it" is.** The session the wearer last named or acted on, then the one last heard,
then the one in focus — the first two only for a minute. "The second one" counts along
the last list heard (for five minutes), else the Rail. "The stuck one" is the one whose
last turn failed. Two candidates is a question back — "Which one? android or voxherd?" —
never a guess, and "everyone" is "One at a time." A message said with nothing in focus
goes to whoever just spoke, and says so. "Kill it" and "stop" still mean the session in
focus, an action on what is in front of you, and the kill card is said out loud.

**Answers are not narration.** They skip the floor the wearer is holding and the silence
budget, and when one ends the floor is the wearer's for four seconds, so a report does not
start talking over their reply. They are spoken only while the headset holds the speech
floor; otherwise they appear on screen. An error about what the wearer just sent is said
too: with the display dormant, a refusal nobody hears was never given.

A verb that names something the headset does not know is not a verb: "tell the user to
wait" and "read me the last line of the log" are instructions, and reach an agent.

---

## Touchpad (audio glasses)

The temple pad is the only input that is not a voice on a pair of audio glasses, so it
gets four gestures and no more — a vocabulary that grows collides with the platform's own
reserved set.

| Gesture | Does |
|---|---|
| Tap | Reads the front question, or the next report — the same as saying "next" |
| Double tap | "Say that again" |
| Swipe forward / back | The next or previous agent in the herd's order, wrapping. With nothing in focus, either way lands on the first |
| Hold | Push-to-talk: the microphone is open only while the pad is held |

**A tap never answers a question.** An acknowledging tap would be a way to approve an
agent's permission request by brushing your own glasses. The rule is the same one that
keeps a voice from answering without the wake phrase — nobody nearby should be able to
approve a tool by saying "yes" — and a pad is easier to brush than a word is to say.

**Each gesture becomes the verb the wearer could have said**, so routing has one path:
there is no second answer to "which session did that mean".

**A gesture with nowhere to go says so** — "No agents yet", "android is the only one".
Silence is what a broken pad looks like, and there is no screen to say otherwise.

---

## Reader verbs (display clients)

Tried **before** anything else while the Reader shows a file, because that is what the
wearer is looking at. Only a few are heard at all in Review and
Follow mode, where they mean the review's changes instead.

| Say | Does |
|---|---|
| "open week dot ts", "read week.ts in android" | Opens it. Only something that *names* a file — an extension, a path, or the word "file" — so "open the settings and add a flag" still reaches the agent |
| "pin this", "unpin", "let go" | Pins what the hand is over, or lets go. A pin goes with whatever is said next |
| "next function", "previous method" | The next or previous definition, pinned and scrolled to |
| "go to weekdays" | A definition by name. Only a name the file has: "go to the airport" is still a message |
| "next page", "previous page", "page down" | A page of 4,000 lines, flush — no line is read twice at the seam |
| "top of the file", "end of the file" | The top or the bottom. On a file that fits in one page — almost every file — they scroll within it rather than answering "that's the top" at someone looking at line 200 |
| "go to line 240", "line 1500" | That line, paging to it when it is past the page on screen. A line is not a region, so it carries nothing: what is said next is not about line 240 merely because the wearer looked at it |
| "what changed in android", "show me the diff" | Review mode. Then "next change", "keep this", "undo this", "refresh" |
| "follow android", "what's android editing?" | Follow mode. Then "catch up", "stop following" |
| "close the file", "done reading" | Puts it away |

**Deliberately spelled out.** A bare "top", "end", "more" or "keep going" is far likelier
to be a message for the agent — "keep going" especially — and these verbs are matched
first, so a loose set would swallow it.

**Whether there is anywhere to go is answered, not ignored**: "next page" on the last page
says *"That's the end of the file."* A verb that silently did nothing is indistinguishable
from one that was never heard.

**Keyboard (Bluetooth, headset clients).** Shift + the navigation keys, while a file is open:
Shift+PageUp/PageDown a page, Shift+Home/End the ends, Shift+↑/↓ the previous and next
definition. Movement only — Enter, Space and Escape stay the terminal's, because each is a
keystroke the wearer meant for the prompt. Nothing unshifted is ever claimed: the Reader
sits *beside* the terminal, not over it.

A pane cannot tell Shift+PageDown from PageDown — it receives `NPage` either way — so no
terminal capability is lost, but a wearer who habitually holds Shift will find that key
going to the Reader and must let go. A held key is consumed on every repeat and acted on
once: acting on each would fire a 4,000-line request per repeat event.

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
| Wake Word | "Hey agent" enable/disable | Disabled | If enabled, glasses/phone listens continuously for "Hey agent" ("Hey Claude" also works) |
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

