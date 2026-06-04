import AppKit
import SwiftUI

@main
struct VoxHerdBridgeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var processManager: BridgeProcessManager
    @State private var preferences: Preferences
    @State private var walkAround = WalkAroundController.shared

    init() {
        let prefs = Preferences()
        let pm = BridgeProcessManager()
        _preferences = State(initialValue: prefs)
        _processManager = State(initialValue: pm)
        pm.autoStart(preferences: prefs)

        // Sync Walk-Around state with the real `pmset disablesleep` flag. If it
        // was left on by a previous run (or set via Terminal), this re-arms the
        // safety auto-off timer so a forgotten session still recovers.
        Task { @MainActor in
            await WalkAroundController.shared.refreshFromSystem()
        }

        if !prefs.hasCompletedOnboarding {
            Task { @MainActor in
                // Brief delay so the menu bar item registers and NSApp is fully up
                // before the modal-ish window pops over the user's screen.
                try? await Task.sleep(for: .milliseconds(250))
                OnboardingWindowController.shared.open(preferences: prefs, processManager: pm)
            }
        }
    }

    var body: some Scene {
        MenuBarExtra("VoxHerd", systemImage: menuBarIcon) {
            StatusBarView(processManager: processManager, preferences: preferences)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(preferences: preferences, processManager: processManager)
        }
    }

    /// While Walk-Around Mode is keeping the Mac awake, the icon switches to a
    /// distinct "walking" glyph so it can't be silently forgotten; otherwise it
    /// reflects whether the bridge is running.
    private var menuBarIcon: String {
        if walkAround.isActive { return "figure.walk.circle.fill" }
        return processManager.state == .running ? "waveform.circle.fill" : "waveform.circle"
    }
}

/// Reverts Walk-Around Mode on quit so the persistent `pmset disablesleep` flag
/// is never left on after VoxHerd exits. The revert is silent (`sudo -n`), so it
/// adds no password prompt to quitting.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminate(
        _ sender: NSApplication
    ) -> NSApplication.TerminateReply {
        // AppKit calls this on the main thread; hop onto the MainActor to read
        // the controller without spuriously delaying quits that don't need it.
        let active = MainActor.assumeIsolated { WalkAroundController.shared.isActive }
        guard active else { return .terminateNow }
        Task { @MainActor in
            await WalkAroundController.shared.revertForQuit()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

/// Manages a standalone NSWindow for Settings since @Environment(\.openSettings)
/// does not work from MenuBarExtra with .window style.
@MainActor
final class SettingsWindowController {
    static let shared = SettingsWindowController()

    private var window: NSWindow?

    func open(preferences: Preferences, processManager: BridgeProcessManager? = nil) {
        if let existing = window, existing.isVisible {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let settingsView = SettingsView(preferences: preferences, processManager: processManager)

        let hostingController = NSHostingController(rootView: settingsView)
        let win = NSWindow(contentViewController: hostingController)
        win.title = "VoxHerd Settings"
        win.styleMask = [.titled, .closable, .resizable]
        win.setContentSize(NSSize(width: 420, height: 800))
        win.minSize = NSSize(width: 380, height: 600)
        win.center()
        win.isReleasedWhenClosed = false
        win.level = .normal

        self.window = win
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}
