import XCTest
@testable import VoxHerdBridge

/// Unit tests for the pure-logic parts of `PowerManagement`. The actual
/// `pmset` / `osascript` invocations are not covered here — they require a
/// real shell and (for setDisableSleep) admin auth. Those are smoke-tested by
/// running the app and checking `pmset -g | grep disablesleep` manually.
final class PowerManagementTests: XCTestCase {

    // MARK: - parseDisableSleep

    func test_parse_emptyInput_returnsFalse() {
        XCTAssertFalse(PowerManagement.parseDisableSleep(""))
    }

    func test_parse_keyAbsent_returnsFalse() {
        let output = """
        System-wide power settings:
         SleepDisabled       0
        Currently in use:
         sleep                15
         standby              1
        """
        XCTAssertFalse(PowerManagement.parseDisableSleep(output))
    }

    func test_parse_realWorld_pmsetOutput_enabled() {
        // Mirrors actual `pmset -g` output (multi-space separators).
        let output = """
        System-wide power settings:
         SleepDisabled       0
        Currently in use:
         standby              1
         Sleep On Power Button 1
         disablesleep         1
         hibernatemode        0
         sleep                15
        """
        XCTAssertTrue(PowerManagement.parseDisableSleep(output))
    }

    func test_parse_realWorld_pmsetOutput_disabled() {
        let output = """
        System-wide power settings:
         SleepDisabled       0
        Currently in use:
         disablesleep         0
         hibernatemode        0
        """
        XCTAssertFalse(PowerManagement.parseDisableSleep(output))
    }

    func test_parse_tabSeparators_enabled() {
        let output = "\tdisablesleep\t\t1"
        XCTAssertTrue(PowerManagement.parseDisableSleep(output))
    }

    func test_parse_singleLine_minimalEnabled() {
        XCTAssertTrue(PowerManagement.parseDisableSleep("disablesleep 1"))
    }

    func test_parse_singleLine_minimalDisabled() {
        XCTAssertFalse(PowerManagement.parseDisableSleep("disablesleep 0"))
    }

    func test_parse_leadingWhitespace_enabled() {
        XCTAssertTrue(PowerManagement.parseDisableSleep("     disablesleep   1"))
    }

    func test_parse_multipleLines_firstMatchWins() {
        // pmset never emits multiple disablesleep lines, but if it ever did,
        // we read the first match. Document that here.
        let output = """
        disablesleep   1
        disablesleep   0
        """
        XCTAssertTrue(PowerManagement.parseDisableSleep(output))
    }

    func test_parse_keyAsLeadingSubstring_doesNotFalseMatch() {
        // A line beginning with another token that contains "disablesleep" as
        // a leading substring must not match.
        let output = " mydisablesleep    1\nstandby 0"
        XCTAssertFalse(PowerManagement.parseDisableSleep(output))
    }

    func test_parse_keyAsTrailingSubstring_doesNotFalseMatch() {
        // If pmset ever adds a key like `disablesleeppolicy`, our prefix-check
        // must not collapse it onto the `disablesleep` key. The whitespace
        // boundary guard handles this.
        let output = " disablesleeppolicy   1\n disablesleep   0"
        XCTAssertFalse(PowerManagement.parseDisableSleep(output))
    }

    func test_parse_keyWithSingleTrailingChar_doesNotFalseMatch() {
        // Single-character extension (`disablesleepy`) must also fail the
        // whitespace-boundary check.
        XCTAssertFalse(PowerManagement.parseDisableSleep("disablesleepy 1"))
    }

    func test_parse_keyOnlyNoValue_returnsFalse() {
        // Malformed input — key present, no value, no trailing whitespace.
        // Defaults to "disabled" rather than crashing on the missing suffix.
        XCTAssertFalse(PowerManagement.parseDisableSleep("disablesleep"))
    }

    func test_parse_keyPlusTrailingSpaceNoValue_returnsFalse() {
        // Another malformed case — key + whitespace but no digit. Suffix is
        // empty after trim, so hasPrefix("1") returns false.
        XCTAssertFalse(PowerManagement.parseDisableSleep("disablesleep "))
    }

    // MARK: - readDisableSleepState (integration smoke)

