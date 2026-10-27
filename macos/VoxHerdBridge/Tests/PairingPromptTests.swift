import AppKit
import XCTest
@testable import VoxHerdBridge

/// Unit tests for the pairing card the menu bar shows while a headset waits.
///
/// The card exists because the two places the code used to appear both fail:
/// the `osascript` notification is a banner you can look away from, and the
/// log line — under `/Applications/VoxHerdBridge.app` the bridge runs
/// `--headless`, so its log is a pipe into this app's 200-entry event list —
/// is pushed out of sight by whatever happens next.
///
/// What is tested here is `PairingPrompts`, which decides everything that can
/// go wrong quietly: a dead code left on screen (the wearer keys it in), a
/// stranger's device name rendered as-is, a line from a bridge built at a
/// different time than this app. `BridgeProcessManager` only hands this a
/// decoded line and a clock; the wiring around it — which decoder claims a
/// line, and the task that takes a card down on its deadline — is in
/// `BridgePairingLineTests` below. What nothing here covers is SwiftUI
/// drawing the card, which only eyes on the menu bar can confirm.
final class PairingPromptTests: XCTestCase {

    // MARK: - Helpers

    /// A payload built by decoding, so the tests go through the same
    /// `CodingKeys` the bridge's JSON does.
    private static func payload(
        state: String = "pending",
        requestId: String = "req-1",
        code: String? = "471 902",
        device: String? = "Test Headset",
        host: String? = "192.168.1.50",
        expiresIn: Double? = 120,
        attempts: Int? = 0,
        reason: String? = nil
    ) -> PairingLine.Payload {
        var json: [String: Any] = ["state": state, "request_id": requestId]
        if let code { json["code"] = code }
        if let device { json["device"] = device }
        if let host { json["host"] = host }
        if let expiresIn { json["expires_in"] = expiresIn }
        if let attempts { json["attempts"] = attempts }
        if let reason { json["reason"] = reason }
        let data = try! JSONSerialization.data(withJSONObject: ["pairing": json])
        return try! JSONDecoder().decode(PairingLine.self, from: data).pairing
    }

    private let t0 = Date(timeIntervalSince1970: 1_700_000_000)

    // MARK: - The line off the pipe

    func testAPendingLineDecodesEveryFieldTheCardShows() {
        let line = """
        {"pairing":{"state":"pending","request_id":"abc","code":"471 902",\
        "device":"Test Headset","host":"192.168.1.50","expires_in":120.0,"attempts":0}}
        """
        let decoded = try! JSONDecoder().decode(PairingLine.self, from: Data(line.utf8))
        XCTAssertEqual(decoded.pairing.state, "pending")
        XCTAssertEqual(decoded.pairing.requestId, "abc")
        XCTAssertEqual(decoded.pairing.code, "471 902")
        XCTAssertEqual(decoded.pairing.device, "Test Headset")
        XCTAssertEqual(decoded.pairing.host, "192.168.1.50")
        XCTAssertEqual(decoded.pairing.expiresIn, 120)
        XCTAssertEqual(decoded.pairing.attempts, 0)
    }

    func testALogLineIsNotAPairingLine() {
        // All three decoders read the same pipe, so none may claim another's
        // line. The log entry and the ready signal have no `pairing` key.
        let log = #"{"ts":"2026-10-25T09:00:00-07:00","level":"warning","project":"bridge","message":"Pairing code for Test Headset (192.168.1.50): 471 902"}"#
        let ready = #"{"status":"ready","port":7777}"#
        XCTAssertNil(try? JSONDecoder().decode(PairingLine.self, from: Data(log.utf8)))
        XCTAssertNil(try? JSONDecoder().decode(PairingLine.self, from: Data(ready.utf8)))
    }

    func testAPairingLineIsNotALogEntryOrAReadySignal() {
        let pairing = #"{"pairing":{"state":"pending","request_id":"abc","code":"471 902","expires_in":120}}"#
        XCTAssertNil(try? JSONDecoder().decode(LogEntry.self, from: Data(pairing.utf8)))
    }

