import Foundation
import ServiceManagement

@Observable
final class Preferences {
    var bridgePort: Int {
        didSet {
            let clamped = max(1024, min(65535, bridgePort))
            if bridgePort != clamped { bridgePort = clamped }
            UserDefaults.standard.set(bridgePort, forKey: "bridgePort")
        }
    }
    var enableTTS: Bool {
        didSet { UserDefaults.standard.set(enableTTS, forKey: "enableTTS") }
    }
    var enableSTT: Bool {
        didSet { UserDefaults.standard.set(enableSTT, forKey: "enableSTT") }
    }
    var enableWakeWord: Bool {
        didSet { UserDefaults.standard.set(enableWakeWord, forKey: "enableWakeWord") }
    }
    var launchAtLogin: Bool {
        didSet {
            UserDefaults.standard.set(launchAtLogin, forKey: "launchAtLogin")
            updateLoginItem()
        }
    }

    init() {
        let defaults = UserDefaults.standard
        let rawPort = defaults.object(forKey: "bridgePort") as? Int ?? 7777
        self.bridgePort = max(1024, min(65535, rawPort))
        self.enableTTS = defaults.object(forKey: "enableTTS") as? Bool ?? true
        self.enableSTT = defaults.object(forKey: "enableSTT") as? Bool ?? false
        self.enableWakeWord = defaults.object(forKey: "enableWakeWord") as? Bool ?? false
        self.launchAtLogin = defaults.object(forKey: "launchAtLogin") as? Bool ?? false
    }

    private func updateLoginItem() {
        do {
            if launchAtLogin {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            // Best effort — login item registration can fail in sandboxed environments
        }
    }
}