    /// Runs the real `pmset -g` binary and confirms the call doesn't throw or
    /// hang. The returned value depends on the host machine's current state,
    /// so we only assert that some Bool came back. Skipped if pmset is
    /// unexpectedly missing.
    func test_readDisableSleepState_returnsWithoutCrashing() async throws {
        try XCTSkipUnless(
            FileManager.default.isExecutableFile(atPath: "/usr/bin/pmset"),
            "pmset not available — skipping integration smoke test"
        )
        _ = await PowerManagement.readDisableSleepState()
    }

    // MARK: - PMError messages

    func test_error_authorizationCancelled_hasUserFacingMessage() {
        let err = PowerManagement.PMError.authorizationCancelled
        XCTAssertNotNil(err.errorDescription)
        XCTAssertTrue(err.errorDescription?.lowercased().contains("password") == true,
                      "Cancellation message should mention 'password' — got: \(err.errorDescription ?? "nil")")
    }

    func test_error_osascriptFailed_includesExitCode() {
        let err = PowerManagement.PMError.osascriptFailed(code: 42, stderr: "boom")
        XCTAssertTrue(err.errorDescription?.contains("42") == true)
    }

    func test_error_sudoersInstallFailed_hasUserFacingMessage() {
        let err = PowerManagement.PMError.sudoersInstallFailed
        XCTAssertNotNil(err.errorDescription)
        XCTAssertFalse(err.errorDescription?.isEmpty == true)
    }

    // MARK: - isValidUsername

    func test_username_typicalNames_areValid() {
        for name in ["bone", "jonathan.hawkins", "user_01", "j-h", "ROOT", "a"] {
            XCTAssertTrue(PowerManagement.isValidUsername(name), "expected valid: \(name)")
        }
    }

    func test_username_rejectsShellMetacharactersAndWhitespace() {
        // These are the inputs that could break the rule line or the heredoc, or
        // smuggle extra commands into sudoers. All must be rejected.
        let bad = [
            "", " ", "two words", "a;b", "a$b", "a`b`", "a\"b", "a'b",
            "a\nb", "a\tb", "a/b", "a*", "a\\b", "a,b", "a=b", "a:b",
        ]
        for name in bad {
            XCTAssertFalse(PowerManagement.isValidUsername(name),
                           "expected invalid: \(name.debugDescription)")
        }
    }

    func test_username_rejectsOverlongInput() {
        XCTAssertFalse(PowerManagement.isValidUsername(String(repeating: "a", count: 33)))
        XCTAssertTrue(PowerManagement.isValidUsername(String(repeating: "a", count: 32)))
    }

    // MARK: - sudoers rule construction

    func test_sudoersLine_grantsExactlyTheTwoPmsetCommands() {
        let line = PowerManagement.sudoersLine(forUser: "bone")
        XCTAssertEqual(
            line,
            "bone ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0,"
                + " /usr/bin/pmset -a disablesleep 1")
        // No wildcards — argv is matched exactly, so the rule grants nothing else.
        XCTAssertFalse(line.contains("*"))
    }

    func test_sudoersContent_endsWithRuleLineAndIsFlushLeft() {
        let content = PowerManagement.sudoersContent(forUser: "bone")
        let lines = content.split(separator: "\n", omittingEmptySubsequences: false)
        XCTAssertEqual(lines.last, Substring(PowerManagement.sudoersLine(forUser: "bone")))
        // Every line must start at column 0; sudoers and the quoted heredoc both
        // depend on no stray leading indentation creeping in.
        for line in lines {
            XCTAssertFalse(line.first == " " || line.first == "\t",
                           "unexpected leading whitespace: \(line.debugDescription)")
        }
        // Comment header documents the grant.
        XCTAssertTrue(content.hasPrefix("# Installed by VoxHerd"))
    }

    // MARK: - appleScriptEscape

    func test_appleScriptEscape_escapesBackslashThenQuote() {
        XCTAssertEqual(PowerManagement.appleScriptEscape("/tmp/plain.sh"), "/tmp/plain.sh")
        XCTAssertEqual(PowerManagement.appleScriptEscape("a\"b"), "a\\\"b")
        // Backslash must be escaped before the quote so the result stays valid.
        XCTAssertEqual(PowerManagement.appleScriptEscape("a\\b"), "a\\\\b")
        XCTAssertEqual(PowerManagement.appleScriptEscape("a\\\"b"), "a\\\\\\\"b")
    }
}