    func testALineMissingTheFieldsThisBuildDoesNotNeedStillDecodes() {
        // The bundled bridge binary is rebuilt separately from this app, so
        // the line crosses a version boundary. A card dropped whole because a
        // key was added or removed is the failure this feature exists to fix.
        let sparse = #"{"pairing":{"state":"resolved","request_id":"abc","reason":"paired","future_field":7}}"#
        let decoded = try? JSONDecoder().decode(PairingLine.self, from: Data(sparse.utf8))
        XCTAssertEqual(decoded?.pairing.state, "resolved")
        XCTAssertEqual(decoded?.pairing.reason, "paired")
        XCTAssertNil(decoded?.pairing.code)
    }

    // MARK: - A card appears

    func testAPendingLineRaisesACardWithTheCodeDeviceAddressAndDeadline() {
        let prompts = PairingPrompts.apply(Self.payload(), to: [], now: t0)
        XCTAssertEqual(prompts.count, 1)
        XCTAssertEqual(prompts[0].requestId, "req-1")
        XCTAssertEqual(prompts[0].code, "471 902")
        XCTAssertEqual(prompts[0].device, "Test Headset")
        XCTAssertEqual(prompts[0].host, "192.168.1.50")
        XCTAssertEqual(prompts[0].expiresAt, t0.addingTimeInterval(120))
        XCTAssertEqual(prompts[0].attempts, 0)
    }

    func testTheCountdownReadsAsAClock() {
        let prompts = PairingPrompts.apply(Self.payload(), to: [], now: t0)
        XCTAssertEqual(prompts[0].countdown(at: t0), "2:00")
        XCTAssertEqual(prompts[0].countdown(at: t0.addingTimeInterval(13)), "1:47")
        XCTAssertEqual(prompts[0].countdown(at: t0.addingTimeInterval(119.5)), "0:01")
        XCTAssertEqual(prompts[0].countdown(at: t0.addingTimeInterval(200)), "0:00")
    }

    func testThreeHeadsetsCanWaitAtOnceAndAFourthDoesNotGrowTheList() {
        // `pairing.MAX_PENDING` is three, so a fourth is a bug or a forgery.
        var prompts: [PairingPrompt] = []
        for i in 1...5 {
            prompts = PairingPrompts.apply(Self.payload(requestId: "req-\(i)"), to: prompts, now: t0)
        }
        XCTAssertEqual(prompts.count, PairingPrompts.maxShown)
        XCTAssertEqual(prompts.map(\.requestId), ["req-3", "req-4", "req-5"])
    }

    // MARK: - A card goes away

    func testEveryWayARequestEndsTakesItsCardDown() {
        // Redeemed, expired, out of tries, locked out — the code stopped
        // working in all four, so the card goes in all four.
        for reason in ["paired", "expired", "failed", "locked"] {
            let pending = PairingPrompts.apply(Self.payload(), to: [], now: t0)
            let resolved = PairingPrompts.apply(
                Self.payload(state: "resolved", code: nil, expiresIn: nil, reason: reason),
                to: pending, now: t0.addingTimeInterval(5)
            )
            XCTAssertTrue(resolved.isEmpty, "a \(reason) request left its card up")
        }
    }

    func testResolvingOneRequestLeavesTheOthersAlone() {
        var prompts = PairingPrompts.apply(Self.payload(requestId: "a"), to: [], now: t0)
        prompts = PairingPrompts.apply(Self.payload(requestId: "b"), to: prompts, now: t0)
        prompts = PairingPrompts.apply(
            Self.payload(state: "resolved", requestId: "a", reason: "paired"),
            to: prompts, now: t0
        )
        XCTAssertEqual(prompts.map(\.requestId), ["b"])
    }

