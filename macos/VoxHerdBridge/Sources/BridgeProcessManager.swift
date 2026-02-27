import AppKit
import Foundation
import os

@MainActor
@Observable
final class BridgeProcessManager {
    enum BridgeState: Equatable {
        case stopped
        case starting
        case running
        case error(String)
    }

    var state: BridgeState = .stopped
    var recentEvents: [LogEntry] = []
    var sessionCount: Int = 0
    var sessions: [SessionInfo] = []
    var port: Int = 7777

    /// Auth token loaded from ~/.voxherd/auth_token after bridge signals ready.
    private(set) var authToken: String?

    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var readTask: Task<Void, Never>?
    private var stderrReadTask: Task<Void, Never>?
    private var restartTask: Task<Void, Never>?
    private var forceKillTask: Task<Void, Never>?
    private var sessionPollTask: Task<Void, Never>?
    private var restartDelay: TimeInterval = 1.0
    private let maxEvents = 200
    private let logger = Logger(subsystem: "com.voxherd.bridge", category: "ProcessManager")

    // Health check: consecutive poll failures while state == .running
    private var consecutivePollFailures = 0
    private let maxPollFailuresBeforeRestart = 5 // ~10s at 2s interval

    /// Last known preferences for auto-restart.
    private var lastPort: Int = 7777
    private var lastEnableTTS: Bool = true
    private var lastEnableSTT: Bool = false
    private var lastEnableWakeWord: Bool = false

    // MARK: - Lifecycle

