import XCTest
@testable import VoxHerdBridge

/// Unit tests for `BridgeProcessManager`'s session bucket-sorting logic.
/// Mirrors `SessionRegistryRecencyOrderTests` on iOS so the two UIs are
/// guaranteed to render the same row order.
///
/// The pure logic under test is `attentionBucket(for:now:)` and
/// `lastActivityDate(_:)`. The `fetchSessions` REST path that ties them
/// together is not covered here — that requires a running bridge and is
/// smoke-tested by launching the app.
final class SessionSortTests: XCTestCase {

    // MARK: - Helpers

    /// Build a SessionInfo via JSON decoding so we don't have to expose a
    /// memberwise init for tests. Matches the shape `/api/sessions`
    /// actually returns.
    private static func session(
        id: String = "sid",
        project: String = "proj",
        status: String = "idle",
        ageSeconds: TimeInterval,
        activityType: String = "sleeping"
    ) -> SessionInfo {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let timestamp = f.string(from: Date(timeIntervalSinceNow: -ageSeconds))
        let json: [String: Any] = [
            "session_id": id,
            "project": project,
            "status": status,
            "activity_type": activityType,
            "last_summary": "",
            "agent_number": 1,
            "sub_agent_count": 0,
            "last_activity": timestamp,
        ]
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(SessionInfo.self, from: data)
    }

    // MARK: - attentionBucket

    func test_activeSession_isBucketOne() {
        let s = Self.session(status: "active", ageSeconds: 30)
        XCTAssertEqual(BridgeProcessManager.attentionBucket(for: s, now: Date()), 1)
    }

    func test_waitingSession_isBucketOne() {
        // Waiting lives in the same bucket as active. A stale waiting
        // session does NOT pin itself at the top; user already saw it.
        let s = Self.session(status: "waiting", ageSeconds: 30)
        XCTAssertEqual(BridgeProcessManager.attentionBucket(for: s, now: Date()), 1)
    }

    func test_idleSession_within2min_isBucketZero() {
        // Just-finished — the row the user wants to see at the top so
        // they can react to whatever just got announced.
        let s = Self.session(status: "idle", ageSeconds: 30)
        XCTAssertEqual(BridgeProcessManager.attentionBucket(for: s, now: Date()), 0)
    }

    func test_idleSession_atExactly2min_isBucketTwo() {
        // 120s is the boundary — strict less-than, so this falls into
        // the "older idle" bucket. Pinning the boundary so a future
        // refactor doesn't accidentally flip it to <=.
        //
        // Uses a fixed reference Date so the round-trip through ISO 8601
        // (which rounds to milliseconds) produces an exact 120.0s delta.
        // A `Date()`-based test would be flaky here because formatter
        // precision drift can push the delta to 119.9997s.
        let reference = Date(timeIntervalSince1970: 1_700_000_000)
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let timestamp = f.string(from: reference.addingTimeInterval(-120))
        let json: [String: Any] = [
            "session_id": "sid", "project": "p", "status": "idle",
            "activity_type": "sleeping", "last_summary": "",
            "agent_number": 1, "sub_agent_count": 0,
            "last_activity": timestamp,
        ]
        let data = try! JSONSerialization.data(withJSONObject: json)
        let s = try! JSONDecoder().decode(SessionInfo.self, from: data)
        XCTAssertEqual(BridgeProcessManager.attentionBucket(for: s, now: reference), 2)
    }

    func test_idleSession_past2min_isBucketTwo() {
        let s = Self.session(status: "idle", ageSeconds: 300)
        XCTAssertEqual(BridgeProcessManager.attentionBucket(for: s, now: Date()), 2)
    }

    // MARK: - lastActivityDate parsing

    func test_lastActivityDate_parsesFractionalSeconds() {
        // The bridge writes `datetime.now(timezone.utc).isoformat()` which
        // always includes microseconds; the primary formatter must handle
        // the fractional-second form natively.
        let s = Self.session(ageSeconds: 60)
        XCTAssertNotNil(BridgeProcessManager.lastActivityDate(s))
    }