    func testACardComesDownOnItsOwnDeadline() {
        // The bridge never says so for a request nobody answers: `pairing.py`
        // sweeps its book only when another pairing call arrives. A card that
        // waited to be told would outlive the code printed on it.
        let prompts = PairingPrompts.apply(Self.payload(), to: [], now: t0)
        XCTAssertEqual(PairingPrompts.pruned(prompts, now: t0.addingTimeInterval(119)).count, 1)
        XCTAssertTrue(PairingPrompts.pruned(prompts, now: t0.addingTimeInterval(120)).isEmpty)
        XCTAssertTrue(PairingPrompts.pruned(prompts, now: t0.addingTimeInterval(500)).isEmpty)
    }

    func testACardThatIsAlreadyDeadNeverGoesUp() {
        // A `pending` line delayed in the pipe, or a bridge whose clock has
        // moved on. Either way there is nothing worth showing.
        XCTAssertTrue(PairingPrompts.apply(Self.payload(expiresIn: 0), to: [], now: t0).isEmpty)
        XCTAssertTrue(PairingPrompts.apply(Self.payload(expiresIn: -5), to: [], now: t0).isEmpty)
        XCTAssertTrue(PairingPrompts.apply(Self.payload(expiresIn: nil), to: [], now: t0).isEmpty)
    }

    func testAStateThisBuildDoesNotKnowLeavesTheScreenAsItIs() {
        let pending = PairingPrompts.apply(Self.payload(), to: [], now: t0)
        let after = PairingPrompts.apply(
            Self.payload(state: "superseded", requestId: "req-1"), to: pending, now: t0
        )
        XCTAssertEqual(after, pending)
    }

    // MARK: - Wrong codes

    func testAWrongTryUpdatesTheCardWithoutRestartingItsClock() {
        // The re-announced line carries the time left, not a fresh two
        // minutes, and the progress bar's full width is what the code was
        // ever good for — so it must keep shrinking across the update.
        var prompts = PairingPrompts.apply(Self.payload(), to: [], now: t0)
        let issued = prompts[0].issuedAt
        prompts = PairingPrompts.apply(
            Self.payload(expiresIn: 110, attempts: 1),
            to: prompts, now: t0.addingTimeInterval(10)
        )
        XCTAssertEqual(prompts.count, 1, "a wrong try must not raise a second card")
        XCTAssertEqual(prompts[0].attempts, 1)
        XCTAssertEqual(prompts[0].issuedAt, issued)
        XCTAssertEqual(prompts[0].expiresAt, t0.addingTimeInterval(120))
        XCTAssertEqual(prompts[0].lifetime, 120)
    }

    func testANegativeAttemptCountCannotMakeTheWarningVanishOrInvert() {
        let prompts = PairingPrompts.apply(Self.payload(attempts: -3), to: [], now: t0)
        XCTAssertEqual(prompts[0].attempts, 0)
    }

    // MARK: - Untrusted text

    func testACodeIsDigitsAndSpacesOrItIsNotShownAtAll() {
        XCTAssertEqual(PairingPrompts.readableCode("471 902"), "471 902")
        XCTAssertEqual(PairingPrompts.readableCode("471902"), "471902")
        // Not repaired into something plausible — distrusted.
        XCTAssertNil(PairingPrompts.readableCode("471-902"))
        XCTAssertNil(PairingPrompts.readableCode("4a1 902"))
        XCTAssertNil(PairingPrompts.readableCode("   "))
        XCTAssertNil(PairingPrompts.readableCode(""))
        XCTAssertNil(PairingPrompts.readableCode(" 471 902"))
        XCTAssertNil(PairingPrompts.readableCode(String(repeating: "1", count: 64)))
        XCTAssertNil(PairingPrompts.readableCode(nil))
    }

    func testAPendingLineWithNoReadableCodeRaisesNoCard() {
        // An empty card is worse than none: it is a thing the wearer will
        // try to read a code off.
        XCTAssertTrue(PairingPrompts.apply(Self.payload(code: nil), to: [], now: t0).isEmpty)
        XCTAssertTrue(PairingPrompts.apply(Self.payload(code: "⚠️"), to: [], now: t0).isEmpty)
    }

