# Plan: Battlefield Canvas Activity View

## Problem Statement

The current `ActivityView.swift` renders project bases in a `LazyVGrid` with agents confined inside small ZStack arenas within each base card. This produces a standard mobile app grid layout that feels static and cramped. The goal is to transform this into a pannable/zoomable canvas -- a war room / RTS battlefield where project bases are spread across a map like buildings, agents can roam outside their bases, and the whole experience feels like a tactical command surface.

## Current State Analysis

### Existing Architecture (ActivityView.swift, 573 lines)
- **ProjectGroup** struct groups sessions by project name, tracks active state and snippets
- **ActivityView** is the root -- uses `ScrollView` + `LazyVGrid` with `GridItem(.adaptive(minimum: 160))`
- **GameHUD** sits at top with AGENTS/ACTIVE/SUB-AGENTS/TASKS counters
- **ProjectBase** is a card with VStack: project name, ZStack arena (140x100 fixed), snippet ticker
- **AgentNode** positions itself within a 140x100 arena using `activityPosition()` which maps activity types to normalized (-1..1) positions. Agents animate between positions with spring animation.
- **SubAgentOrbit** draws sub-agents in a circle around the parent agent node with connection lines
- **BaseSnippetTicker** shows truncated activity text at the bottom of each base

### Key Data Types (Models.swift)
- `SessionInfo` -- id, project, status, activityType, subAgentTasks, agentNumber, activitySnippet
- `ActivityType` -- enum with .thinking, .writing, .searching, .running, .building, .testing, .working, .completed, .sleeping, .errored, .stopped, .approval, .input, .registered, .dead
- `SubAgentTask` -- id, subject, status, activeForm, agentType
- `ProjectGroup` (local to ActivityView) -- groups sessions by project name

### Integration Points
- `AppState.sessions` provides `SessionRegistry` with live session data
- `AppState.activeProject` determines which project is highlighted
- `state.beginListening(forProject:)` is called on base tap (voice activation)
- `SessionRegistry.sortedSessions(activeProject:)` provides ordering
- Font: `.code()` extension on `Font` (Google Sans Code monospace)
- Haptics: `HapticManager` in Config/

## Design Vision

### RTS Battlefield Metaphor
The canvas represents a tactical operations map. Projects are forward operating bases (FOBs) spread across the map. Agents are units that:
- Stay near their base when idle/sleeping
- Move to "activity zones" on the canvas when working (thinking zone, writing zone, testing zone, etc.)
- Visually show what they're doing through movement, not just icons

### Key Visual Elements
1. **Canvas background** -- dark with subtle animated grid lines (like a tactical display or radar screen)
2. **Project bases** -- hexagonal or rounded structures anchored at spread-out positions, glowing when active
3. **Agent nodes** -- the existing circles, but now free to move around the entire canvas, not just within their base's 140x100 box
4. **Activity zones** -- labeled regions of the canvas (e.g., "THINK" zone top-center, "BUILD" zone bottom-right) with subtle boundary indicators
5. **Connection lines** -- thin dashed lines from agents back to their home base, so you can see who belongs where
6. **Sub-agent orbits** -- remain around parent agents but now with more room to spread
7. **HUD overlay** -- fixed position, not scrolling with the canvas

### Interaction Model
- **Pan**: Single-finger drag to pan the canvas
- **Zoom**: Pinch to zoom in/out (bounded: 0.5x to 3.0x)
- **Double-tap**: Zoom to fit all active agents (or reset to default)
- **Tap base**: Activate project for voice command (same as current)
- **Tap agent**: Quick info popup (session status, snippet)
- **Minimap**: Small overview in corner showing full canvas with viewport indicator

## Implementation Plan

### Phase 1: Canvas Infrastructure (new file: `CanvasState.swift`)

Create an `@Observable` class to manage all canvas state separate from the view.

