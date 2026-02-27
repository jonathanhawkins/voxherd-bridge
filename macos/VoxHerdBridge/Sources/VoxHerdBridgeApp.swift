import AppKit
import SwiftUI

@main
struct VoxHerdBridgeApp: App {
    @State private var processManager: BridgeProcessManager
    @State private var preferences: Preferences

    init() {
        let prefs = Preferences()
        let pm = BridgeProcessManager()
        _preferences = State(initialValue: prefs)
        _processManager = State(initialValue: pm)
        pm.autoStart(preferences: prefs)
    }

    var body: some Scene {
        MenuBarExtra(
            "VoxHerd",
            systemImage: processManager.state == .running ? "waveform.circle.fill" : "waveform.circle"
        ) {
            StatusBarView(processManager: processManager, preferences: preferences)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(preferences: preferences, processManager: processManager)
        }
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
            .frame(width: 380, height: 480)

        let hostingController = NSHostingController(rootView: settingsView)
        let win = NSWindow(contentViewController: hostingController)
        win.title = "VoxHerd Settings"
        win.styleMask = [.titled, .closable]
        win.center()
        win.isReleasedWhenClosed = false
        win.level = .normal

        self.window = win
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}
