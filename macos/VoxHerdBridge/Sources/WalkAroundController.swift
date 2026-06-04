import Foundation
import os

/// Owns Walk-Around Mode state and its safety nets. The toggle, the menu bar
/// icon, and app termination all read/drive through this single object so the
/// UI can never disagree with the real `pmset disablesleep` flag, and the flag
/// can never be left on after VoxHerd is gone.
///
/// Interactive prompts only ever come from `setEnabled` (the explicit toggle).
/// `revertForQuit` and the auto-off timer are silent-only — they rely on the
/// no-password rule that `setEnabled` installs on first enable.
@MainActor
@Observable
final class WalkAroundController {
    static let shared = WalkAroundController()

    /// Safety net: even if the user forgets, clamshell sleep is re-enabled after
    /// this long. Long enough for a real walk, short enough that a forgotten
    /// session can't cook the Mac overnight.
    static let autoOffAfter: Duration = .seconds(2 * 60 * 60)

    private let log = Logger(subsystem: "com.voxherd.bridge", category: "WalkAround")

    /// Mirrors the real system flag. Read by the toggle and the menu bar icon.
    private(set) var isActive = false
    /// True while an interactive toggle is in flight (disables the switch).
    private(set) var inFlight = false
    /// Transient, user-facing error from the last toggle attempt.
    private(set) var lastError: String?

    private var timeoutTask: Task<Void, Never>?
    private var errorClearTask: Task<Void, Never>?

    private init() {}

    /// Align `isActive` with the real `pmset disablesleep` flag. Call on launch
    /// and whenever Settings appears so the UI never shows a stale state. If the
    /// flag is already on (e.g. set in a previous run), (re)arm the safety timer.
    func refreshFromSystem() async {
        let actual = await PowerManagement.readDisableSleepState()
        isActive = actual
        if actual { armTimeout() } else { cancelTimeout() }
    }

    /// Interactive toggle from the Settings switch. May prompt for a password
    /// the first time (to install the no-password rule); silent thereafter.
    func setEnabled(_ enabled: Bool) async {
        // `.disabled(inFlight)` only applies on the next render, so two taps in
        // one frame could both land here — guard against a second auth prompt.
        guard !inFlight else { return }
        inFlight = true
        defer { inFlight = false }
        errorClearTask?.cancel()
        lastError = nil
        do {
            _ = try await PowerManagement.setDisableSleepInteractive(enabled)
            isActive = enabled
            if enabled { armTimeout() } else { cancelTimeout() }
        } catch let err as PowerManagement.PMError {
            presentError(err.errorDescription)
        } catch {
            presentError(error.localizedDescription)
        }
    }

    /// Silent revert for app termination. Never prompts — relies on the
    /// no-password rule installed when the feature was first enabled. Returns
    /// once the attempt completes so the terminate handler can reply.
    func revertForQuit() async {
        guard isActive else { return }
        let ok = await PowerManagement.setDisableSleepSilently(false)
        log.info("Quit revert of disablesleep: \(ok ? "ok" : "failed (no rule?)")")
        if ok { isActive = false }
        cancelTimeout()
    }

    // MARK: - Safety timeout

    private func armTimeout() {
        timeoutTask?.cancel()
        timeoutTask = Task { [weak self] in
            try? await Task.sleep(for: WalkAroundController.autoOffAfter)
            guard !Task.isCancelled else { return }
            await self?.autoOff()
        }
    }

    private func autoOff() async {
        let ok = await PowerManagement.setDisableSleepSilently(false)
        log.notice("Walk-Around auto-off fired; silent revert \(ok ? "ok" : "failed")")
        if ok { isActive = false }
        timeoutTask = nil
    }

    private func cancelTimeout() {
        timeoutTask?.cancel()
        timeoutTask = nil
    }

    private func presentError(_ message: String?) {
        lastError = message
        errorClearTask?.cancel()
        errorClearTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard !Task.isCancelled else { return }
            self?.lastError = nil
        }
    }
}
