import SwiftUI

struct SettingsView: View {
    @Bindable var preferences: Preferences
    var processManager: BridgeProcessManager?
    @State private var hookInstallStatus: String?
    @State private var tokenCopied = false
    @State private var showToken = false
    @State private var showQRCode = false
    @State private var qrAutoHideTask: Task<Void, Never>?

    var body: some View {
        Form {
            bridgeServerSection
            authTokenSection
            voiceFeaturesSection
            systemSection
            aboutSection
        }
        .formStyle(.grouped)
        .frame(minWidth: 400, idealWidth: 420, minHeight: 600, idealHeight: 800)
    }

    private var bridgeServerSection: some View {
        Section("Bridge Server") {
            HStack {
                Text("Port")
                Spacer()
                TextField("Port", value: $preferences.bridgePort, format: .number)
                    .frame(width: 80)
                    .textFieldStyle(.roundedBorder)
            }
            Text("Valid range: 1024–65535")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var authTokenSection: some View {
        Section("Auth Token") {
            if let token = processManager?.authToken, !token.isEmpty {
                tokenDisplay(token: token)
                tokenActions(token: token)
                qrCodeSection(token: token)
            } else {
                Text("Token will appear after bridge starts")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func tokenDisplay(token: String) -> some View {
        HStack {
            if showToken {
                Text(token)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(1)
            } else {
                Text(String(token.prefix(8)) + "..." + String(token.suffix(4)))
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button(showToken ? "Hide" : "Reveal") {
                showToken.toggle()
            }
            .buttonStyle(.borderless)
            .font(.caption)
        }
    }

    private func tokenActions(token: String) -> some View {
        VStack(alignment: .leading) {
            HStack {
                Button("Copy Token") {
                    if processManager?.copyAuthTokenToClipboard() == true {
                        tokenCopied = true
                        Task {
                            try? await Task.sleep(for: .seconds(2))
                            tokenCopied = false
                        }
                    }
                }
                if tokenCopied {
                    Text("Copied!")
                        .font(.caption2)
                        .foregroundStyle(.green)
                }
            }
            Text("Paste this into the iOS app's Settings to connect securely.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func qrCodeSection(token: String) -> some View {
        Divider()

        Button(showQRCode ? "Hide QR Code" : "Show QR Code for iOS") {
            showQRCode.toggle()
            qrAutoHideTask?.cancel()
            if showQRCode {
                qrAutoHideTask = Task {
                    try? await Task.sleep(for: .seconds(60))
                    guard !Task.isCancelled else { return }
                    showQRCode = false
                }
            }
        }
        .buttonStyle(.borderless)

        if showQRCode {
            qrCodeImage(token: token)
        }
    }

    private func qrCodeImage(token: String) -> some View {
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
        let payload = components.string ?? "voxherd://connect"

        return VStack {
            if let qrImage = QRCodeGenerator.generate(from: payload, size: 200) {
                Image(nsImage: qrImage)
                    .interpolation(.none)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 200, height: 200)
                    .padding(.vertical, 8)
            }

            Text("Scan with VoxHerd iOS app or iPhone Camera")
                .font(.caption2)
                .foregroundStyle(.secondary)

            Text("Auto-hides in 60 seconds")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private var voiceFeaturesSection: some View {
        Section("Voice Features") {
            Toggle("Text-to-Speech (TTS)", isOn: $preferences.enableTTS)
                .help("Announce events through Mac speakers")
            Toggle("Speech-to-Text (STT)", isOn: $preferences.enableSTT)
                .help("Listen for voice commands after announcements")
            Toggle("Wake Word Detection", isOn: $preferences.enableWakeWord)
                .help("Always-on microphone listening for 'Hey Claude'")
        }
    }

    private var systemSection: some View {
        Section("System") {
            Toggle("Launch at Login", isOn: $preferences.launchAtLogin)

            HStack {
                Button("Install Hooks") {
                    installHooks()
                }
                .help("Copy Claude Code hook scripts to ~/.voxherd/hooks/")

                if let status = hookInstallStatus {
                    Text(status)
                        .font(.caption2)
                        .foregroundStyle(status.contains("Failed") ? .red : .green)
                }

                Spacer()

                Button("Open Logs Folder") {
                    let logsPath = NSHomeDirectory() + "/.voxherd/logs"
                    NSWorkspace.shared.open(URL(fileURLWithPath: logsPath))
                }
            }
        }
    }

    private var aboutSection: some View {
        Section("About") {
            LabeledContent("Version", value: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev")
        }
    }

    private func installHooks() {
        hookInstallStatus = nil

        guard let resourcePath = Bundle.main.resourcePath else {
            hookInstallStatus = "Failed: no bundle resources"
            return
        }

        // Resolve to canonical path to prevent symlink traversal
        let rawPath = (resourcePath as NSString).appendingPathComponent("hooks/install.sh")
        let canonicalPath = (rawPath as NSString).standardizingPath

        // Verify the resolved path is still inside the app bundle
        guard canonicalPath.hasPrefix(resourcePath) else {
            hookInstallStatus = "Failed: path outside bundle"
            return
        }

        guard FileManager.default.isExecutableFile(atPath: canonicalPath) else {
            hookInstallStatus = "Failed: install.sh not found in bundle"
            return
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [canonicalPath]
        do {
            try proc.run()
            proc.waitUntilExit()
            hookInstallStatus = proc.terminationStatus == 0 ? "Installed" : "Failed (exit \(proc.terminationStatus))"
        } catch {
            hookInstallStatus = "Failed: \(error.localizedDescription)"
        }
    }
}
