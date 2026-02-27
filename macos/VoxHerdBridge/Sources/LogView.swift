import SwiftUI

// MARK: - LogView

struct LogView: View {
    @Binding var entries: [LogEntry]

    @State private var searchText: String = ""
    @State private var levelFilter: LevelFilter = .all

    enum LevelFilter: String, CaseIterable {
        case all = "All"
        case errors = "Errors"
        case warnings = "Warnings"
        case success = "Success"

        var matchingLevel: String? {
            switch self {
            case .all: return nil
            case .errors: return "error"
            case .warnings: return "warning"
            case .success: return "success"
            }
        }
    }

    private var filteredEntries: [LogEntry] {
        entries.filter { entry in
            // Level filter
            if let level = levelFilter.matchingLevel, entry.level != level {
                return false
            }
            // Text search
            if !searchText.isEmpty {
                let query = searchText.lowercased()
                let matchesProject = entry.project.lowercased().contains(query)
                let matchesMessage = entry.message.lowercased().contains(query)
                if !matchesProject && !matchesMessage {
                    return false
                }
            }
            return true
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            // Filter bar
            filterBar
                .padding(.horizontal, 8)
                .padding(.vertical, 6)

            Divider()

            // Log content
            if filteredEntries.isEmpty {
                emptyState
            } else {
                logScrollView
            }
        }
    }

    // MARK: - Filter Bar

    private var filterBar: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundColor(.secondary)
                    .font(.caption2)
                TextField("Filter logs...", text: $searchText)
                    .textFieldStyle(.plain)
                    .font(.caption2)
                if !searchText.isEmpty {
                    Button {
                        searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundColor(.secondary)
                            .font(.caption2)
                    }
                    .buttonStyle(.plain)
                }
                Spacer()
                Button("Clear") {
                    entries.removeAll()
                }
                .font(.caption2)
                .buttonStyle(.plain)
                .foregroundColor(.secondary)
                .disabled(entries.isEmpty)
            }

            HStack(spacing: 4) {
                ForEach(LevelFilter.allCases, id: \.self) { filter in
                    FilterPill(
                        title: filter.rawValue,
                        isSelected: levelFilter == filter,
                        color: pillColor(for: filter)
                    ) {
                        levelFilter = filter
                    }
                }
                Spacer()
                Text("\(filteredEntries.count) event\(filteredEntries.count == 1 ? "" : "s")")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundColor(.secondary)
            }
        }
    }

    // MARK: - Log Scroll View

    private var logScrollView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(filteredEntries) { entry in
                        LogEntryRow(entry: entry)
                            .id(entry.id)
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
            }
            .onChange(of: entries.count) {
                // Auto-scroll to newest entry at bottom
                if let last = filteredEntries.last {
                    withAnimation(.easeOut(duration: 0.15)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
    }

    // MARK: - Empty State

    private var emptyState: some View {
        VStack(spacing: 6) {
            Spacer()
            Image(systemName: "text.alignleft")
                .font(.title2)
                .foregroundColor(.secondary)
            if !searchText.isEmpty || levelFilter != .all {
                Text("No matching events")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Text("Try adjusting your filters")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            } else {
                Text("No events yet")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Text("Events will appear here as Claude Code sessions report in")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
            }
            Spacer()
        }
        .frame(maxWidth: .infinity)
        .padding()
    }

    // MARK: - Helpers

    private func pillColor(for filter: LevelFilter) -> Color {
        switch filter {
        case .all: return .primary
        case .errors: return .red
        case .warnings: return .yellow
        case .success: return .green
        }
    }
}

// MARK: - LogEntryRow

struct LogEntryRow: View {
    let entry: LogEntry

    private var levelDotColor: Color {
        entry.levelColor
    }

    private var formattedTime: String {
        // Show just HH:MM:SS from ISO 8601 timestamp
        if let tIndex = entry.timestamp.firstIndex(of: "T") {
            let timeStart = entry.timestamp.index(after: tIndex)
            let timePart = entry.timestamp[timeStart...]
            // Trim timezone suffix if present
            if let plusIndex = timePart.firstIndex(of: "+") {
                return String(timePart[..<plusIndex])
            } else if let zIndex = timePart.firstIndex(of: "Z") {
                return String(timePart[..<zIndex])
            }
            return String(timePart)
        }
        return entry.timestamp
    }

    var body: some View {
        HStack(alignment: .top, spacing: 4) {
            // Timestamp
            Text(formattedTime)
                .font(.system(.caption2, design: .monospaced))
                .foregroundColor(.secondary)
                .lineLimit(1)

            // Project name
            Text(entry.project)
                .font(.caption2)
                .bold()
                .lineLimit(1)

            // Level dot
            Circle()
                .fill(levelDotColor)
                .frame(width: 5, height: 5)
                .padding(.top, 3)

            // Message
            Text(entry.message)
                .font(.caption2)
                .foregroundColor(entry.levelColor)
                .lineLimit(2)
        }
        .padding(.vertical, 1)
    }
}

// MARK: - FilterPill

private struct FilterPill: View {
    let title: String
    let isSelected: Bool
    let color: Color
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(.caption2, weight: isSelected ? .semibold : .regular))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(
                    RoundedRectangle(cornerRadius: 4)
                        .fill(isSelected ? color.opacity(0.2) : Color.clear)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 4)
                        .strokeBorder(isSelected ? color.opacity(0.5) : Color.secondary.opacity(0.3), lineWidth: 0.5)
                )
                .foregroundColor(isSelected ? color : .secondary)
        }
        .buttonStyle(.plain)
    }
}