```swift
// ios/VoxHerd/VoxHerd/UI/CanvasState.swift

@Observable
final class CanvasState {
    // Camera transform
    var offset: CGSize = .zero       // pan offset in canvas space
    var scale: CGFloat = 1.0         // zoom level
    var lastOffset: CGSize = .zero   // gesture tracking
    var lastScale: CGFloat = 1.0     // gesture tracking

    // Layout
    var canvasSize: CGSize = CGSize(width: 2000, height: 2000)
    var basePositions: [String: CGPoint] = [:]  // project name -> canvas position

    // Interaction
    var selectedProject: String?
    var hoveredAgent: String?        // session ID

    // Computed agent positions (project-independent, based on activity)
    func agentPosition(for session: SessionInfo, basePos: CGPoint) -> CGPoint
    func assignBasePositions(projects: [ProjectGroup])
}
```

**Base Position Algorithm:**
- Use a spatial hash / force-directed layout inspired approach, but deterministic:
  - Hash the project name to get a seed angle
  - Place bases in a spiral pattern radiating from center, with ~300pt minimum spacing
  - Active project gets center position
  - New projects get appended to the next available spiral position
  - Positions are stable -- they don't shift when projects are added/removed (use stored positions keyed by project name, only compute for new ones)

**Agent Position Algorithm (the key change):**
- Agents move in CANVAS SPACE, not within their base's bounding box
- Each `ActivityType` maps to a direction/distance from the base:
  - `.thinking` -- orbit slowly near the base (small radius, rotational movement)
  - `.writing` -- move 80-120pt to the "write zone" (left of base)
  - `.searching` -- move 100-150pt toward "search zone" (above base)
  - `.running`, `.building` -- move 60-100pt to "build zone" (right of base)
  - `.testing` -- move 80-120pt to "test zone" (below-right of base)
  - `.approval`, `.input` -- move toward canvas center (the "command post")
  - `.sleeping`, `.completed`, `.stopped` -- return to base (0,0 offset from base)
  - `.errored`, `.dead` -- slight offset with "fallen" visual
- Multiple agents of the same project in the same activity type fan out with angular offsets (like current stagger logic, but bigger radius)

### Phase 2: Canvas View (rewrite `ActivityView.swift`)

Replace the ScrollView+LazyVGrid with a ZStack-based canvas that applies affine transforms for pan/zoom.

```
Structure:
ActivityView
  ZStack {
    // Layer 0: Grid background (Canvas view, animated)
    TacticalGrid(scale:)

    // Layer 1: Activity zone labels (faded text like "THINK", "BUILD")
    ActivityZoneLabels(scale:)

    // Layer 2: Connection lines (agent -> home base)
    ConnectionLines(sessions:, basePositions:, agentPositions:)

    // Layer 3: Project bases (the "buildings")
    ForEach(projectGroups) { ProjectBaseNode(...) }

    // Layer 4: Agent nodes (free-roaming)
    ForEach(allSessions) { AgentNode(...) }

    // Layer 5: Sub-agent particles (around agents)
    ForEach(agentsWithSubAgents) { SubAgentCloud(...) }
  }
  .scaleEffect(canvasState.scale)
  .offset(canvasState.offset)
  .gesture(panGesture.simultaneously(with: zoomGesture))
  .overlay(alignment: .top) { GameHUD(...) }
  .overlay(alignment: .bottomTrailing) { Minimap(...) }
```

**Gesture Handling (iOS 17+):**
```swift
var panGesture: some Gesture {
    DragGesture()
        .onChanged { value in
            canvasState.offset = CGSize(
                width: canvasState.lastOffset.width + value.translation.width,
                height: canvasState.lastOffset.height + value.translation.height
            )
        }
        .onEnded { _ in
            canvasState.lastOffset = canvasState.offset
        }
}

var zoomGesture: some Gesture {
    MagnifyGesture()
        .onChanged { value in
            let newScale = canvasState.lastScale * value.magnification
            canvasState.scale = min(max(newScale, 0.4), 3.0)
        }
        .onEnded { _ in
            canvasState.lastScale = canvasState.scale
        }
}
```

**Double-tap to reset/fit:**
```swift
.onTapGesture(count: 2) {
    withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
        canvasState.resetToFit(activeAgents: activeSessions)
    }
}
```

### Phase 3: Tactical Grid Background

Use SwiftUI `Canvas` + `TimelineView` for an animated grid that responds to zoom level.

