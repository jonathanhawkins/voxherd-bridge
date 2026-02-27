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