    func testADeviceNameIsCutTheWayTheBridgeCutsIt() {
        // Mirrors `pairing.device_name`. The bridge has already done this;
        // this process is the one that renders the result.
        XCTAssertEqual(PairingPrompts.readableDevice("Test Headset"), "Test Headset")
        XCTAssertEqual(PairingPrompts.readableDevice("Headset\" & beep & \""), "Headset beep")
        XCTAssertEqual(PairingPrompts.readableDevice("Headset\n\nOne"), "HeadsetOne")
        XCTAssertEqual(PairingPrompts.readableDevice("  spaced   out  "), "spaced out")
        XCTAssertEqual(
            PairingPrompts.readableDevice(String(repeating: "x", count: 200)),
            String(repeating: "x", count: PairingPrompts.maxDeviceChars)
        )
        XCTAssertEqual(PairingPrompts.readableDevice("日本語"), PairingPrompts.unknownDevice)
        XCTAssertEqual(PairingPrompts.readableDevice(nil), PairingPrompts.unknownDevice)
        XCTAssertEqual(PairingPrompts.readableDevice(""), PairingPrompts.unknownDevice)
    }

    func testADeviceNameCannotBeDressedUpAsAnAddress() {
        // The address is the one field a stranger cannot choose, which is the
        // whole reason it is on the card. A device name that could pass for
        // one would undo that.
        let prompts = PairingPrompts.apply(
            Self.payload(device: "Headset (asked from 10.0.0.1)"), to: [], now: t0
        )
        XCTAssertEqual(prompts[0].device, "Headset asked from 10.0.0.1")
        XCTAssertFalse(prompts[0].device.contains("("))
    }

    func testAnAddressKeepsWhatAddressesAreMadeOf() {
        XCTAssertEqual(PairingPrompts.readableHost("192.168.1.50"), "192.168.1.50")
        XCTAssertEqual(PairingPrompts.readableHost("fe80::1%en0"), "fe80::1%en0")
        XCTAssertEqual(PairingPrompts.readableHost("headset.local"), "headset.local")
        XCTAssertEqual(PairingPrompts.readableHost("10.0.0.1 <b>"), "10.0.0.1b")
        XCTAssertEqual(
            PairingPrompts.readableHost(String(repeating: "9", count: 200)).count,
            PairingPrompts.maxHostChars
        )
        XCTAssertEqual(PairingPrompts.readableHost(nil), PairingPrompts.unknownHost)
        XCTAssertEqual(PairingPrompts.readableHost(""), PairingPrompts.unknownHost)
    }
}

/// The fold from a real stdout line to what the menu bar holds.
///
/// `PairingPromptTests` covers the decision; this covers the wiring around it
/// — which of the three decoders claims a line, and the 1 s task that takes a
/// card down when its code dies. Both are invisible from outside the class,
/// and both were going to be verified by eye otherwise.
@MainActor
final class BridgePairingLineTests: XCTestCase {

    /// Inert: `BridgeProcessManager()` spawns nothing until `start()`.
    private func manager() -> BridgeProcessManager { BridgeProcessManager() }