    /// Auto-start the bridge shortly after the process manager is created.
    /// Uses a brief delay to let SwiftUI finish scene setup.
    func autoStart(preferences: Preferences) {
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(300))
            guard state == .stopped else { return }
            start(
                port: preferences.bridgePort,
                enableTTS: preferences.enableTTS,
                enableSTT: preferences.enableSTT,
                enableWakeWord: preferences.enableWakeWord
            )
        }
    }

    func start(
        port: Int = 7777,
        enableTTS: Bool = true,
        enableSTT: Bool = false,
        enableWakeWord: Bool = false
    ) {
        guard state == .stopped || isErrorState else { return }

        // Validate port range
        guard (1024...65535).contains(port) else {
            state = .error("Invalid port: \(port) (must be 1024–65535)")
            logger.error("Invalid port number: \(port)")
            return
        }

        state = .starting
        restartDelay = 1.0
        consecutivePollFailures = 0
        self.port = port
        self.lastPort = port
        self.lastEnableTTS = enableTTS
        self.lastEnableSTT = enableSTT
        self.lastEnableWakeWord = enableWakeWord

        // Kill any lingering process on the port to prevent EADDRINUSE
        Self.killProcessOnPort(port)

        let bridgePath = Self.bridgeBinaryPath()
        guard let bridgePath else {
            state = .error("Bridge binary not found in app bundle")
            logger.error("Bridge binary not found")
            return
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: bridgePath)

        var args = ["run", "--headless"]
        if enableTTS { args.append("--tts") }
        if enableSTT { args.append("--listen") }
        if enableWakeWord { args.append("--wake-word") }
        args.append(contentsOf: ["--port", "\(port)"])
        proc.arguments = args

        // Build environment — only add system-owned directories to PATH.
        // User-writable dirs (~/.local/bin, ~/.cargo/bin) are intentionally
        // excluded to prevent PATH hijacking.
        var env = ProcessInfo.processInfo.environment
        let extraPaths = [
            "/usr/local/bin",
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        ]
        let currentPath = env["PATH"] ?? ""
        let currentDirs = Set(currentPath.split(separator: ":").map(String.init))
        let additions = extraPaths.filter { !currentDirs.contains($0) }
        if !additions.isEmpty {
            env["PATH"] = (additions + [currentPath]).joined(separator: ":")
        }

        // Point to bundled STT binary
        if let sttPath = Self.sttBinaryPath() {
            env["VOXHERD_STT_BINARY"] = sttPath
        }
        proc.environment = env

        // Capture stdout for JSON log lines
        let outPipe = Pipe()
        proc.standardOutput = outPipe

        // Capture stderr for error reporting
        let errPipe = Pipe()
        proc.standardError = errPipe

        self.stdoutPipe = outPipe
        self.stderrPipe = errPipe
        self.process = proc

        // Handle termination
        proc.terminationHandler = { [weak self] process in
            Task { @MainActor in
                guard let self else { return }
                let code = process.terminationStatus
                if self.state == .running || self.state == .starting {
                    self.logger.warning("Bridge exited unexpectedly with code \(code)")
                    self.state = .error("Bridge exited (code \(code))")
                    self.scheduleRestart()
                } else {
                    self.state = .stopped
                }
            }
        }

        do {
            try proc.run()
            logger.info("Bridge process started (PID: \(proc.processIdentifier))")
            startReadingOutput(pipe: outPipe)
            startReadingStderr(pipe: errPipe)
        } catch {
            state = .error("Failed to start: \(error.localizedDescription)")
            logger.error("Failed to start bridge: \(error)")
        }
    }

    func stop() {
        stopSessionPolling()

        restartTask?.cancel()
        restartTask = nil

        readTask?.cancel()
        readTask = nil

        stderrReadTask?.cancel()
        stderrReadTask = nil

        forceKillTask?.cancel()
        forceKillTask = nil

        guard let proc = process, proc.isRunning else {
            state = .stopped
            process = nil
            stdoutPipe = nil
            stderrPipe = nil
            return
        }

        state = .stopped // Set before sending signal to prevent restart

        // Send SIGINT for graceful shutdown
        proc.interrupt()

        // Force kill after 5 seconds if still running
        forceKillTask = Task { [logger] in
            try? await Task.sleep(for: .seconds(5))
            guard !Task.isCancelled else { return }
            if proc.isRunning {
                proc.terminate()
                logger.warning("Force-killed bridge after timeout")
            }
        }

        process = nil
        stdoutPipe = nil
        stderrPipe = nil
    }

    func restart(
        port: Int = 7777,
        enableTTS: Bool = true,
        enableSTT: Bool = false,
        enableWakeWord: Bool = false
    ) {
        stop()
        Task {
            try? await Task.sleep(for: .milliseconds(500))
            start(port: port, enableTTS: enableTTS, enableSTT: enableSTT, enableWakeWord: enableWakeWord)
        }
    }

    // MARK: - Output Parsing

    private func startReadingOutput(pipe: Pipe) {
        readTask = Task.detached { [weak self] in
            let handle = pipe.fileHandleForReading
            var buffer = Data()

            while !Task.isCancelled {
                let data = handle.availableData
                if data.isEmpty { break } // EOF

                buffer.append(data)

                while let newlineIndex = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                    let lineData = buffer[buffer.startIndex..<newlineIndex]
                    buffer.removeSubrange(buffer.startIndex...newlineIndex)

                    guard let line = String(data: lineData, encoding: .utf8),
                          !line.isEmpty else { continue }

                    await self?.processLine(line)
                }
            }
        }
    }

    private func startReadingStderr(pipe: Pipe) {
        stderrReadTask = Task.detached { [weak self] in
            let handle = pipe.fileHandleForReading
            var buffer = Data()

            while !Task.isCancelled {
                let data = handle.availableData
                if data.isEmpty { break }

                buffer.append(data)

                while let newlineIndex = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                    let lineData = buffer[buffer.startIndex..<newlineIndex]
                    buffer.removeSubrange(buffer.startIndex...newlineIndex)

                    guard let line = String(data: lineData, encoding: .utf8),
                          !line.isEmpty else { continue }

                    await self?.processStderrLine(line)
                }
            }
        }
    }

    private func processLine(_ line: String) {
        guard let data = line.data(using: .utf8) else { return }

        // Check for ready signal
        if let ready = try? JSONDecoder().decode(ReadySignal.self, from: data),
           ready.status == "ready" {
            state = .running
            port = ready.port
            logger.info("Bridge ready on port \(ready.port)")
            loadAuthToken()
            startSessionPolling()
            return
        }

        // Parse as log entry
        if let entry = try? JSONDecoder().decode(LogEntry.self, from: data) {
            recentEvents.append(entry)
            if recentEvents.count > maxEvents {
                recentEvents.removeFirst(recentEvents.count - maxEvents)
            }

            // Trigger an immediate session refresh on registration/removal events
            // so the UI updates without waiting for the next 2s poll cycle.
            let msg = entry.message
            if msg.contains("registered") || msg.contains("removed")
                || msg.contains("deregistered") || msg.contains("Cleared") {
                Task { await fetchSessions() }
            }
        }
    }

    private func processStderrLine(_ line: String) {
        logger.error("Bridge stderr: \(line)")
        // Surface critical stderr as error log entries in the UI
        if line.contains("Error") || line.contains("Traceback") || line.contains("error") {
            let entry = LogEntry(
                timestamp: ISO8601DateFormatter().string(from: Date()),
                level: "error",
                project: "bridge",
                message: line
            )
            recentEvents.append(entry)
            if recentEvents.count > maxEvents {
                recentEvents.removeFirst(recentEvents.count - maxEvents)
            }
        }
    }

    // MARK: - Session Polling

    private func startSessionPolling() {
        sessionPollTask?.cancel()
        sessionPollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled else { break }
                await self?.fetchSessions()
            }
        }
    }

    private func stopSessionPolling() {
        sessionPollTask?.cancel()
        sessionPollTask = nil
        sessions = []
        sessionCount = 0
    }

    private func fetchSessions() async {
        guard state == .running else { return }
        let currentPort = self.port
        guard let url = URL(string: "http://127.0.0.1:\(currentPort)/api/sessions") else {
            logger.error("Invalid session poll URL for port \(currentPort)")
            return
        }
        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            if let token = authToken, !token.isEmpty {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse,
                  httpResponse.statusCode == 200 else {
                recordPollFailure()
                return
            }

            // Healthy response — reset failure counter
            consecutivePollFailures = 0

            // The endpoint returns { session_id: { ...session_data } }
            // Decode as a dictionary keyed by session_id
            let decoded = try JSONDecoder().decode([String: SessionInfo].self, from: data)
            let sorted = decoded.values.sorted { a, b in
                // Active/attention sessions first, then by project name
                if a.isActive != b.isActive { return a.isActive }
                if a.needsAttention != b.needsAttention { return a.needsAttention }
                return a.project.localizedCaseInsensitiveCompare(b.project) == .orderedAscending
            }
            sessions = sorted
            sessionCount = sorted.count
        } catch {
            logger.debug("Session poll failed: \(error)")
            recordPollFailure()
        }
    }

    /// Track consecutive health check failures and force-restart when threshold is exceeded.
    private func recordPollFailure() {
        guard state == .running else { return }
        consecutivePollFailures += 1
        logger.warning("Health check failed (\(self.consecutivePollFailures)/\(self.maxPollFailuresBeforeRestart))")

        if consecutivePollFailures >= maxPollFailuresBeforeRestart {
            logger.error("Bridge unresponsive after \(self.consecutivePollFailures) health checks, forcing restart")
            consecutivePollFailures = 0
            // Force-kill the process and restart with saved preferences
            forceRestartWithSavedPreferences()
        }
    }

    /// Kill the current process (if any) and restart with the last-known preferences.
    private func forceRestartWithSavedPreferences() {
        stop()
        let port = lastPort
        let tts = lastEnableTTS
        let stt = lastEnableSTT
        let wakeWord = lastEnableWakeWord
        Task {
            try? await Task.sleep(for: .seconds(1))
            self.start(port: port, enableTTS: tts, enableSTT: stt, enableWakeWord: wakeWord)
        }
    }

    // MARK: - Auth Token

    static let authTokenPath = NSHomeDirectory() + "/.voxherd/auth_token"

    /// Read auth token from ~/.voxherd/auth_token (written by the bridge on startup).
    private func loadAuthToken() {
        do {
            let token = try String(contentsOfFile: Self.authTokenPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !token.isEmpty {
                authToken = token
                logger.info("Auth token loaded from \(Self.authTokenPath)")
            }
        } catch {
            logger.warning("No auth token at \(Self.authTokenPath): \(error)")
        }
    }

    /// Copy the current auth token to the pasteboard.
    /// Automatically clears it after 30 seconds to limit exposure.
    func copyAuthTokenToClipboard() -> Bool {
        guard let token = authToken, !token.isEmpty else { return false }
        let pasteboard = NSPasteboard.general
        let changeCount = pasteboard.changeCount
        pasteboard.clearContents()
        pasteboard.setString(token, forType: .string)

        // Auto-clear after 30 seconds if pasteboard hasn't been changed
        Task {
            try? await Task.sleep(for: .seconds(30))
            if pasteboard.changeCount == changeCount + 1 {
                pasteboard.clearContents()
                pasteboard.setString("", forType: .string)
            }
        }
        return true
    }

    // MARK: - Auto-restart

    private func scheduleRestart() {
        let delay = restartDelay
        let port = lastPort
        let tts = lastEnableTTS
        let stt = lastEnableSTT
        let wakeWord = lastEnableWakeWord
        logger.info("Restarting bridge in \(delay)s...")

        restartTask = Task {
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled else { return }
            self.restartDelay = min(self.restartDelay * 2, 30.0)
            self.start(port: port, enableTTS: tts, enableSTT: stt, enableWakeWord: wakeWord)
        }
    }

    // MARK: - Helpers

    private var isErrorState: Bool {
        if case .error = state { return true }
        return false
    }

    // MARK: - Port Cleanup

    /// Kill only voxherd-bridge processes on the given port.
    /// Uses lsof with -c to filter by command name, avoiding killing unrelated processes.
    private static func killProcessOnPort(_ port: Int) {
        guard (1024...65535).contains(port) else { return }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        // -c voxherd: only match processes whose command starts with "voxherd"
        // -t: terse output (PIDs only)
        // -i: match network files on this port
        proc.arguments = ["-t", "-c", "voxherd", "-i", ":\(port)"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        proc.waitUntilExit()

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let output = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !output.isEmpty else { return }

        for pidStr in output.split(separator: "\n") {
            if let pid = Int32(pidStr.trimmingCharacters(in: .whitespaces)),
               pid > 1 { // Never kill PID 0 or 1
                kill(pid, SIGTERM)
            }
        }
        // Brief pause for port to be released
        Thread.sleep(forTimeInterval: 0.3)
    }

    // MARK: - Bundle Paths

    static func bridgeBinaryPath() -> String? {
        // 1. In the .app bundle: Contents/Resources/voxherd-bridge/voxherd-bridge
        if let resourcePath = Bundle.main.resourcePath {
            let path = (resourcePath as NSString).appendingPathComponent("voxherd-bridge/voxherd-bridge")
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }

        #if DEBUG
        // 2. Development fallback: next to the .app bundle (same dir)
        let nextToApp = (Bundle.main.bundlePath as NSString)
            .deletingLastPathComponent
            .appending("/dist/voxherd-bridge/voxherd-bridge")
        if FileManager.default.isExecutableFile(atPath: nextToApp) {
            return nextToApp
        }

        // 3. Development fallback: use VOXHERD_PROJECT_DIR env var or ~/.voxherd config
        if let projectDir = ProcessInfo.processInfo.environment["VOXHERD_PROJECT_DIR"] {
            let devPath = projectDir + "/macos/dist/voxherd-bridge/voxherd-bridge"
            if FileManager.default.isExecutableFile(atPath: devPath) {
                return devPath
            }
        }
        // 4. Last resort: check ~/.voxherd/config for project_dir
        let configPath = NSHomeDirectory() + "/.voxherd/config.json"
        if let data = FileManager.default.contents(atPath: configPath),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let dir = config["project_dir"] as? String {
            let devPath = dir + "/macos/dist/voxherd-bridge/voxherd-bridge"
            if FileManager.default.isExecutableFile(atPath: devPath) {
                return devPath
            }
        }
        #endif

        return nil
    }

    static func sttBinaryPath() -> String? {
        // Check bundle first
        if let resourcePath = Bundle.main.resourcePath {
            let path = (resourcePath as NSString).appendingPathComponent("voxherd-bridge/bridge/stt/voxherd-listen")
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }

        #if DEBUG
        // Development fallback: use VOXHERD_PROJECT_DIR env var
        if let projectDir = ProcessInfo.processInfo.environment["VOXHERD_PROJECT_DIR"] {
            let devPath = projectDir + "/macos/dist/voxherd-bridge/bridge/stt/voxherd-listen"
            if FileManager.default.isExecutableFile(atPath: devPath) {
                return devPath
            }
        }
        // Fallback: check ~/.voxherd/config for project_dir
        let configPath = NSHomeDirectory() + "/.voxherd/config.json"
        if let data = FileManager.default.contents(atPath: configPath),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let dir = config["project_dir"] as? String {
            let devPath = dir + "/macos/dist/voxherd-bridge/bridge/stt/voxherd-listen"
            if FileManager.default.isExecutableFile(atPath: devPath) {
                return devPath
            }
        }
        #endif

        return nil
    }
}

// MARK: - Supporting Types

private struct ReadySignal: Decodable {
    let status: String
    let port: Int
}
