import SwiftUI

struct SettingsView: View {
    @Bindable var preferences: Preferences
    var processManager: BridgeProcessManager?
    @State private var hookInstallStatus: String?
    @State private var tokenCopied = false
    @State private var showToken = false
    @State private var showLargeQR = false

    // Walk-Around Mode is owned by a shared controller so the toggle, the menu
    // bar icon, and app termination all agree on one source of truth (the real
    // `pmset disablesleep` flag) and the safety auto-off survives this window
    // closing.
    private let walkAround = WalkAroundController.shared

    var body: some View {
        Form {
            bridgeServerSection
            pairingSection
            voiceFeaturesSection
            walkAroundSection
            systemSection
            aboutSection
        }
        .formStyle(.grouped)
        .frame(minWidth: 420, idealWidth: 440, minHeight: 660, idealHeight: 800)
    }

    private var bridgeServerSection: some View {
        Section("Bridge Server") {
            LabeledContent("Port") {
                TextField("", value: $preferences.bridgePort, format: .number.grouping(.never))
                    .frame(width: 80)
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
            }
            Text("Valid range: 1024–65535")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var pairingSection: some View {
        Section("Pair iPhone") {
            if let token = processManager?.authToken, !token.isEmpty {
                pairingContent(token: token)
            } else {
                Label("Token will appear after bridge starts", systemImage: "hourglass")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 4)
            }
        }
    }

    private func pairingContent(token: String) -> some View {
        let payload = pairingPayload(token: token)

        return VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 14) {
                QRThumb(payload: payload, size: 96) { showLargeQR = true }

                VStack(alignment: .leading, spacing: 6) {
                    Text("Scan with the iPhone Camera or the VoxHerd app.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    Button("Enlarge QR Code") { showLargeQR = true }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .popover(isPresented: $showLargeQR, arrowEdge: .leading) {
                            LargeQRPopover(payload: payload)
                        }
                }
                Spacer(minLength: 0)
            }

            Divider()

            VStack(alignment: .leading, spacing: 6) {
                Text("Or paste the token manually")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                HStack(spacing: 8) {
                    Text(showToken ? token : String(token.prefix(8)) + "…" + String(token.suffix(4)))
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Button(showToken ? "Hide" : "Reveal") { showToken.toggle() }
                        .buttonStyle(.borderless)
                        .font(.caption)

                    Button {
                        if processManager?.copyAuthTokenToClipboard() == true {
                            tokenCopied = true
                            Task {
                                try? await Task.sleep(for: .seconds(2))
                                tokenCopied = false
                            }
                        }
                    } label: {
                        Label(tokenCopied ? "Copied" : "Copy", systemImage: tokenCopied ? "checkmark" : "doc.on.doc")
                            .labelStyle(.titleAndIcon)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .tint(tokenCopied ? .green : .accentColor)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func pairingPayload(token: String) -> String {
        let host = NetworkInfo.primaryLANAddress() ?? "127.0.0.1"
        let port = processManager?.port ?? 7777
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

    private var voiceFeaturesSection: some View {
        Section {
            voiceToggle(
                title: "Text-to-Speech",
                subtitle: "Announce agent results through your Mac speakers",
                isOn: Binding(
                    get: { preferences.enableTTS },
                    set: { newValue in
                        preferences.enableTTS = newValue
                        if let pm = processManager {
                            Task { await pm.setTTSEnabled(newValue) }
                        }
                    }
                )
            )
            voiceToggle(
                title: "Speech-to-Text",
                subtitle: "Listen for voice commands after announcements",
                isOn: $preferences.enableSTT
            )
            voiceToggle(
                title: "Wake Word",
                subtitle: "Always-on listening for \"Hey Claude\"",
                isOn: $preferences.enableWakeWord
            )

            Label {
                Text("Silence a single agent: start it with `VOXHERD_QUIET=1` set (e.g. `VOXHERD_QUIET=1 claude`). It still shows on the dashboard and glasses but never speaks — handy when several agents are running at once.")
            } icon: {
                Image(systemName: "speaker.slash")
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        } header: {
            Text("Voice Features")
        }
    }

    private func voiceToggle(title: String, subtitle: String, isOn: Binding<Bool>) -> some View {
        Toggle(isOn: isOn) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(subtitle)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var walkAroundSection: some View {
        Section {
            Toggle(isOn: Binding(
                get: { walkAround.isActive },
                set: { newValue in Task { await walkAround.setEnabled(newValue) } }
            )) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Keep Mac awake with lid closed")
                    Text("Stream to your glasses while you walk. Asks for your password the first time.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .disabled(walkAround.inFlight)

            Text("Turns itself off when you quit VoxHerd or after 2 hours. Closing the lid limits airflow — don't run heavy tasks for long stretches.")
                .font(.caption2)
                .foregroundStyle(.secondary)

            if let errorMessage = walkAround.lastError {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        } header: {
            Label("Walk-Around Mode", systemImage: "figure.walk.motion")
        }
        .task { await walkAround.refreshFromSystem() }
    }

    private var systemSection: some View {
        Section("System") {
            Toggle("Launch at Login", isOn: $preferences.launchAtLogin)

            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 10) {
                    Button("Install Hooks") { installHooks() }
                        .help("Copy Claude Code hook scripts to ~/.voxherd/hooks/")

                    if let status = hookInstallStatus {
                        Label(
                            status,
                            systemImage: status.contains("Failed") ? "xmark.circle.fill" : "checkmark.circle.fill"
                        )
                        .font(.caption)
                        .foregroundStyle(status.contains("Failed") ? .red : .green)
                    }

                    Spacer()

                    Button("Open Welcome…") {
                        OnboardingWindowController.shared.open(
                            preferences: preferences,
                            processManager: processManager
                        )
                    }
                    .buttonStyle(.borderless)
                    .help("Re-open the first-launch setup window")
                }

                Text("Hooks let Claude Code notify the bridge when agents finish.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            Button("Open Logs Folder") {
                let logsPath = NSHomeDirectory() + "/.voxherd/logs"
                NSWorkspace.shared.open(URL(fileURLWithPath: logsPath))
            }
            .buttonStyle(.borderless)
        }
    }

    private var aboutSection: some View {
        Section("About") {
            LabeledContent("Version", value: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev")
        }
    }

    private func installHooks() {
        hookInstallStatus = nil
        switch HookInstaller.install() {
        case .installed:
            hookInstallStatus = "Installed"
        case .failed(let reason):
            hookInstallStatus = "Failed: \(reason)"
        }
    }
}

/// Thumbnail QR rendered alongside the pairing token. Tap-target for the
/// enlarged popover so users can either click the image or the dedicated button.
private struct QRThumb: View {
    let payload: String
    let size: CGFloat
    var onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            ZStack {
                if let image = QRCodeGenerator.generate(from: payload, size: size) {
                    Image(nsImage: image)
                        .interpolation(.none)
                        .resizable()
                        .scaledToFit()
                } else {
                    Color.gray.opacity(0.2)
                    Image(systemName: "qrcode")
                        .font(.system(size: size * 0.4))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: size, height: size)
            .background(Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .stroke(Color(NSColor.separatorColor), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .help("Click to enlarge")
    }
}

private struct LargeQRPopover: View {
    let payload: String

    var body: some View {
        VStack(spacing: 12) {
            if let image = QRCodeGenerator.generate(from: payload, size: 320) {
                Image(nsImage: image)
                    .interpolation(.none)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 320, height: 320)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            Text("Scan with the iPhone Camera or the VoxHerd iOS app.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .padding(20)
    }
}
