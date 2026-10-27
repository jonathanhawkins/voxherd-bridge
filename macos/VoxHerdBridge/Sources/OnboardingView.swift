import AppKit
import SwiftUI

struct OnboardingView: View {
    @Bindable var preferences: Preferences
    var processManager: BridgeProcessManager?
    var dismiss: () -> Void

    @State private var hookStatus: HookStatus
    @State private var installing = false
    @State private var showQR = false
    @State private var prerequisites = Prerequisites.check()

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
                    requirementsStep
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
            Text("A few quick steps to get voice control of your AI agents.")
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

    private var requirementsStep: some View {
        StepCard(number: 1, title: "Check what VoxHerd needs") {
            Text("VoxHerd works with coding agents running inside tmux on this Mac.")
                .font(.callout)
                .foregroundStyle(.secondary)

            requirementRow(
                found: prerequisites.tmux != nil,
                title: "tmux",
                detail: prerequisites.tmux ?? (prerequisites.homebrew
                    ? "Not found. In Terminal: brew install tmux"
                    : "Not found. Install Homebrew, then in Terminal: brew install tmux")
            )
            if prerequisites.tmux == nil && !prerequisites.homebrew {
                Link("Homebrew", destination: URL(string: "https://brew.sh")!)
                    .font(.callout)
                    .padding(.leading, 26)
            }

            requirementRow(
                found: !prerequisites.agents.isEmpty,
                title: "A coding agent",
                detail: prerequisites.agents.isEmpty
                    ? "None found. Install one and sign in to it in Terminal first."
                    : prerequisites.agents.map(\.name).joined(separator: ", ")
            )
            if prerequisites.agents.isEmpty {
                HStack(spacing: 12) {
                    Link("Claude Code", destination: URL(string: "https://github.com/anthropics/claude-code")!)
                    Link("Codex", destination: URL(string: "https://github.com/openai/codex")!)
                    Link("Gemini CLI", destination: URL(string: "https://github.com/google-gemini/gemini-cli")!)
                }
                .font(.callout)
                .padding(.leading, 26)
            }

            Button("Check Again") { prerequisites = Prerequisites.check() }
        }
    }

    private func requirementRow(found: Bool, title: String, detail: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: found ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(found ? .green : .red)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.callout.weight(.medium))
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    private var hooksStep: some View {
        StepCard(number: 2, title: "Install Claude Code hooks") {
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
        StepCard(number: 3, title: "Set up Tailscale (optional)") {
            Text("Only needed to talk to your bridge from outside your home network. Free for personal use.")
                .font(.callout)
                .foregroundStyle(.secondary)

            HStack(spacing: 12) {
                Link("Tailscale for Mac", destination: URL(string: "https://tailscale.com/download/mac")!)
                Link("for iPhone", destination: URL(string: "https://apps.apple.com/us/app/tailscale/id1470499037")!)
                // The headset is Android. Without this the one client that most
                // needs a tailnet -- it has no cellular and roams between
                // networks -- had no link here at all.
                Link("for the headset", destination: URL(string: "https://play.google.com/store/apps/details?id=com.tailscale.ipn")!)
                Spacer()
                tailscaleStatusBadge
            }
            .font(.callout)
        }
    }

    @ViewBuilder
    /// Three states, because the middle one is not success and the last one was
    /// silent.
    ///
    /// This had no `else`: with Tailscale absent the badge rendered *nothing*,
    /// so the step showed download links and gave no sign that anything was
    /// wanted — the one case where the user has something to do.
    ///
    /// "Installed" was also green, which read as done. An install with no
    /// address is not reachable: `tailscaled` is not up or nobody has signed
    /// in, and the bridge's `/api/connection-info` has no tailnet URL to hand
    /// out. Only an address earns green.
    private var tailscaleStatusBadge: some View {
        if NetworkInfo.tailscaleAddress() != nil {
            Label("Connected", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .font(.callout)
        } else if NetworkInfo.isTailscaleInstalled() {
            Label("Installed — sign in to connect", systemImage: "exclamationmark.circle.fill")
                .foregroundStyle(.orange)
                .font(.callout)
        } else {
            Label("Not installed", systemImage: "arrow.down.circle")
                .foregroundStyle(.secondary)
                .font(.callout)
        }
    }

    private var pairingStep: some View {
        StepCard(number: 4, title: "Pair your iPhone or headset") {
            Text("On iPhone, install the VoxHerd app and scan the bridge's QR code. On a headset, open VoxHerd: it finds this bridge, and you say the six-digit code this Mac shows.")
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