```swift
// TacticalGrid.swift

struct TacticalGrid: View {
    let scale: CGFloat

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0/30.0)) { timeline in
            Canvas { context, size in
                let time = timeline.date.timeIntervalSinceReferenceDate
                drawGrid(context: context, size: size, time: time, scale: scale)
            }
        }
    }
}
```

Grid features:
- **Major grid lines** every 200pt (visible at all zoom levels), color: white at 0.04 opacity
- **Minor grid lines** every 50pt (visible only when zoomed > 1.2x), color: white at 0.02 opacity
- **Scanline sweep** -- a subtle horizontal line that sweeps top to bottom every 8 seconds (very low opacity, like a radar sweep). Achieved with a gradient fill on a horizontal strip that moves based on `time`.
- **Coordinate markers** at grid intersections when zoomed > 1.5x (like map coordinates)
- **Edge vignette** -- radial gradient from clear center to dark edges, giving a CRT/radar feel

### Phase 4: Project Base Nodes (rewrite `ProjectBase`)

Transform from card-in-grid to a "building on the map" visual.

```swift
// ProjectBaseNode.swift (extracted from ActivityView)

struct ProjectBaseNode: View {
    let group: ProjectGroup
    let position: CGPoint      // canvas position
    let isSelected: Bool
    let canvasScale: CGFloat
    let onTap: () -> Void

    var body: some View {
        VStack(spacing: 6) {
            // Hexagonal or rounded platform shape
            baseShape

            // Project name label
            Text(group.name.uppercased())
                .font(.code(.caption2, weight: .bold))
                .tracking(2)
                .foregroundStyle(color)

            // Agent count badge
            if group.sessions.count > 0 {
                Text("\(group.sessions.count) UNIT\(group.sessions.count == 1 ? "" : "S")")
                    .font(.code(size: 8, weight: .medium))
                    .foregroundStyle(color.opacity(0.5))
            }

            // Snippet (only when zoomed enough)
            if canvasScale > 0.8, !group.latestSnippet.isEmpty {
                BaseSnippetTicker(text: group.latestSnippet, color: color)
                    .frame(maxWidth: 140)
            }
        }
        .position(position)
        .onTapGesture { onTap() }
    }

    private var baseShape: some View {
        // Hexagonal platform with pulsing glow for active bases
        ZStack {
            // Ground shadow
            Ellipse()
                .fill(color.opacity(0.05))
                .frame(width: 100, height: 40)
                .offset(y: 20)

            // Main platform
            HexagonShape()
                .fill(Color(white: 0.06))
                .frame(width: 80, height: 80)
                .overlay {
                    HexagonShape()
                        .stroke(color.opacity(hasActive ? 0.5 : 0.15), lineWidth: 1.5)
                }
                .shadow(color: hasActive ? color.opacity(0.3) : .clear, radius: 20)
        }
    }
}
```

### Phase 5: Free-Roaming Agent Nodes (rewrite `AgentNode`)

The biggest behavioral change -- agents now position in canvas space.

```swift
struct CanvasAgentNode: View {
    let session: SessionInfo
    let homeBase: CGPoint          // their project's base position
    let canvasScale: CGFloat
    let teamColor: Color

    @State private var position: CGPoint = .zero
    @State private var animating = false

    // Agent size scales inversely with zoom (so they stay readable when zoomed out)
    private var nodeSize: CGFloat {
        max(24, 30 / max(canvasScale, 0.5))
    }

    var body: some View {
        ZStack {
            // Sub-agent cloud (if any)
            if !session.subAgentTasks.isEmpty {
                SubAgentCloud(tasks: session.subAgentTasks, teamColor: teamColor)
            }

            // Connection line back to base (dashed)
            // Drawn as a Path from .zero to base offset

            // The agent circle with activity ring
            agentCircle
        }
        .position(position)
        .animation(.spring(response: 0.8, dampingFraction: 0.65), value: position)
        .onChange(of: session.displayActivityType) { _, newType in
            position = calculatePosition(activity: newType, home: homeBase)
        }
        .onAppear {
            position = calculatePosition(activity: session.displayActivityType, home: homeBase)
            animating = true
        }
    }

    private func calculatePosition(activity: ActivityType, home: CGPoint) -> CGPoint {
        let direction = activityDirection(activity)
        let distance = activityDistance(activity)
        return CGPoint(
            x: home.x + direction.dx * distance,
            y: home.y + direction.dy * distance
        )
    }
}
```

