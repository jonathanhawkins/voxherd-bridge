import SwiftUI

// MARK: - SessionInfo

/// A Claude Code session tracked by the bridge server.
/// Populated from the GET /api/sessions REST endpoint.
struct SessionInfo: Identifiable, Codable {
    var id: String { sessionId }

    let sessionId: String
    let project: String
    let status: String
    let activityType: String
    let lastSummary: String
    let agentNumber: Int
    let subAgentCount: Int
    let lastActivity: String

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case project
        case status
        case activityType = "activity_type"
        case lastSummary = "last_summary"
        case agentNumber = "agent_number"
        case subAgentCount = "sub_agent_count"
        case lastActivity = "last_activity"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try container.decode(String.self, forKey: .sessionId)
        project = try container.decodeIfPresent(String.self, forKey: .project) ?? "unknown"
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "active"
        activityType = try container.decodeIfPresent(String.self, forKey: .activityType) ?? "registered"
        lastSummary = try container.decodeIfPresent(String.self, forKey: .lastSummary) ?? ""
        agentNumber = try container.decodeIfPresent(Int.self, forKey: .agentNumber) ?? 1
        subAgentCount = try container.decodeIfPresent(Int.self, forKey: .subAgentCount) ?? 0
        lastActivity = try container.decodeIfPresent(String.self, forKey: .lastActivity) ?? ""
    }

    /// SF Symbol name for the current activity type.
    var activityIcon: String {
        switch activityType {
        case "thinking": return "brain"
        case "writing": return "pencil.line"
        case "searching": return "magnifyingglass"
        case "running": return "play.fill"
        case "building": return "hammer.fill"
        case "testing": return "checkmark.shield"
        case "working": return "gearshape"
        case "completed": return "checkmark.circle.fill"
        case "sleeping": return "moon.fill"
        case "errored": return "exclamationmark.triangle.fill"
        case "stopped": return "stop.circle.fill"
        case "approval": return "hand.raised.fill"
        case "input": return "questionmark.circle.fill"
        case "registered": return "bolt.fill"
        case "dead": return "xmark.circle.fill"
        default: return "circle.fill"
        }
    }

    /// Color representing the session's current state.
    var statusColor: Color {
        switch activityType {
        case "thinking", "writing", "searching", "working":
            return .blue
        case "running", "building", "testing":
            return .orange
        case "completed":
            return .green
        case "sleeping", "stopped":
            return .secondary
        case "errored", "dead":
            return .red
        case "approval", "input":
            return .yellow
        case "registered":
            return .cyan
        default:
            return .secondary
        }
    }

    /// Human-readable activity label.
    var activityLabel: String {
        switch activityType {
        case "thinking": return "Thinking"
        case "writing": return "Writing"
        case "searching": return "Searching"
        case "running": return "Running"
        case "building": return "Building"
        case "testing": return "Testing"
        case "working": return "Working"
        case "completed": return "Completed"
        case "sleeping": return "Idle"
        case "errored": return "Error"
        case "stopped": return "Stopped"
        case "approval": return "Needs Approval"
        case "input": return "Needs Input"
        case "registered": return "Starting"
        case "dead": return "Dead"
        default: return activityType.capitalized
        }
    }

    /// Whether this session is actively doing work.
    var isActive: Bool {
        switch activityType {
        case "thinking", "writing", "searching", "running",
             "building", "testing", "working", "registered":
            return true
        default:
            return false
        }
    }

    /// Whether this session needs user attention.
    var needsAttention: Bool {
        activityType == "approval" || activityType == "input"
    }
}

// MARK: - Pairing

/// One `{"pairing": {...}}` line from the headless bridge's stdout.
///
/// `bridge/pairing.py` writes one on every change to its book of pending
/// requests: `pending` when a headset asks (and again when a wrong code is
/// tried against it), `resolved` when the code is redeemed, expires, runs out
/// of tries, or a flood locks pairing.
///
/// Every field but `state` and `requestId` is optional on purpose. This line
/// crosses a version boundary — the bundled bridge binary is rebuilt
/// separately from this app — and a card dropped whole because one key was
/// renamed is the failure this feature exists to prevent.
struct PairingLine: Decodable {
    let pairing: Payload