    func testAPairingLineOffTheWireRaisesACard() {
        let pm = manager()
        pm.processLine(#"{"pairing":{"state":"pending","request_id":"abc","code":"471 902","device":"Test Headset","host":"192.168.1.50","expires_in":120,"attempts":0}}"#)
        XCTAssertEqual(pm.pairingPrompts.count, 1)
        XCTAssertEqual(pm.pairingPrompts[0].code, "471 902")
        XCTAssertEqual(pm.pairingPrompts[0].host, "192.168.1.50")
        // And it is not also filed as a log entry.
        XCTAssertTrue(pm.recentEvents.isEmpty)
    }

    func testTheLogLineCarryingTheSameCodeIsStillJustALogLine() {
        // The bridge logs the code too, and that log line is where the code
        // used to go to die. It must not be mistaken for a card.
        let pm = manager()
        pm.processLine(#"{"ts":"2026-10-25T09:00:00-07:00","level":"warning","project":"bridge","message":"Pairing code for Test Headset (192.168.1.50): 471 902 -- expires in 2 minutes"}"#)
        XCTAssertTrue(pm.pairingPrompts.isEmpty)
        XCTAssertEqual(pm.recentEvents.count, 1)
    }

    func testAResolvedLineTakesTheCardDown() {
        let pm = manager()
        pm.processLine(#"{"pairing":{"state":"pending","request_id":"abc","code":"471 902","expires_in":120}}"#)
        XCTAssertEqual(pm.pairingPrompts.count, 1)
        pm.processLine(#"{"pairing":{"state":"resolved","request_id":"abc","reason":"paired"}}"#)
        XCTAssertTrue(pm.pairingPrompts.isEmpty)
    }

    func testACardComesDownOnItsOwnWhenNothingElseArrives() async throws {
        // The bridge sends no `resolved` line for a request nobody answers,
        // so this is the only thing that clears it. Real seconds, because the
        // task that does it is a real 1 s loop.
        let pm = manager()
        pm.processLine(#"{"pairing":{"state":"pending","request_id":"abc","code":"471 902","expires_in":1.0}}"#)
        XCTAssertEqual(pm.pairingPrompts.count, 1)
        try await Task.sleep(for: .seconds(2.5))
        XCTAssertTrue(pm.pairingPrompts.isEmpty, "the card outlived its code")
    }

    func testStoppingTheBridgeTakesEveryCardDown() {
        // A bridge that is gone cannot honour a code it handed out.
        let pm = manager()
        pm.processLine(#"{"pairing":{"state":"pending","request_id":"abc","code":"471 902","expires_in":120}}"#)
        pm.stop()
        XCTAssertTrue(pm.pairingPrompts.isEmpty)
    }

    func testAFreshBridgeStartsWithNoCards() {
        // The ready signal means a new process with an empty book, so
        // anything left on screen is from the last one.
        let pm = manager()
        pm.processLine(#"{"pairing":{"state":"pending","request_id":"abc","code":"471 902","expires_in":120}}"#)
        pm.processLine(#"{"status":"ready","port":7777}"#)
        XCTAssertTrue(pm.pairingPrompts.isEmpty)
    }
}

/// The menu bar glyph, and which claim on it wins.
final class MenuBarIconTests: XCTestCase {

    func testAWaitingPairingRequestTakesTheIcon() {
        XCTAssertEqual(
            MenuBarIcon.name(pairingWaiting: true, walkAroundActive: false, bridgeRunning: true),
            "person.badge.key.fill"
        )
    }

    func testPairingOutranksWalkAround() {
        // The two coincide: Walk-Around is what you turn on to walk about
        // with the glasses, which is when you pair them. A code is gone in
        // two minutes; the walking glyph comes back when the code does.
        XCTAssertEqual(
            MenuBarIcon.name(pairingWaiting: true, walkAroundActive: true, bridgeRunning: true),
            "person.badge.key.fill"
        )
    }

    func testWithNothingPendingTheIconIsWhatItAlwaysWas() {
        XCTAssertEqual(
            MenuBarIcon.name(pairingWaiting: false, walkAroundActive: true, bridgeRunning: true),
            "figure.walk.circle.fill"
        )
        XCTAssertEqual(
            MenuBarIcon.name(pairingWaiting: false, walkAroundActive: false, bridgeRunning: true),
            "waveform.circle.fill"
        )
        XCTAssertEqual(
            MenuBarIcon.name(pairingWaiting: false, walkAroundActive: false, bridgeRunning: false),
            "waveform.circle"
        )
    }

    func testEveryGlyphExists() {
        // A name macOS cannot resolve renders as nothing at all, which in the
        // menu bar is an empty space the user cannot click deliberately.
        for pairing in [true, false] {
            for walking in [true, false] {
                for running in [true, false] {
                    let name = MenuBarIcon.name(
                        pairingWaiting: pairing, walkAroundActive: walking, bridgeRunning: running
                    )
                    XCTAssertNotNil(
                        NSImage(systemSymbolName: name, accessibilityDescription: nil),
                        "no such SF Symbol: \(name)"
                    )
                }
            }
        }
    }
}