**Activity direction/distance mappings:**
| ActivityType | Direction (angle from base) | Distance (pts) | Notes |
|---|---|---|---|
| .thinking | Orbiting (rotates) | 40-60 | Gentle orbit animation |
| .writing | 210 degrees (lower-left) | 80-120 | "Workshop" zone |
| .searching | 330 degrees (upper-right) | 100-140 | "Recon" zone |
| .running | 150 degrees (lower-right) | 70-100 | "Operations" zone |
| .building | 180 degrees (right) | 90-110 | Near "Operations" |
| .testing | 120 degrees (below) | 80-100 | "Testing range" |
| .approval | Toward canvas center | 60-80 | Moves toward HQ |
| .input | Toward canvas center | 70-90 | Same as approval |
| .sleeping | 0 | 0 | Returns to base |
| .completed | 0 | 10 | Nearly at base, slight offset |
| .stopped | 0 | 5 | At base |
| .errored | Random | 30-50 | "Fallen" slightly away |
| .dead | Random | 40 | Same area as errored |
| .working | 270 degrees (up) | 50-70 | Generic active zone |
| .registered | 0 | 0 | At base, just arrived |

When multiple agents from the same project have the same activity type, they fan out with angular offsets of ~20 degrees between each.

### Phase 6: Connection Lines

Draw thin lines from each agent back to their home base. These lines make it visually obvious which base an agent belongs to, especially when agents are far from their base.

```swift
struct ConnectionLines: View {
    let sessions: [SessionInfo]
    let basePositions: [String: CGPoint]
    let agentPositions: [String: CGPoint]  // session ID -> canvas position

    var body: some View {
        Canvas { context, size in
            for session in sessions {
                guard let base = basePositions[session.project],
                      let agent = agentPositions[session.id] else { continue }

                let distance = hypot(agent.x - base.x, agent.y - base.y)
                guard distance > 20 else { continue }  // don't draw tiny lines

                var path = Path()
                path.move(to: base)
                path.addLine(to: agent)

                let color = teamColor(for: session.project)
                context.stroke(
                    path,
                    with: .color(color.opacity(0.15)),
                    style: StrokeStyle(lineWidth: 1, dash: [4, 4])
                )
            }
        }
    }
}
```

### Phase 7: Minimap Overlay

A small (80x80) overview in the bottom-right corner showing the full canvas with colored dots for bases and a viewport rectangle.

```swift
struct CanvasMinimap: View {
    let basePositions: [String: CGPoint]
    let agentPositions: [String: CGPoint]
    let canvasSize: CGSize
    let viewportRect: CGRect    // current visible area in canvas coords
    let onTap: (CGPoint) -> Void  // tap to jump to location

    private let minimapSize: CGFloat = 80

    var body: some View {
        Canvas { context, size in
            let scaleX = size.width / canvasSize.width
            let scaleY = size.height / canvasSize.height

            // Draw base dots
            for (project, pos) in basePositions {
                let mapped = CGPoint(x: pos.x * scaleX, y: pos.y * scaleY)
                let color = teamColor(for: project)
                context.fill(Circle().path(in: CGRect(x: mapped.x - 3, y: mapped.y - 3, width: 6, height: 6)),
                            with: .color(color))
            }

            // Draw viewport rectangle
            let vr = CGRect(
                x: viewportRect.minX * scaleX,
                y: viewportRect.minY * scaleY,
                width: viewportRect.width * scaleX,
                height: viewportRect.height * scaleY
            )
            context.stroke(Path(vr), with: .color(.white.opacity(0.5)), lineWidth: 1)
        }
        .frame(width: minimapSize, height: minimapSize)
        .background(Color(white: 0.05).opacity(0.8))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay {
            RoundedRectangle(cornerRadius: 8)
                .stroke(.white.opacity(0.1), lineWidth: 1)
        }
        .onTapGesture { location in
            let canvasX = location.x / minimapSize * canvasSize.width
            let canvasY = location.y / minimapSize * canvasSize.height
            onTap(CGPoint(x: canvasX, y: canvasY))
        }
    }
}
```

