import Foundation

/// What the bridge needs on this Mac before it can hear an agent: tmux, and at
/// least one assistant CLI running inside it.
///
/// Without them the bridge starts, pairs a phone or a headset, and then shows
/// nothing at all -- no sessions, and no error, because an empty tmux server
/// is a valid answer. That looked identical to a broken bridge, so onboarding
/// asks first.
///
/// The search is the bridge's own, not this app's. The app hands the bridge a
/// fixed PATH (`BridgeProcessManager`), and the bridge widens it with the
/// assistants' install directories under `$HOME` (`bridge/env_utils.py`,
/// `_EXTRA_PATH_DIRS`). A check against the app's PATH would tick an npm
/// install the bridge never finds, or miss `~/.local/bin/claude`, which the
/// bridge does find; either way the tick would mean nothing.
enum Prerequisites {

    struct Tool: Equatable {
        let command: String
        let name: String
    }

    static let tmux = Tool(command: "tmux", name: "tmux")

    /// The assistants the bridge dispatches to. Composer runs as `grok`.
    static let agents = [
        Tool(command: "claude", name: "Claude Code"),
        Tool(command: "codex", name: "Codex"),
        Tool(command: "gemini", name: "Gemini CLI"),
        Tool(command: "grok", name: "Grok Build"),
    ]

    struct Report: Equatable {
        /// Where tmux is, or nil.
        let tmux: String?
        /// The assistants found, in `agents` order.
        let agents: [Tool]
        /// Whether Homebrew is here, which decides how tmux is offered.
        let homebrew: Bool

        var ready: Bool { tmux != nil && !agents.isEmpty }
    }

    /// The bridge's directories, in its order: the PATH the app gives it, then
    /// `_EXTRA_PATH_DIRS`. Keep this and those two lists in step.
    static func searchDirectories(home: String) -> [String] {
        [
            "/usr/bin", "/bin", "/usr/sbin", "/sbin",
            "/usr/local/sbin", "/usr/local/bin",
            "/opt/homebrew/bin", "/opt/homebrew/sbin",
            home + "/.local/bin",
            home + "/.cargo/bin",
            home + "/.claude/local",
            home + "/.gemini/bin",
            home + "/.codex/bin",
            home + "/.grok/bin",
        ]
    }

    static func locate(_ command: String, in directories: [String], isExecutable: (String) -> Bool) -> String? {
        directories.lazy.map { $0 + "/" + command }.first(where: isExecutable)
    }

    static func check(
        home: String = NSHomeDirectory(),
        isExecutable: (String) -> Bool = FileManager.default.isExecutableFile(atPath:)
    ) -> Report {
        let directories = searchDirectories(home: home)
        return Report(
            tmux: locate(tmux.command, in: directories, isExecutable: isExecutable),
            agents: agents.filter { locate($0.command, in: directories, isExecutable: isExecutable) != nil },
            homebrew: locate("brew", in: directories, isExecutable: isExecutable) != nil
        )
    }
}
