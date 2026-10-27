import AppKit
import Foundation
import os

private let log = Logger(subsystem: "com.voxherd.bridge", category: "PowerManagement")

/// Wraps `pmset disablesleep` so the menu bar app can flip a single flag that
/// keeps the Mac awake with the lid closed. Apple's official clamshell mode
/// requires an external monitor + keyboard + mouse; this is the documented
/// workaround for headless / glasses-only setups.
///
/// `disablesleep` is a *persistent, system-wide* flag — it survives app quit and
/// stays set until something clears it. Left on with the lid shut, the Mac can't
/// sleep and overheats. To let VoxHerd revert it automatically (on quit and on a
/// safety timeout) without nagging for a password every time, the first enable
/// installs a tightly-scoped `/etc/sudoers.d/voxherd` rule granting NOPASSWD for
/// exactly the two `pmset -a disablesleep {0,1}` commands. After that, toggling
/// and auto-revert run silently via `sudo -n`. Interactive prompts only ever
/// happen on the user's explicit first toggle.
enum PowerManagement {
    /// Path of the no-password rule we install. Scoped to two exact commands.
    static let sudoersPath = "/etc/sudoers.d/voxherd"

    enum PMError: Swift.Error, LocalizedError {
        case authorizationCancelled
        case invalidUsername(String)
        case sudoersInstallFailed
        case osascriptFailed(code: Int32, stderr: String)

        var errorDescription: String? {
            switch self {
            case .authorizationCancelled:
                return "Cancelled. Your password is required to set this up the first time."
            case .invalidUsername:
                return "Couldn't enable — unexpected account name."
            case .sudoersInstallFailed:
                return "Couldn't install the no-password rule safely; nothing was changed."
            case .osascriptFailed(let code, _):
                return "Couldn't update sleep setting (exit \(code))."
            }
        }
    }

    // MARK: - Read