    func test_lastActivityDate_emptyString_returnsNil() {
        let json: [String: Any] = [
            "session_id": "sid", "project": "p", "status": "idle",
            "activity_type": "sleeping", "last_summary": "",
            "agent_number": 1, "sub_agent_count": 0,
            "last_activity": "",
        ]
        let data = try! JSONSerialization.data(withJSONObject: json)
        let s = try! JSONDecoder().decode(SessionInfo.self, from: data)
        XCTAssertNil(BridgeProcessManager.lastActivityDate(s))
    }

    // MARK: - End-to-end: drive the full sort

    /// Replicates the comparator in `fetchSessions()` so we test the
    /// exact ordering rule the UI sees. If the comparator changes, this
    /// test should be updated to match — the helpers above pin the
    /// individual bucket math, this one pins the row-order outcome.
    private static func sort(_ ss: [SessionInfo], now: Date = Date()) -> [SessionInfo] {
        ss.sorted { a, b in
            let ba = BridgeProcessManager.attentionBucket(for: a, now: now)
            let bb = BridgeProcessManager.attentionBucket(for: b, now: now)
            if ba != bb { return ba < bb }
            let ta = BridgeProcessManager.lastActivityDate(a) ?? .distantPast
            let tb = BridgeProcessManager.lastActivityDate(b) ?? .distantPast
            if ta != tb { return ta > tb }
            return a.project.localizedCaseInsensitiveCompare(b.project) == .orderedAscending
        }
    }

    func test_sort_justFinishedAboveWorkingAboveStaleIdle() {
        let sessions = [
            Self.session(id: "stale-idle", project: "a", status: "idle",   ageSeconds: 300),
            Self.session(id: "working",    project: "b", status: "active", ageSeconds: 30),
            Self.session(id: "just-done",  project: "c", status: "idle",   ageSeconds: 60),
        ]
        let ordered = Self.sort(sessions).map(\.sessionId)
        XCTAssertEqual(ordered, ["just-done", "working", "stale-idle"],
                       "Three-bucket attention sort: idle<2min → top, working → middle, stale idle → bottom. Matches the user-requested 2026-05-20 dashboard ordering.")
    }

    func test_sort_userScreenshotScenario() {
        // The scenario from the user's macOS screenshot: several aligned-tools
        // and hack-day sessions, one "working", several "idle". With the new
        // sort, working should outrank stale idle even though stale idle had
        // a more recent lastActivity bump in some cases.
        let sessions = [
            Self.session(id: "aligned-13", project: "aligned-tools", status: "idle",   ageSeconds: 400),
            Self.session(id: "aligned-15", project: "aligned-tools", status: "idle",   ageSeconds: 450),
            Self.session(id: "aligned-8",  project: "aligned-tools", status: "idle",   ageSeconds: 2800),
            Self.session(id: "hack-15",    project: "hack-day",      status: "active", ageSeconds: 1700),
            Self.session(id: "hack-9",     project: "hack-day",      status: "idle",   ageSeconds: 950),
            Self.session(id: "voxherd-13", project: "voxherd",       status: "idle",   ageSeconds: 30),  // just finished
        ]
        let ordered = Self.sort(sessions).map(\.sessionId)
        XCTAssertEqual(
            ordered.first, "voxherd-13",
            "Just-finished (idle, 30s) wins the top — that's the row the user wants to react to."
        )
        XCTAssertEqual(
            ordered[1], "hack-15",
            "Working (active, 28 min ago) ranks second — ahead of all the older idle sessions, even though they have fresher lastActivity bumps."
        )
        // Within the older-idle bucket, recency desc.
        XCTAssertEqual(
            Array(ordered.dropFirst(2)),
            ["aligned-13", "aligned-15", "hack-9", "aligned-8"],
            "Older-idle bucket sorted by lastActivity descending."
        )
    }

    func test_sort_workingBeatsStaleWaiting() {
        // 2026-05-20 regression: a waiting session that's been waiting a
        // long time should NOT pin itself above newer working activity.
        // Both share bucket 1, so recency wins within the bucket.
        let sessions = [
            Self.session(id: "stale-wait", project: "a", status: "waiting", ageSeconds: 600),
            Self.session(id: "fresh-work", project: "b", status: "active",  ageSeconds: 30),
        ]
        let ordered = Self.sort(sessions).map(\.sessionId)
        XCTAssertEqual(ordered, ["fresh-work", "stale-wait"],
                       "Inside bucket 1, recency wins: a stale waiting session does not stay pinned above fresh active work.")
    }
}