### Phase 8: Activity Zone Labels

Faded text labels on the canvas that indicate conceptual zones. These are purely cosmetic -- they help the user build a mental map of where different activity types cluster.

```swift
struct ActivityZoneLabels: View {
    let scale: CGFloat
    let canvasCenter: CGPoint

    // Only show when zoomed out enough to see the big picture
    var body: some View {
        if scale < 1.5 {
            Group {
                zoneLabel("RECON", at: CGPoint(x: canvasCenter.x + 200, y: canvasCenter.y - 180))
                zoneLabel("WORKSHOP", at: CGPoint(x: canvasCenter.x - 200, y: canvasCenter.y + 120))
                zoneLabel("OPS", at: CGPoint(x: canvasCenter.x + 180, y: canvasCenter.y + 100))
                zoneLabel("TESTING", at: CGPoint(x: canvasCenter.x + 100, y: canvasCenter.y + 200))
                zoneLabel("HQ", at: canvasCenter)
            }
            .opacity(max(0, 1 - scale) * 0.3)  // fade as zoom increases
        }
    }

    private func zoneLabel(_ text: String, at position: CGPoint) -> some View {
        Text(text)
            .font(.code(size: 14, weight: .bold))
            .tracking(6)
            .foregroundStyle(.white.opacity(0.08))
            .position(position)
    }
}
```

## File Changes Summary

### New Files
| File | Purpose |
|---|---|
| `ios/VoxHerd/VoxHerd/UI/CanvasState.swift` | Canvas camera state (@Observable), position calculations, layout algorithm |
| `ios/VoxHerd/VoxHerd/UI/TacticalGrid.swift` | Animated grid background using Canvas + TimelineView |
| `ios/VoxHerd/VoxHerd/UI/ProjectBaseNode.swift` | Individual project base "building" on the canvas |
| `ios/VoxHerd/VoxHerd/UI/CanvasAgentNode.swift` | Free-roaming agent circle with activity ring + sub-agents |
| `ios/VoxHerd/VoxHerd/UI/ConnectionLines.swift` | Canvas-drawn dashed lines from agents to their home base |
| `ios/VoxHerd/VoxHerd/UI/CanvasMinimap.swift` | Small overview map in corner |
| `ios/VoxHerd/VoxHerd/UI/HexagonShape.swift` | Reusable hexagon Shape for base platforms |

### Modified Files
| File | Changes |
|---|---|
| `ios/VoxHerd/VoxHerd/UI/ActivityView.swift` | Complete rewrite: replace ScrollView+LazyVGrid with ZStack canvas, integrate gesture handling, compose new sub-views |

### Unchanged Files
| File | Why |
|---|---|
| `Models.swift` | No model changes needed -- existing types are sufficient |
| `AppState.swift` | No changes -- ActivityView still reads from `state.sessions` and `state.activeProject` |
| `SessionRegistry.swift` | No changes |
| `DashboardView.swift` | No changes -- ActivityView is navigated to via NavigationLink, interface stays the same |

## Technical Decisions

### Why not SwiftUI `Canvas` for everything?
SwiftUI `Canvas` (the drawing primitive) is great for the grid background and connection lines where we need hundreds of lines rendered efficiently. But for interactive elements (bases, agents), we want proper SwiftUI views because:
- They get tap gestures, accessibility, animations for free
- `.position()` modifier works naturally with coordinate transforms
- `.scaleEffect()` and `.offset()` on the parent ZStack handles the camera transform

### Why not a UIScrollView wrapper?
UIScrollView with `minimumZoomScale`/`maximumZoomScale` is tempting but:
- Harder to overlay the HUD and minimap as fixed elements
- SwiftUI gesture composition is cleaner for our use case
- We need custom zoom anchoring (zoom toward pinch center)
- We want animated transitions when resetting view, which is harder with UIScrollView

### Why deterministic spiral layout instead of force-directed?
- Force-directed layouts jitter and require iterative settling, which causes visual instability
- Projects shouldn't move when other projects appear/disappear
- Spiral layout with name-hashing gives stable, reproducible positions
- Simple to implement, easy to reason about