    struct Payload: Decodable {
        let state: String
        let requestId: String
        let code: String?
        let device: String?
        let host: String?
        let expiresIn: Double?
        let attempts: Int?
        let reason: String?

        enum CodingKeys: String, CodingKey {
            case state
            case requestId = "request_id"
            case code
            case device
            case host
            case expiresIn = "expires_in"
            case attempts
            case reason
        }
    }
}

/// A pairing request the Mac is showing, for as long as its code works.
struct PairingPrompt: Identifiable, Equatable {
    let requestId: String
    /// Already grouped for reading off a screen — "471 902". The bridge's
    /// `spaced()` decides where the gap goes, so this app never has a second
    /// opinion about it.
    let code: String
    let device: String
    let host: String
    /// When this request first appeared. Kept across a re-announcement so a
    /// wrong code does not look like a fresh two minutes.
    let issuedAt: Date
    let expiresAt: Date
    /// Wrong codes tried against this request. A wearer who has typed nothing
    /// and sees this above zero is watching someone else guess.
    let attempts: Int

    var id: String { requestId }

    /// How long this code was ever good for — the progress bar's full width.
    var lifetime: TimeInterval {
        max(1, expiresAt.timeIntervalSince(issuedAt))
    }

    func secondsRemaining(at now: Date) -> Int {
        max(0, Int(expiresAt.timeIntervalSince(now).rounded(.up)))
    }

    func hasExpired(at now: Date) -> Bool {
        now >= expiresAt
    }

    /// "1:47".
    func countdown(at now: Date) -> String {
        let left = secondsRemaining(at: now)
        return String(format: "%d:%02d", left / 60, left % 60)
    }
}

/// What the menu bar shows for pairing, decided in one place.
///
/// Pure on purpose. The process manager's whole job is to hand this a decoded
/// line and the current time, because everything that can go wrong with a
/// pairing card goes wrong silently: a stale code sits on screen and the
/// wearer keys it in, an unsanitised device name is a stranger's text in the
/// user's menu bar, and a line this app does not understand must leave what
/// is already on screen alone rather than replace it with nothing.
enum PairingPrompts {
    /// `pairing.MAX_PENDING`. The bridge will not have more waiting than
    /// this, so a run of `pending` lines beyond it is a bug or a forgery —
    /// either way the menu bar is not the place to grow without limit. The
    /// most recently announced are the ones kept: a wearer who just asked is
    /// looking for their own code.
    static let maxShown = 3

    /// Mirrors `pairing.MAX_DEVICE_CHARS` and `pairing.DEFAULT_DEVICE`.
    static let maxDeviceChars = 40
    static let unknownDevice = "a headset"
    /// Long enough for IPv6 with a zone index, short enough to render.
    static let maxHostChars = 45
    static let unknownHost = "an unknown address"
    /// Six digits and a space, with room to spare if the code ever grows.
    static let maxCodeChars = 16

    /// `prompts` after `payload`, which may add, replace or remove one.
    static func apply(
        _ payload: PairingLine.Payload,
        to prompts: [PairingPrompt],
        now: Date
    ) -> [PairingPrompt] {
        switch payload.state {
        case "resolved":
            // Redeemed, expired, out of tries, or locked out — the code no
            // longer works, so it comes off the screen whichever it was.
            return prompts.filter { $0.requestId != payload.requestId }

        case "pending":
            // A card with no readable code, or one that is already dead, is
            // worse than no card: it is a thing the wearer will try to use.
            guard let code = readableCode(payload.code),
                  let expiresIn = payload.expiresIn, expiresIn > 0 else {
                return prompts
            }
            let existing = prompts.first { $0.requestId == payload.requestId }
            let prompt = PairingPrompt(
                requestId: payload.requestId,
                code: code,
                device: readableDevice(payload.device),
                host: readableHost(payload.host),
                issuedAt: existing?.issuedAt ?? now,
                expiresAt: now.addingTimeInterval(expiresIn),
                attempts: max(0, payload.attempts ?? 0)
            )
            var updated = prompts
            if let index = updated.firstIndex(where: { $0.requestId == payload.requestId }) {
                updated[index] = prompt
            } else {
                updated.append(prompt)
            }
            return Array(updated.suffix(maxShown))

        default:
            // A state this build does not know. Leave the screen as it is:
            // guessing is how a card goes stale.
            return prompts
        }
    }

