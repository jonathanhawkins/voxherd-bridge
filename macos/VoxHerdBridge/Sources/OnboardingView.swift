import AppKit
import SwiftUI

struct OnboardingView: View {
    @Bindable var preferences: Preferences
    var processManager: BridgeProcessManager?
    var dismiss: () -> Void

    @State private var hookStatus: HookStatus
    @State private var installing = false
    @State private var showQR = false

    init(preferences: Preferences, processManager: BridgeProcessManager?, dismiss: @escaping () -> Void) {
        self.preferences = preferences
        self.processManager = processManager
        self.dismiss = dismiss
        // Pre-populate based on whether settings.json already references the hooks
        // so re-running onboarding doesn't re-install over a working setup.
        _hookStatus = State(initialValue: HookInstaller.isInstalled ? .installed : .pending)
    }

    enum HookStatus: Equatable {
        case pending
        case installing
        case installed
        case failed(String)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    hooksStep
                    tailscaleStep
                    pairingStep
                }
                .padding(20)
            }
            footer
        }
        .frame(width: 520, height: 640)
        .background(Color(NSColor.windowBackgroundColor))
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Welcome to VoxHerd")
                .font(.system(.title2, design: .default, weight: .semibold))
            Text("Three quick steps to get voice control of your AI agents.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(Color(NSColor.controlBackgroundColor))
    }

    private var footer: some View {
        HStack {
            Spacer()
            Button("Skip") { finish() }
                .keyboardShortcut(.cancelAction)
            Button("Done") { finish() }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
        }
        .padding(16)
        .background(Color(NSColor.controlBackgroundColor))
    }

    private func finish() {
        preferences.hasCompletedOnboarding = true
        dismiss()
    }

    // MARK: - Steps

    private var hooksStep: some View {
        StepCard(number: 1, title: "Install Claude Code hooks") {
            Text("VoxHerd needs to know when your agents finish so it can announce results. This adds a hook entry to `~/.claude/settings.json` (also `~/.codex/`, `~/.gemini/`, and `~/.grok/hooks/` if those CLIs are installed).")
                .font(.callout)
                .foregroundStyle(.secondary)

            HStack(spacing: 12) {
                Button(action: install) {
                    if installing {
                        ProgressView().controlSize(.small).padding(.horizontal, 8)
                    } else {
                        Text(hookStatus == .installed ? "Re-install Hooks" : "Install Hooks")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(installing)

                statusBadge
            }

            if case .failed(let reason) = hookStatus {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    private var tailscaleStep: some View {
        StepCard(number: 2, title: "Set up Tailscale (optional)") {
            Text("Only needed to talk to your bridge from outside your home network. Free for personal use.")
                .font(.callout)
                .foregroundStyle(.secondary)

            HStack(spacing: 12) {
                Link("Tailscale for Mac", destination: URL(string: "https://tailscale.com/download/mac")!)
                Link("Tailscale for iPhone", destination: URL(string: "https://apps.apple.com/us/app/tailscale/id1470499037")!)
                Spacer()
                tailscaleStatusBadge
            }
            .font(.callout)
        }
    }

    @ViewBuilder
    private var tailscaleStatusBadge: some View {
        if NetworkInfo.tailscaleAddress() != nil {
            Label("Connected", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .font(.callout)
        } else if NetworkInfo.isTailscaleInstalled() {
            Label("Installed", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .font(.callout)
        }
    }

    private var pairingStep: some View {
        StepCard(number: 3, title: "Pair your iPhone") {
            Text("Install the VoxHerd iOS app, then scan the bridge's QR code to connect.")
                .font(.callout)
                .foregroundStyle(.secondary)

            HStack(spacing: 12) {
                Button("Show QR Code") { showQR.toggle() }
                if let token = processManager?.authToken, !token.isEmpty {
                    Text("Bridge ready")
                        .font(.caption)
                        .foregroundStyle(.green)
                } else {
                    Text("Waiting for bridge…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if showQR {
                if let pm = processManager, let token = pm.authToken, !token.isEmpty {
                    QRPayload(token: token, port: pm.port)
                } else {
                    Text("Bridge hasn't emitted an auth token yet. Wait a few seconds and try again.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func install() {
        installing = true
        hookStatus = .installing
        Task.detached(priority: .userInitiated) {
            let result = HookInstaller.install()
            await MainActor.run {
                installing = false
                switch result {
                case .installed: hookStatus = .installed
                case .failed(let reason): hookStatus = .failed(reason)
                }
            }
        }
    }

    @ViewBuilder
    private var statusBadge: some View {
        switch hookStatus {
        case .pending:
            EmptyView()
        case .installing:
            EmptyView()
        case .installed:
            Label("Installed", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .font(.callout)
        case .failed:
            Label("Failed", systemImage: "xmark.circle.fill")
                .foregroundStyle(.red)
                .font(.callout)
        }
    }
}

private struct StepCard<Content: View>: View {
    let number: Int
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                ZStack {
                    Circle()
                        .fill(Color.accentColor.opacity(0.15))
                        .frame(width: 28, height: 28)
                    Text("\(number)")
                        .font(.system(.callout, design: .rounded, weight: .semibold))
                        .foregroundStyle(.tint)
                }
                Text(title)
                    .font(.system(.title3, design: .default, weight: .semibold))
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(NSColor.controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color(NSColor.separatorColor), lineWidth: 1)
        )
    }
}

private struct QRPayload: View {
    let token: String
    let port: Int

    var body: some View {
        let payload = buildPayload()
        if let image = QRCodeGenerator.generate(from: payload, size: 180) {
            Image(nsImage: image)
                .interpolation(.none)
                .resizable()
                .scaledToFit()
                .frame(width: 180, height: 180)
                .padding(.top, 6)
        } else {
            Text("Couldn't generate QR code.")
                .font(.caption)
                .foregroundStyle(.red)
        }
    }

    private func buildPayload() -> String {
        let host = NetworkInfo.primaryLANAddress() ?? "127.0.0.1"
        var components = URLComponents()
        components.scheme = "voxherd"
        components.host = "connect"
        components.queryItems = [
            URLQueryItem(name: "host", value: host),
            URLQueryItem(name: "port", value: "\(port)"),
            URLQueryItem(name: "token", value: token),
        ]
        if let ts = NetworkInfo.tailscaleAddress() {
            components.queryItems?.append(URLQueryItem(name: "tailscale", value: ts))
        }
        return components.string ?? "voxherd://connect"
    }
}

@MainActor
final class OnboardingWindowController {
    static let shared = OnboardingWindowController()

    private var window: NSWindow?

    func open(preferences: Preferences, processManager: BridgeProcessManager?) {
        if let existing = window, existing.isVisible {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let view = OnboardingView(
            preferences: preferences,
            processManager: processManager,
            dismiss: { [weak self] in
                self?.window?.close()
                self?.window = nil
            }
        )

        let hostingController = NSHostingController(rootView: view)
        let win = NSWindow(contentViewController: hostingController)
        win.title = "Welcome to VoxHerd"
        win.styleMask = [.titled, .closable]
        win.setContentSize(NSSize(width: 520, height: 640))
        win.center()
        win.isReleasedWhenClosed = false
        win.level = .normal

        self.window = win
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}
