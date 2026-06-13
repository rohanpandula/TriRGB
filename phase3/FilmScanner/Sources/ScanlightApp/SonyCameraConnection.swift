// SonyCameraConnection - shared SDK camera readiness state.
//
// Saved IP/auth values are configuration only. This object is the single
// app-wide source of truth for whether the camera has actually been probed
// through sony-capture for the currently configured credentials.

import Foundation
import SwiftUI

@MainActor
final class SonyCameraConnection: ObservableObject {

    enum Phase: Equatable {
        case notUsed
        case notConfigured([String])
        case configured
        case checking
        case online(message: String, ip: String, verifiedAt: Date)
        case offline(message: String)
        case stale(message: String)
    }

    @Published private(set) var phase: Phase = .configured

    private let checkTimeout: TimeInterval

    private struct Signature: Equatable {
        var transport: String
        var ip: String
        var mac: String
        var user: String
        var password: String

        init(_ settings: ScanSettings) {
            transport = settings.sonyTransportMode
            ip = Self.clean(settings.sonyIpAddress)
            mac = Self.clean(settings.sonyMacAddress)
            user = Self.clean(settings.sonyUser)
            password = Self.clean(settings.sonyPassword)
        }

        private static func clean(_ value: String?) -> String {
            (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    private var lastResult: SonyConnectionProbeResult?
    private var lastResultAt: Date?
    private var lastSignature: Signature?
    private var checkGeneration: UInt64 = 0

    init(checkTimeout: TimeInterval = 25) {
        self.checkTimeout = checkTimeout
    }

    var isChecking: Bool {
        if case .checking = phase { return true }
        return false
    }

    var isOnline: Bool {
        if case .online = phase { return true }
        return false
    }

    var tint: Color {
        switch phase {
        case .notUsed, .configured:
            return Theme.State.idle
        case .notConfigured, .stale:
            return Theme.State.warning
        case .checking:
            return Theme.State.info
        case .online:
            return Theme.State.success
        case .offline:
            return Theme.State.danger
        }
    }

    var statusText: String {
        switch phase {
        case .notUsed:
            return "not used by current trigger"
        case .notConfigured:
            return "camera not set up"
        case .configured:
            return "configured - not verified"
        case .checking:
            return "checking camera..."
        case .online(_, let ip, _):
            return "online - \(ip)"
        case .offline:
            return "camera unreachable"
        case .stale:
            return "needs re-check"
        }
    }

    var chipText: String {
        switch phase {
        case .notUsed:
            return "NOT USED"
        case .notConfigured:
            return "MISSING"
        case .configured:
            return "NOT VERIFIED"
        case .checking:
            return "CHECKING"
        case .online:
            return "ONLINE"
        case .offline:
            return "OFFLINE"
        case .stale:
            return "STALE"
        }
    }

    var detailText: String {
        switch phase {
        case .notUsed:
            return "The current trigger mode does not use the Sony SDK."
        case .notConfigured(let missing):
            return "Add \(missing.joined(separator: ", ")) in Set Up before checking the camera."
        case .configured:
            return "Saved settings only. Check Camera opens a real Sony SDK session before calibration or scanning."
        case .checking:
            return "Opening a short Sony SDK session. This does not fire the shutter. This should finish within \(Int(ceil(checkTimeout))) seconds."
        case .online(let message, _, let verifiedAt):
            return "\(message) Verified \(Self.relativeFormatter.localizedString(for: verifiedAt, relativeTo: Date()))."
        case .offline(let message):
            return message
        case .stale(let message):
            return message
        }
    }

    func update(for settings: ScanSettings) {
        guard settings.triggerMode == "sdk" else {
            phase = .notUsed
            return
        }
        if case .checking = phase {
            return
        }

        let missing = Self.missingFields(in: settings)
        guard missing.isEmpty else {
            lastResult = nil
            lastResultAt = nil
            lastSignature = nil
            phase = .notConfigured(missing)
            return
        }

        let signature = Signature(settings)
        if let lastSignature, lastSignature == signature,
           let lastResult, let lastResultAt {
            let ip = Self.connectionLabel(for: signature)
            phase = lastResult.success
                ? .online(message: lastResult.message, ip: ip, verifiedAt: lastResultAt)
                : .offline(message: lastResult.message)
            return
        }

        if lastResult != nil {
            phase = .stale(message: "Camera settings changed. Check Camera again before SDK capture.")
        } else {
            phase = .configured
        }
    }

    @discardableResult
    func check(store: SettingsStore, orchestratorClient: OrchestratorClient) async -> Bool {
        guard !isChecking else { return false }

        guard store.settings.triggerMode == "sdk" else {
            update(for: store.settings)
            return true
        }

        let missing = Self.missingFields(in: store.settings)
        guard missing.isEmpty else {
            phase = .notConfigured(missing)
            return false
        }

        phase = .checking
        checkGeneration &+= 1
        let generation = checkGeneration

        defer {
            if checkGeneration == generation, case .checking = phase {
                let message = Task.isCancelled
                    ? "Sony connection check was interrupted before it finished. Try Check Camera again."
                    : "Sony connection check did not finish cleanly. Try Check Camera again; close Imaging Edge Remote or live view if either app has the camera connected."
                phase = .offline(message: message)
            }
        }

        let resolved = orchestratorClient.settingsWithResolvedSonyIP(store.settings)
        if !resolved.usesSonyUSB, resolved.sonyIpAddress != store.settings.sonyIpAddress {
            store.settings.sonyIpAddress = resolved.sonyIpAddress
        }

        let signature = Signature(store.settings)
        NSLog("Scanlight Sony Check Camera started for %@", Self.connectionLabel(for: signature))
        let result = await Self.runProbeWithTimeout(
            settings: store.settings,
            orchestratorClient: orchestratorClient,
            timeout: checkTimeout
        )
        guard checkGeneration == generation else { return false }
        let checkedAt = Date()
        lastSignature = signature
        lastResult = result
        lastResultAt = checkedAt
        let ip = Self.connectionLabel(for: signature)
        phase = result.success
            ? .online(message: result.message, ip: ip, verifiedAt: checkedAt)
            : .offline(message: result.message)
        NSLog("Scanlight Sony Check Camera finished: %@", result.success ? "online" : "offline")
        return result.success
    }

    func markOffline(_ message: String, settings: ScanSettings) {
        guard settings.triggerMode == "sdk" else {
            update(for: settings)
            return
        }
        lastSignature = Signature(settings)
        lastResult = SonyConnectionProbeResult(success: false, message: message)
        lastResultAt = Date()
        update(for: settings)
    }

    private static func missingFields(in settings: ScanSettings) -> [String] {
        var missing: [String] = []
        if !settings.usesSonyUSB && clean(settings.sonyIpAddress).isEmpty {
            missing.append("Sony IP")
        }
        if clean(settings.sonyUser).isEmpty {
            missing.append("Access Auth user")
        }
        if clean(settings.sonyPassword).isEmpty {
            missing.append("Access Auth password")
        }
        return missing
    }

    private static func connectionLabel(for signature: Signature) -> String {
        signature.transport == "usb"
            ? "USB"
            : (signature.ip.isEmpty ? "unknown IP" : signature.ip)
    }

    private static func clean(_ value: String?) -> String {
        (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Race the real probe against a hard deadline. The result of whichever
    /// finishes first wins; the loser is abandoned (NOT awaited).
    ///
    /// Why not `withTaskGroup`: the group's body implicitly awaits every
    /// child task on exit. If the probe child is stuck on synchronous
    /// process/pipe I/O that doesn't honour cooperative cancellation, the
    /// timeout child winning the race doesn't unblock the parent — the
    /// group still waits, and `phase` stays `.checking` forever. The
    /// continuation-based resolver below resumes the parent on the first
    /// result and lets the loser finish on its own.
    private static func runProbeWithTimeout(
        settings: ScanSettings,
        orchestratorClient: OrchestratorClient,
        timeout: TimeInterval
    ) async -> SonyConnectionProbeResult {
        let timeoutSeconds = Int(ceil(max(timeout, 0.1)))
        let timeoutResult = SonyConnectionProbeResult(
            success: false,
            message: "Sony connection check did not finish within \(timeoutSeconds) seconds. Try again; close Imaging Edge Remote or live view if either app has the camera connected."
        )

        let resolver = SonyProbeResolver()

        let probeTask = Task.detached(priority: .userInitiated) {
            let result = await orchestratorClient.checkSonyConnection(settings: settings)
            resolver.resolve(with: result)
        }

        let timeoutTask = Task.detached(priority: .userInitiated) {
            let ns = UInt64(max(timeout, 0.1) * 1_000_000_000)
            try? await Task.sleep(nanoseconds: ns)
            resolver.resolve(with: timeoutResult)
        }

        let result = await resolver.value

        // Best-effort: signal both losers to unwind. Cooperative — the
        // probe task may still be sitting on a hung Process; that's fine,
        // it doesn't pin us.
        probeTask.cancel()
        timeoutTask.cancel()

        return result
    }

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter
    }()
}

/// Single-resolution result holder used by `runProbeWithTimeout` to race
/// the real probe against the timeout. Whoever calls `resolve(with:)`
/// first wins; subsequent calls are dropped. `value` resumes the awaiting
/// task as soon as either side resolves — it does NOT wait for the loser
/// to finish, which is how this avoids the stuck-`.checking` regression
/// when the probe is blocked on uncancellable synchronous I/O.
private final class SonyProbeResolver: @unchecked Sendable {
    private let lock = NSLock()
    private var result: SonyConnectionProbeResult?
    private var continuation: CheckedContinuation<SonyConnectionProbeResult, Never>?

    var value: SonyConnectionProbeResult {
        get async {
            await withCheckedContinuation { cont in
                lock.lock()
                if let pending = result {
                    lock.unlock()
                    cont.resume(returning: pending)
                } else {
                    continuation = cont
                    lock.unlock()
                }
            }
        }
    }

    func resolve(with newResult: SonyConnectionProbeResult) {
        lock.lock()
        guard result == nil else {
            lock.unlock()
            return
        }
        result = newResult
        let cont = continuation
        continuation = nil
        lock.unlock()
        cont?.resume(returning: newResult)
    }
}
