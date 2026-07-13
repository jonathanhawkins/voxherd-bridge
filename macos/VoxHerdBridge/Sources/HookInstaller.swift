import Foundation

/// Wraps the bundled `hooks/install.sh` so SettingsView and the first-launch
/// onboarding flow share one implementation. The script is shipped in the app
/// bundle's Resources (see `macos/build-app.sh` Step 4).
enum HookInstaller {
    enum Result {
        case installed
        case failed(String)
    }

    static var isInstalled: Bool {
        let home = NSHomeDirectory()
        // Any one of the supported assistant hook targets is enough to
        // treat hooks as installed (Claude / Gemini settings, Grok hooks dir).
        if let claudeSettings = try? String(
            contentsOfFile: home + "/.claude/settings.json",
            encoding: .utf8
        ), claudeSettings.contains(".voxherd/hooks/") {
            return true
        }
        if let geminiSettings = try? String(
            contentsOfFile: home + "/.gemini/settings.json",
            encoding: .utf8
        ), geminiSettings.contains(".voxherd/hooks/") {
            return true
        }
        let grokHooks = home + "/.grok/hooks"
        if let entries = try? FileManager.default.contentsOfDirectory(atPath: grokHooks) {
            for name in entries where name.hasPrefix("voxherd-") && name.hasSuffix(".json") {
                if let body = try? String(
                    contentsOfFile: grokHooks + "/" + name,
                    encoding: .utf8
                ), body.contains(".voxherd/hooks/") || body.contains("on-stop") {
                    return true
                }
            }
        }
        return false
    }

    static func install() -> Result {
        guard let resourcePath = Bundle.main.resourcePath else {
            return .failed("no bundle resources")
        }
        // Resolve symlinks on BOTH sides before comparing — `standardizingPath`
        // does not follow symlinks, so if any parent dir of the bundle is a
        // symlink the prefix check trivially passes.
        let rawPath = (resourcePath as NSString).appendingPathComponent("hooks/install.sh")
        let canonicalPath = (rawPath as NSString).resolvingSymlinksInPath
        let canonicalBundle = (resourcePath as NSString).resolvingSymlinksInPath
        guard canonicalPath.hasPrefix(canonicalBundle) else {
            return .failed("path outside bundle")
        }
        guard FileManager.default.isExecutableFile(atPath: canonicalPath) else {
            return .failed("install.sh not found in bundle")
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [canonicalPath]
        let stderr = Pipe()
        proc.standardError = stderr
        do {
            try proc.run()
            proc.waitUntilExit()
            if proc.terminationStatus == 0 {
                return .installed
            }
            let errData = (try? stderr.fileHandleForReading.readToEnd()) ?? Data()
            let detail = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return .failed(detail.isEmpty ? "exit \(proc.terminationStatus)" : detail)
        } catch {
            return .failed(error.localizedDescription)
        }
    }
}