### Canvas size
- Fixed 2000x2000 canvas for now (iPhone screen ~390x844 visible at 1x)
- At default zoom (1.0x), about 30% of canvas is visible
- At min zoom (0.4x), almost entire canvas is visible
- This gives plenty of room for ~20 projects without crowding

### Performance considerations
- `Canvas` view for grid and connection lines (GPU-accelerated drawing)
- `TimelineView` with `.animation(minimumInterval: 1.0/30.0)` for grid scanline (not 60fps -- too expensive for decoration)
- Agent position changes go through SwiftUI animation system (not manual frame updates)
- Base positions are computed once and cached (not recomputed every frame)
- Minimap uses `Canvas` for efficient rendering of many dots

## Phased Rollout

### Sprint 1 (Core Canvas)
1. Create `CanvasState.swift` with camera transform + base position algorithm
2. Create `TacticalGrid.swift` with basic grid (no scanline yet)
3. Rewrite `ActivityView.swift` body to use ZStack + gesture handling
4. Move existing `ProjectBase` to use `.position()` with canvas coordinates
5. Verify pan/zoom works, HUD stays fixed

### Sprint 2 (Free-Roaming Agents)
1. Create `CanvasAgentNode.swift` with canvas-space positioning
2. Implement activity direction/distance calculations
3. Create `ConnectionLines.swift`
4. Extract `ProjectBaseNode.swift` with hexagonal visual
5. Wire up agent position updates on activity type changes

### Sprint 3 (Polish)
1. Create `CanvasMinimap.swift`
2. Add `ActivityZoneLabels.swift`
3. Add scanline animation to `TacticalGrid`
4. Add double-tap to fit
5. Add edge vignette / fog effect
6. Tune all animation springs and timing
7. Test with 1, 3, 5, 10, 20 projects

### Sprint 4 (Refinement)
1. Add agent info popup on tap
2. Add "zoom to project" gesture (long press on minimap dot)
3. Performance profiling with Instruments (Core Animation + Time Profiler)
4. Accessibility audit (VoiceOver labels for bases and agents)
5. Haptic feedback on base tap, zoom boundaries

## Testing Strategy

### Manual Testing Matrix
- [ ] 0 sessions: empty state with just the grid
- [ ] 1 project, 1 agent: single base, agent movement
- [ ] 1 project, 3 agents: fan-out behavior when same activity type
- [ ] 3 projects: base spacing, different colors
- [ ] 10 projects: canvas feels spacious, not cramped
- [ ] 20 projects: performance acceptable at 0.4x zoom
- [ ] Pan to edge of canvas: no over-scroll past bounds
- [ ] Zoom in to 3x: agent details visible, grid subdivisions appear
- [ ] Zoom out to 0.4x: all bases visible, minimap viewport accurate
- [ ] Double-tap: animates to fit view
- [ ] Tap base: voice activation works (same as current)
- [ ] Agent moves when activityType changes: spring animation, connection line follows
- [ ] Sub-agents orbit around moved agent
- [ ] Rotate device: canvas redraws correctly

### XCTest (Unit)
- `CanvasState` position calculations: verify deterministic layout
- `activityDirection` / `activityDistance` mappings
- Base position stability: adding a new project doesn't move existing ones
- Zoom clamp: verify scale stays within 0.4...3.0
- Pan bounds: verify offset doesn't exceed canvas edges

## Open Questions

1. **Should bases be draggable?** Users could rearrange their map manually. This adds complexity (persisting positions) but is very satisfying in canvas UIs. Recommendation: defer to Phase 2 -- auto-layout first, manual override later.

2. **Should we persist canvas camera position?** When navigating away and back, should it remember zoom/pan? Recommendation: yes, store in `CanvasState` which lives on `AppState` (not `@State` in the view). This way it persists during the session but resets on app restart.

3. **Should agent "thinking" be an orbit animation?** The current thinking ring pulses. On the canvas, agents in thinking state could slowly orbit their base at a small radius, giving a pacing/contemplating visual. Recommendation: yes, use `TimelineView` with circular path for thinking agents.

4. **Edge glow vs fog of war for inactive areas?** Full fog of war (Metal shader) is visually striking but adds complexity. A simple radial vignette (dark edges) achieves 80% of the effect. Recommendation: start with vignette, consider Metal shader in Phase 2 if desired.
