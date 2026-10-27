import SwiftUI

struct StatusBarView: View {
    var processManager: BridgeProcessManager
    var preferences: Preferences

    private var isRunning: Bool {
        processManager.state == .running
    }

    private var statusText: String {
        switch processManager.state {
        case .stopped: return "Stopped"
        case .starting: return "Starting..."
        case .running: return "Running"
        case .error(let msg): return "Error: \(msg)"
        }
    }

    private var statusColor: Color {
        switch processManager.state {
        case .stopped: return .red
        case .starting: return .yellow
        case .running: return .green
        case .error: return .red
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            headerSection
                .padding(.horizontal, 12)
                .padding(.top, 12)
                .padding(.bottom, 8)

            // A waiting pairing request goes above everything, because it is
            // the only thing here with a deadline.
            if !processManager.pairingPrompts.isEmpty {
                pairingSection
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
            }

            Divider()

            // Sessions
            sessionsSection
                .padding(.vertical, 8)

            Divider()

            // Recent events
            eventsSection

            Divider()

            // Controls
            controlsSection
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
        }
        .frame(width: 320)
    }

    // MARK: - Header

    private var headerSection: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)

            Text("VoxHerd Bridge")
                .font(.system(.headline, design: .default, weight: .semibold))

            Spacer()

            if isRunning {
                Text(":\(processManager.port)")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }

            Text(statusText)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - Pairing

    private var pairingSection: some View {
        VStack(spacing: 6) {
            ForEach(processManager.pairingPrompts) { prompt in
                PairingCard(prompt: prompt)
            }
        }
    }

    // MARK: - Sessions

    private var sessionsSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Section header
            HStack {
                Text("Sessions")
                    .font(.system(.caption, weight: .medium))
                    .foregroundStyle(.secondary)

                Spacer()

                if !processManager.sessions.isEmpty {
                    sessionSummaryBadges
                }
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 6)

            if !isRunning {
                // Bridge not running
                HStack {
                    Spacer()
                    VStack(spacing: 4) {
                        Image(systemName: "bolt.slash")
                            .font(.title3)
                            .foregroundStyle(.tertiary)
                        Text("Bridge not running")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 6)
                    Spacer()
                }
            } else if processManager.sessions.isEmpty {
                // Running but no sessions
                HStack {
                    Spacer()
                    VStack(spacing: 4) {
                        Image(systemName: "terminal")
                            .font(.title3)
                            .foregroundStyle(.tertiary)
                        Text("No active sessions")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                        Text("Start Claude Code in a tmux pane")
                            .font(.caption2)
                            .foregroundStyle(.quaternary)
                    }
                    .padding(.vertical, 6)
                    Spacer()
                }
            } else {
                // Session list
                VStack(spacing: 1) {
                    ForEach(processManager.sessions) { session in
                        SessionRow(session: session)
                    }
                }
            }
        }
    }

    /// Compact summary badges showing counts by state category.
    private var sessionSummaryBadges: some View {
        let active = processManager.sessions.filter(\.isActive).count
        let attention = processManager.sessions.filter(\.needsAttention).count
        let idle = processManager.sessions.count - active - attention

        return HStack(spacing: 6) {
            if active > 0 {
                summaryPill(count: active, color: .blue, icon: "bolt.fill")
            }
            if attention > 0 {
                summaryPill(count: attention, color: .yellow, icon: "hand.raised.fill")
            }
            if idle > 0 {
                summaryPill(count: idle, color: .secondary, icon: "moon.fill")
            }
        }
    }

    private func summaryPill(count: Int, color: Color, icon: String) -> some View {
        HStack(spacing: 2) {
            Image(systemName: icon)
                .font(.system(size: 8))
            Text("\(count)")
                .font(.system(.caption2, design: .monospaced, weight: .medium))
        }
        .foregroundStyle(color)
        .padding(.horizontal, 5)
        .padding(.vertical, 2)
        .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 4))
    }

    // MARK: - Events

    private var eventsSection: some View {
        Group {
            if processManager.recentEvents.isEmpty {
                Text("No events yet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 8)
            } else {
                // A height, not a maxHeight. A ScrollView has no ideal height,
                // and the MenuBarExtra window sizes itself from the ideal: with
                // maxHeight the window reserved room for the log while the log
                // laid out at zero, so "45 events" showed no rows and the
                // window had a blank strip below the controls.
                LogView(entries: Binding(
                    get: { processManager.recentEvents },
                    set: { processManager.recentEvents = $0 }
                ))
                .frame(height: 200)
            }
        }
    }

    // MARK: - Controls

    private var isStarting: Bool {
        processManager.state == .starting
    }

    private var controlsSection: some View {
        HStack {
            if isStarting {
                Button("Starting…") {}
                    .buttonStyle(.borderedProminent)
                    .tint(.yellow)
                    .controlSize(.small)
                    .disabled(true)
            } else {
                Button(isRunning ? "Stop Bridge" : "Start Bridge") {
                    if isRunning {
                        processManager.stop()
                    } else {
                        processManager.start(
                            port: preferences.bridgePort,
                            enableTTS: preferences.enableTTS,
                            enableSTT: preferences.enableSTT,
                            enableWakeWord: preferences.enableWakeWord
                        )
                    }
                }
                .buttonStyle(.borderedProminent)
                .tint(isRunning ? .red : .green)
                .controlSize(.small)
            }

            Spacer()

            muteButton

            Button {
                SettingsWindowController.shared.open(preferences: preferences, processManager: processManager)
            } label: {
                Image(systemName: "gear")
            }
            .controlSize(.small)

            Button("Quit") {
                processManager.stop()
                NSApplication.shared.terminate(nil)
            }
            .controlSize(.small)
        }
    }

    private var muteButton: some View {
        Button {
            let newValue = !preferences.enableTTS
            preferences.enableTTS = newValue
            Task { await processManager.setTTSEnabled(newValue) }
        } label: {
            Image(systemName: preferences.enableTTS ? "speaker.wave.2.fill" : "speaker.slash.fill")
                .foregroundStyle(preferences.enableTTS ? Color.primary : Color.red)
        }
        .controlSize(.small)
        .help(preferences.enableTTS ? "Mute announcements" : "Unmute announcements")
    }
}