    /// Returns true iff `pmset -g` reports `disablesleep 1`. Never throws —
    /// any failure (missing binary, parse error, non-zero exit) collapses to
    /// false so the UI defaults to "off" rather than misleading the user.
    static func readDisableSleepState() async -> Bool {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                // `/usr/bin/pmset` is a guaranteed macOS system binary. Don't
                // gate it on `isExecutableFile` — that probe can silently
                // return false inside frozen .app bundles, masking the real
                // call site (same reason CLAUDE.md says don't `shutil.which`
                // system binaries). Let `Process.run()` throw if missing.
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
                proc.arguments = ["-g"]
                let stdout = Pipe()
                proc.standardOutput = stdout
                proc.standardError = Pipe()
                do {
                    try proc.run()
                    proc.waitUntilExit()
                    guard proc.terminationStatus == 0 else {
                        log.warning("pmset -g exited \(proc.terminationStatus)")
                        continuation.resume(returning: false)
                        return
                    }
                    let data = (try? stdout.fileHandleForReading.readToEnd()) ?? Data()
                    let text = String(data: data, encoding: .utf8) ?? ""
                    continuation.resume(returning: parseDisableSleep(text))
                } catch {
                    log.error("pmset -g failed to launch: \(error.localizedDescription)")
                    continuation.resume(returning: false)
                }
            }
        }
    }

    // MARK: - Write

    /// Interactive toggle, used by the Settings switch. Tries the silent path
    /// first (works once the no-password rule exists); if that fails, falls
    /// back to the elevated path that installs the rule and sets the flag in a
    /// single authenticated step. May show the macOS auth dialog — but only on
    /// the explicit user toggle, never from background/quit code.
    @MainActor
    static func setDisableSleepInteractive(_ enabled: Bool) async throws -> Bool {
        if await setDisableSleepSilently(enabled) {
            return true
        }
        log.info("Silent set failed (rule missing?) — elevating to install rule + set flag")
        return try await elevateInstallAndSet(enabled)
    }

    /// Silent set via `sudo -n` — never prompts. Returns true on success, false
    /// if the no-password rule isn't in place (or sudo would otherwise prompt).
    /// Safe to call from the auto-off timer and from app termination, where a
    /// password dialog would be hostile or impossible.
    static func setDisableSleepSilently(_ enabled: Bool) async -> Bool {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let proc = Process()
                // `sudo -n` => non-interactive: exits non-zero rather than
                // prompting. With our NOPASSWD rule present, it just runs.
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/sudo")
                proc.arguments = ["-n", "/usr/bin/pmset", "-a", "disablesleep", enabled ? "1" : "0"]
                proc.standardOutput = Pipe()
                proc.standardError = Pipe()
                do {
                    try proc.run()
                    proc.waitUntilExit()
                    continuation.resume(returning: proc.terminationStatus == 0)
                } catch {
                    log.error("sudo -n pmset failed to launch: \(error.localizedDescription)")
                    continuation.resume(returning: false)
                }
            }
        }
    }

    /// Install the no-password rule and set the flag in one authenticated shell
    /// script (single password prompt). The script validates the new sudoers
    /// drop-in with `visudo -cf` before moving it into place, then re-validates
    /// the whole sudoers set and rolls back if anything is wrong — a malformed
    /// drop-in can otherwise break `sudo` system-wide.
    @MainActor
    private static func elevateInstallAndSet(_ enabled: Bool) async throws -> Bool {
        let user = NSUserName()
        guard isValidUsername(user) else {
            log.error("Refusing to build sudoers rule for invalid username")
            throw PMError.invalidUsername(user)
        }

        // Bring the menu bar app to the front so the auth sheet is associated
        // with VoxHerd rather than the previously focused app (LSUIElement quirk).
        NSApp.activate(ignoringOtherApps: true)

        // The script is inlined into the AppleScript, NOT written to a file.
        //
        // It used to go to a temp file whose path was handed to root's /bin/sh.
        // `do shell script ... with administrator privileges` authorizes first
        // and root opens the path second, and that file lived in
        // NSTemporaryDirectory() — owned and writable by the unprivileged user,
        // for the entire duration of the password dialog (the cleanup `defer`
        // only ran after waitUntilExit). Any process running as the user could
        // watch for the filename, truncate it, and write its own payload while
        // the user was already being asked to approve VoxHerd; 0o700 does not
        // help, because the attacker *is* the owner. Root then ran their script.
        //
        // Inlining removes the file, so there is nothing to swap between the
        // authorization and the execution. `do shell script` hands the string to
        // `sh -c`, so the heredoc and `set -e` behave exactly as before.
        let scriptBody = installScript(forUser: user, enable: enabled)
        let appleScript =
            "do shell script \"\(appleScriptEscape(scriptBody))\""
            + " with administrator privileges"

        log.info("Elevating to install sudoers rule and set disablesleep=\(enabled ? 1 : 0)")

        return try await Task.detached(priority: .userInitiated) {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            proc.arguments = ["-e", appleScript]
            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            proc.standardOutput = stdoutPipe
            proc.standardError = stderrPipe
            try proc.run()
            proc.waitUntilExit()

            if proc.terminationStatus == 0 {
                log.info("sudoers rule installed and disablesleep updated")
                return true
            }
            let errData = (try? stderrPipe.fileHandleForReading.readToEnd()) ?? Data()
            let stderr = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            // AppleScript surfaces `do shell script` failures as `... (N)` where
            // N is the command's exit code. -128 = user cancelled the auth
            // prompt; 91/92 are our own validation/rollback exit codes.
            if stderr.contains("(-128)") {
                log.notice("User cancelled the authentication prompt")
                throw PMError.authorizationCancelled
            }
            if stderr.contains("(91)") || stderr.contains("(92)") {
                log.error("sudoers validation failed — rule not installed")
                throw PMError.sudoersInstallFailed
            }
            // Mark stderr as .private — it can include $HOME paths (the username).
            log.error("osascript failed (exit \(proc.terminationStatus)): \(stderr, privacy: .private)")
            throw PMError.osascriptFailed(code: proc.terminationStatus, stderr: stderr)
        }.value
    }

    // MARK: - Pure helpers (unit-tested)

    /// Parse the stdout of `pmset -g` and return whether the `disablesleep`
    /// key is set to 1. Exposed internally for unit testing — production code
    /// reaches this through `readDisableSleepState()`.
    static func parseDisableSleep(_ text: String) -> Bool {
        let keyName = "disablesleep"
        for raw in text.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard line.hasPrefix(keyName) else { continue }
            let rest = line.dropFirst(keyName.count)
            // Require a whitespace boundary after the key, so a future
            // hypothetical `disablesleeppolicy` key doesn't false-match here.
            guard let next = rest.first, next.isWhitespace else { continue }
            let suffix = rest.trimmingCharacters(in: .whitespaces)
            return suffix.hasPrefix("1")
        }
        return false
    }

    /// Whether `name` is safe to embed verbatim in a sudoers rule. Restricting
    /// to the POSIX portable username charset means no whitespace, quotes, or
    /// newlines can reach the file — the rule line and heredoc stay well-formed.
    static func isValidUsername(_ name: String) -> Bool {
        !name.isEmpty && name.count <= 32
            && name.range(of: "^[A-Za-z0-9._-]+$", options: .regularExpression) != nil
    }

    /// The single sudoers rule line: NOPASSWD for exactly the two pmset commands
    /// we ever run. No wildcards — sudo matches argv exactly, so this grants no
    /// other privilege.
    static func sudoersLine(forUser user: String) -> String {
        "\(user) ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0,"
            + " /usr/bin/pmset -a disablesleep 1"
    }

    /// Full contents of `/etc/sudoers.d/voxherd`. Built via array-join so every
    /// line is flush-left (sudoers + the quoted heredoc both care).
    static func sudoersContent(forUser user: String) -> String {
        [
            "# Installed by VoxHerd for Walk-Around Mode.",
            "# Lets VoxHerd silently toggle clamshell sleep (pmset disablesleep) so it",
            "# can auto-revert on quit and on a safety timeout without a password",
            "# prompt. Scoped to exactly two commands; grants no other privileges.",
            sudoersLine(forUser: user),
        ].joined(separator: "\n")
    }

    /// The privileged shell script run as root: write the drop-in to a temp
    /// file, validate it in isolation, move it into place, re-validate the whole
    /// sudoers set (rolling back on failure), then set the flag.
    private static func installScript(forUser user: String, enable: Bool) -> String {
        let content = sudoersContent(forUser: user)
        let flag = enable ? "1" : "0"
        return [
            "#!/bin/sh",
            "set -e",
            "SUDOERS=\"\(sudoersPath)\"",
            "TMP=\"$(/usr/bin/mktemp /tmp/voxherd-sudoers.XXXXXX)\"",
            "/bin/cat > \"$TMP\" <<'VOXHERD_SUDOERS_EOF'",
            content,
            "VOXHERD_SUDOERS_EOF",
            "/usr/sbin/chown root:wheel \"$TMP\"",
            "/bin/chmod 0440 \"$TMP\"",
            "/usr/sbin/visudo -cf \"$TMP\" >/dev/null 2>&1 || { /bin/rm -f \"$TMP\"; exit 91; }",
            "/bin/mv \"$TMP\" \"$SUDOERS\"",
            "/usr/sbin/visudo -c >/dev/null 2>&1 || { /bin/rm -f \"$SUDOERS\"; exit 92; }",
            "/usr/bin/pmset -a disablesleep \(flag)",
        ].joined(separator: "\n") + "\n"
    }

    /// Escape a string for inclusion inside an AppleScript double-quoted literal.
    /// Newlines become `\n` because an AppleScript double-quoted literal cannot
    /// contain a raw one — required now that the whole script, not just a path,
    /// is carried inside the literal.
    static func appleScriptEscape(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\r\n", with: "\\n")
            .replacingOccurrences(of: "\n", with: "\\n")
            .replacingOccurrences(of: "\r", with: "\\n")
            .replacingOccurrences(of: "\t", with: "\\t")
    }
}
