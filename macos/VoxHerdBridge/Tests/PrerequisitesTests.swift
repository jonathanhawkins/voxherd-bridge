import XCTest
@testable import VoxHerdBridge

/// Unit tests for `Prerequisites`, the onboarding check for tmux and an agent.
///
/// The filesystem is a set of paths handed in as `isExecutable`, so each test
/// states exactly which binaries exist and where.
final class PrerequisitesTests: XCTestCase {

    private let home = "/Users/tester"

    private func report(_ executables: Set<String>) -> Prerequisites.Report {
        Prerequisites.check(home: home, isExecutable: { executables.contains($0) })
    }

    func testHomebrewTmuxAndANativeClaudeInstallAreReady() {
        let r = report(["/opt/homebrew/bin/tmux", "/opt/homebrew/bin/brew", "/Users/tester/.local/bin/claude"])
        XCTAssertEqual(r.tmux, "/opt/homebrew/bin/tmux")
        XCTAssertEqual(r.agents.map(\.command), ["claude"])
        XCTAssertTrue(r.homebrew)
        XCTAssertTrue(r.ready)
    }

    /// The bridge does not search nvm's directories, so a tick here would be a
    /// promise the bridge breaks.
    func testAnAgentOnlyWhereTheBridgeDoesNotLookIsNotFound() {
        let r = report(["/usr/local/bin/tmux", "/Users/tester/.nvm/versions/node/v22.0.0/bin/gemini"])
        XCTAssertEqual(r.agents, [])
        XCTAssertFalse(r.ready)
    }

    func testAnAgentWithoutTmuxIsNotReady() {
        let r = report(["/Users/tester/.claude/local/claude"])
        XCTAssertNil(r.tmux)
        XCTAssertFalse(r.homebrew)
        XCTAssertFalse(r.ready)
    }

    func testAgentsAreReportedInTheirListedOrder() {
        let r = report(["/usr/local/bin/tmux", "/opt/homebrew/bin/grok", "/opt/homebrew/bin/codex", "/opt/homebrew/bin/claude"])
        XCTAssertEqual(r.agents.map(\.name), ["Claude Code", "Codex", "Grok Build"])
    }

    func testTheFirstDirectoryWinsAsTheBridgesPathWould() {
        let r = report(["/usr/local/bin/tmux", "/opt/homebrew/bin/tmux"])
        XCTAssertEqual(r.tmux, "/usr/local/bin/tmux")
    }

    /// `searchDirectories` copies the bridge's own list. If `env_utils.py` gains
    /// a directory, onboarding must look there too, or it reports "none found"
    /// for an agent the bridge would have used.
    func testEveryDirectoryTheBridgeAddsIsSearched() throws {
        let envUtils = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()      // Tests
            .deletingLastPathComponent()      // VoxHerdBridge
            .deletingLastPathComponent()      // macos
            .deletingLastPathComponent()      // repo root
            .appendingPathComponent("bridge/env_utils.py")
        let source = try String(contentsOf: envUtils, encoding: .utf8)
        // The non-Windows list: everything after `else:` up to its closing bracket.
        let unix = try XCTUnwrap(source.range(of: "else:\n").map { source[$0.upperBound...] })
        let list = try XCTUnwrap(unix.range(of: "\n    ]").map { unix[..<$0.lowerBound] })

        var bridgeDirs: [String] = []
        let quoted = try NSRegularExpression(pattern: #""([^"]+)""#)
        let text = String(list)
        for match in quoted.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            guard let range = Range(match.range(at: 1), in: text) else { continue }
            let entry = String(text[range])
            if entry == "/snap/bin" { continue }  // Linux only
            bridgeDirs.append(entry.replacingOccurrences(of: "~", with: home))
        }

        XCTAssertGreaterThan(bridgeDirs.count, 10, "parsed too little of env_utils.py: \(bridgeDirs)")
        let searched = Set(Prerequisites.searchDirectories(home: home))
        for dir in bridgeDirs {
            XCTAssertTrue(searched.contains(dir), "the bridge searches \(dir); onboarding does not")
        }
    }
}