// MARK: - PairingCard

/// A pairing request, for as long as its code is good for.
///
/// Everything on this card is here because the wearer cannot get it anywhere
/// else. The code, because a headset does not use its cameras and so cannot
/// scan the QR the iPhone scans. The asking address, because it is the only
/// part of the request a stranger could not choose — a card naming only the
/// device looks identical whether the request came from the wearer's own
/// headset or from someone else on the network, which is what would make a
/// relay invisible (see `notification()` in `bridge/pairing.py`). The
/// countdown, because the code dies in two minutes whether or not anyone is
/// looking at it.
private struct PairingCard: View {
    let prompt: PairingPrompt

    var body: some View {
        // The clock drives the countdown, not the model: a card that
        // re-published itself every second to tick would redraw the whole
        // popover with it.
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let remaining = prompt.secondsRemaining(at: context.date)

            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 5) {
                    Image(systemName: "person.badge.key.fill")
                        .font(.system(size: 10))
                    Text("Pairing request")
                        .font(.system(.caption, weight: .semibold))

                    Spacer()

                    Text(prompt.countdown(at: context.date))
                        .font(.system(.caption2, design: .monospaced))
                        .monospacedDigit()
                        .foregroundStyle(remaining <= 20 ? Color.orange : Color.secondary)
                }
                .foregroundStyle(.primary)

                Text(prompt.code)
                    .font(.system(size: 30, weight: .semibold, design: .monospaced))
                    .kerning(2)
                    .frame(maxWidth: .infinity)
                    .textSelection(.enabled)
                    .accessibilityLabel(spelledOut(prompt.code))

                ProgressView(value: Double(remaining), total: prompt.lifetime)
                    .progressViewStyle(.linear)
                    .tint(remaining <= 20 ? .orange : .accentColor)

                // The device name is a stranger's string; the address is not.
                HStack(spacing: 4) {
                    Text(prompt.device)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Text("asked from \(prompt.host)")
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                if prompt.attempts > 0 {
                    HStack(spacing: 4) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.system(size: 9))
                        Text(prompt.attempts == 1
                             ? "1 wrong code tried"
                             : "\(prompt.attempts) wrong codes tried")
                            .font(.caption2)
                    }
                    .foregroundStyle(.orange)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 9)
            .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 7))
            .overlay(
                RoundedRectangle(cornerRadius: 7)
                    .strokeBorder(Color.accentColor.opacity(0.35), lineWidth: 1)
            )
        }
    }

    /// "471 902" as "4 7 1, 9 0 2", so VoiceOver reads digits rather than
    /// "four hundred and seventy-one".
    private func spelledOut(_ code: String) -> String {
        code.split(separator: " ")
            .map { $0.map(String.init).joined(separator: " ") }
            .joined(separator: ", ")
    }
}

// MARK: - SessionRow

/// A single session displayed in the sessions list.
private struct SessionRow: View {
    let session: SessionInfo

    var body: some View {
        HStack(spacing: 8) {
            // Activity icon with colored background
            Image(systemName: session.activityIcon)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(session.statusColor)
                .frame(width: 20, height: 20)
                .background(session.statusColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 4))

            // Project name and agent number
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(session.project)
                        .font(.system(.caption, weight: .medium))
                        .lineLimit(1)

                    if session.agentNumber > 1 {
                        Text("#\(session.agentNumber)")
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                }

                // Summary or activity label
                if !session.lastSummary.isEmpty && !session.isActive {
                    Text(session.lastSummary)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                } else {
                    Text(session.activityLabel)
                        .font(.caption2)
                        .foregroundStyle(session.statusColor.opacity(0.8))
                        .lineLimit(1)
                }
            }

            Spacer()

            // Sub-agent count if any
            if session.subAgentCount > 0 {
                HStack(spacing: 2) {
                    Image(systemName: "person.2.fill")
                        .font(.system(size: 8))
                    Text("\(session.subAgentCount)")
                        .font(.system(.caption2, design: .monospaced))
                }
                .foregroundStyle(.secondary)
                .padding(.horizontal, 4)
                .padding(.vertical, 2)
                .background(.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 3))
            }

            // Attention indicator
            if session.needsAttention {
                Circle()
                    .fill(.yellow)
                    .frame(width: 6, height: 6)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .contentShape(Rectangle())
        .background(session.needsAttention ? Color.yellow.opacity(0.04) : Color.clear)
    }
}