    /// `prompts` with the dead ones gone.
    ///
    /// The bridge cannot be relied on to say so. Nothing in `pairing.py` runs
    /// on a timer — it sweeps its book only when another pairing call arrives
    /// — so a request nobody follows up on produces no `resolved` line at
    /// all, and a card waiting to be told would outlive its code.
    static func pruned(_ prompts: [PairingPrompt], now: Date) -> [PairingPrompt] {
        prompts.filter { !$0.hasExpired(at: now) }
    }

    // MARK: Untrusted text

    /// `raw` if it reads as a code: digits and single spaces, nothing else.
    ///
    /// Returns nil rather than a cleaned-up remnant. A code is the one field
    /// here that has to be exactly right, so a line carrying anything but
    /// digits is a line to distrust, not to repair.
    static func readableCode(_ raw: String?) -> String? {
        guard let raw, raw.count <= maxCodeChars else { return nil }
        guard raw.allSatisfy({ ($0.isASCII && $0.isNumber) || $0 == " " }),
              raw.contains(where: { $0.isASCII && $0.isNumber }),
              !raw.hasPrefix(" "), !raw.hasSuffix(" ") else { return nil }
        return raw
    }

    /// `raw` cut the way `pairing.device_name` cuts it.
    ///
    /// The bridge already does this, and does it again here because the name
    /// is a stranger's string either way and this is the process that renders
    /// it. SwiftUI `Text` draws it as text — it is never a format string and
    /// never reaches a shell — but a name of 400 combining characters would
    /// still push the code off its own card.
    static func readableDevice(_ raw: String?) -> String {
        guard let raw else { return unknownDevice }
        let kept = raw.filter {
            ($0.isASCII && ($0.isLetter || $0.isNumber))
                || $0 == " " || $0 == "." || $0 == "_" || $0 == "-"
        }
        let collapsed = kept.split(separator: " ", omittingEmptySubsequences: true)
            .joined(separator: " ")
        let cut = String(collapsed.prefix(maxDeviceChars))
            .trimmingCharacters(in: .whitespaces)
        return cut.isEmpty ? unknownDevice : cut
    }

    /// `raw` if it reads as an address: what IPv4, IPv6 and hostnames are made of.
    ///
    /// This is the field the wearer compares against what their own headset
    /// says, which is the whole reason it is on the card, so it must not be
    /// something a device name could be mistaken for.
    static func readableHost(_ raw: String?) -> String {
        guard let raw else { return unknownHost }
        let kept = raw.filter {
            ($0.isASCII && ($0.isLetter || $0.isNumber))
                || $0 == "." || $0 == ":" || $0 == "-" || $0 == "%" || $0 == "_"
        }
        let cut = String(kept.prefix(maxHostChars))
        return cut.isEmpty ? unknownHost : cut
    }
}

// MARK: - LogEntry

struct LogEntry: Identifiable, Codable {
    let id: UUID
    let timestamp: String
    let level: String
    let project: String
    let message: String

    init(timestamp: String, level: String, project: String, message: String) {
        self.id = UUID()
        self.timestamp = timestamp
        self.level = level
        self.project = project
        self.message = message
    }

    var levelColor: Color {
        switch level {
        case "success": return .green
        case "warning": return .yellow
        case "error": return .red
        case "info": return .cyan
        default: return .primary
        }
    }

    enum CodingKeys: String, CodingKey {
        case timestamp = "ts"
        case level, project, message
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.id = UUID()
        self.timestamp = try container.decode(String.self, forKey: .timestamp)
        self.level = try container.decode(String.self, forKey: .level)
        self.project = try container.decode(String.self, forKey: .project)
        self.message = try container.decode(String.self, forKey: .message)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(timestamp, forKey: .timestamp)
        try container.encode(level, forKey: .level)
        try container.encode(project, forKey: .project)
        try container.encode(message, forKey: .message)
    }
}
