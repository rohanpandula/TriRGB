// ScanlightViewModel — @MainActor ObservableObject that owns all driver state
// and mutations. ScanlightView is a pure rendering layer; nothing in the View
// should call Scanlight, FakeTransport, or SerialPortTransport directly.

import Combine
import Darwin
import Foundation
import ScanlightSwift

@MainActor
final class ScanlightViewModel: ObservableObject {

    // MARK: - Published state

    /// The port path field (user-editable; ignored when -FakeTransport YES).
    @Published var port: String = ""

    /// Whether a transport is currently open.
    @Published var isConnected: Bool = false

    /// Human-readable connection status string.
    @Published var connectionStatusString: String = "disconnected"

    /// Firmware version string, e.g. "fw=1 hw=1".
    @Published var firmwareString: String = "—"

    /// Hardware version string, e.g. "hw=1".
    @Published var hardwareString: String = "—"

    /// LED temperature, e.g. "32.50 °C".
    @Published var ledTempString: String = "—"

    /// VBUS voltage, e.g. "5050 mV".
    @Published var vbusString: String = "—"

    /// Red channel slider level (0–255).
    @Published var redLevel: Double = 0

    /// Green channel slider level (0–255).
    @Published var greenLevel: Double = 0

    /// Blue channel slider level (0–255).
    @Published var blueLevel: Double = 0

    /// White channel slider level (0–255).
    @Published var whiteLevel: Double = 0

    /// The four exclusive light channels (drives the switch-style Light tab).
    enum LightChannel { case red, green, blue, white }

    /// Which single channel is currently lit, or nil if all off. Lighting one
    /// channel is exclusive — the firmware outputs one set of values and for
    /// narrowband capture you expose one channel at a time. Drives the on/off
    /// state of the per-channel toggles.
    @Published var activeChannel: LightChannel?

    /// Pulse duration text field (milliseconds).
    @Published var pulseMs: String = "100"

    /// Last error message, or empty string if none.
    @Published var lastError: String = ""

    /// Log lines, newest first; capped at 200 entries.
    @Published var logLines: [String] = []

    /// True when manual light controls can safely send serial commands.
    var manualControlsEnabled: Bool {
        isConnected && portOwner == .idle
    }

    // MARK: - Port-ownership guard (Phase 07)

    /// Who currently owns the Scanlight serial port. When not `.idle`, manual
    /// light actions (connect/setChannel/pulse) are rejected with a clear error
    /// rather than racing the orchestrator (`.scanning`) or the calibration
    /// script (`.calibrating`) for the port — a double-open corrupts scans
    /// silently. `disconnect()` is intentionally NOT guarded (ScanCoordinator
    /// calls it deliberately when starting a scan).
    ///
    /// Writers: ScanCoordinator (`.scanning`/`.idle`) and CalibrationView
    /// (`.calibrating`/`.idle`). `@Published` so the Light and Calibrate tabs
    /// reactively disable their port-grabbing controls while it is owned.
    @Published var portOwner: PortOwner = .idle

    var scanlightPort: String {
        port.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Init

    init(transportFactory: @escaping () -> Result<ScanlightTransport, Error>) {
        self.transportFactory = transportFactory
    }

    // MARK: - Actions

    func connect() {
        guard guardPortOwner("connect") else { return }
        log("connect: opening transport")
        let transportResult: Result<ScanlightTransport, Error>
        if !scanlightPort.isEmpty {
            transportResult = Result { try SerialPortTransport(devicePath: scanlightPort) }
        } else {
            transportResult = transportFactory()
        }

        switch transportResult {
        case .success(let transport):
            let driver = Scanlight(transport: transport)
            self.scanlight = driver
            do {
                let (fw, hw) = try driver.getFWVersion()
                firmwareString = "fw=\(fw) hw=\(hw)"
                hardwareString = "hw=\(hw)"
                let _ = try? driver.getDefaultRGB()
                refreshTelemetry()
                connectionStatusString = "connected"
                isConnected = true
                lastError = ""
                log("connect: ok — firmware \(firmwareString)")
            } catch {
                lastError = "connect: \(error)"
                log("error connect: \(error)")
                driver.close()
                scanlight = nil
            }
        case .failure(let error):
            lastError = "connect: \(error)"
            log("error connect (transport): \(error)")
            isConnected = false
        }
    }

    func disconnect() {
        scanlight?.close()
        scanlight = nil
        isConnected = false
        connectionStatusString = "disconnected"
        firmwareString = "—"
        hardwareString = "—"
        ledTempString = "—"
        vbusString = "—"
        log("disconnect: closed")
    }

    // Each turnOnX lights ONLY its channel (the others are zeroed, so the
    // device + the sliders both reflect single-channel exclusivity) and records
    // `activeChannel` on success. Driven by `toggle(_:)` from the Light tab.
    func turnOnRed() {
        guard guardPortOwner("turnOnRed"), guardConnected("turnOnRed") else { return }
        greenLevel = 0; blueLevel = 0; whiteLevel = 0
        attempt("turnOnRed") {
            try scanlight?.setColor(r: Int(redLevel))
            activeChannel = .red
        }
    }

    func turnOnGreen() {
        guard guardPortOwner("turnOnGreen"), guardConnected("turnOnGreen") else { return }
        redLevel = 0; blueLevel = 0; whiteLevel = 0
        attempt("turnOnGreen") {
            try scanlight?.setColor(g: Int(greenLevel))
            activeChannel = .green
        }
    }

    func turnOnBlue() {
        guard guardPortOwner("turnOnBlue"), guardConnected("turnOnBlue") else { return }
        redLevel = 0; greenLevel = 0; whiteLevel = 0
        attempt("turnOnBlue") {
            try scanlight?.setColor(b: Int(blueLevel))
            activeChannel = .blue
        }
    }

    func turnOnWhite() {
        guard guardPortOwner("turnOnWhite"), guardConnected("turnOnWhite") else { return }
        redLevel = 0; greenLevel = 0; blueLevel = 0
        attempt("turnOnWhite") {
            try scanlight?.setColor(w: Int(whiteLevel))
            activeChannel = .white
        }
    }

    func allOff() {
        guard guardPortOwner("allOff"), guardConnected("allOff") else { return }
        attempt("allOff") {
            try scanlight?.off()
            redLevel = 0; greenLevel = 0; blueLevel = 0; whiteLevel = 0
            activeChannel = nil
        }
    }

    /// Switch-style tap from the Light tab: off → on at full brightness
    /// (exclusive), on → all off. No need to set a slider level first — a fresh
    /// tap defaults the channel to full unless you've already dialed in a level.
    func toggle(_ channel: LightChannel) {
        if activeChannel == channel {
            allOff()
            return
        }
        switch channel {
        case .red:   if redLevel == 0 { redLevel = 255 }
        case .green: if greenLevel == 0 { greenLevel = 255 }
        case .blue:  if blueLevel == 0 { blueLevel = 255 }
        case .white: if whiteLevel == 0 { whiteLevel = 255 }
        }
        apply(channel)
    }

    /// Live-dim: re-send the active channel at its current slider level. No-op
    /// unless `channel` is the one currently lit (so dragging an off channel's
    /// slider just stages a level for the next tap).
    func setLevel(_ channel: LightChannel, to value: Double) {
        guard activeChannel == channel else { return }
        apply(channel)
    }

    private func apply(_ channel: LightChannel) {
        switch channel {
        case .red:   turnOnRed()
        case .green: turnOnGreen()
        case .blue:  turnOnBlue()
        case .white: turnOnWhite()
        }
    }

    func firePulse() {
        guard guardPortOwner("firePulse"), guardConnected("firePulse") else { return }
        guard let ms = Int(pulseMs),
              (10...2550).contains(ms),
              ms % 10 == 0 else {
            lastError = "pulse ms must be integer in 10..2550, multiple of 10"
            log("error firePulse: invalid pulse ms '\(pulseMs)'")
            return
        }
        attempt("firePulse") {
            try scanlight?.pulseShutter(pulseMs: ms)
        }
    }

    func clearLog() {
        logLines.removeAll()
    }

    // MARK: - Privates

    private let transportFactory: () -> Result<ScanlightTransport, Error>
    private var scanlight: Scanlight?

    /// Returns false and sets lastError when the port is owned by the scanner.
    /// Exported as `internal` so ScanCoordinatorTests can call it directly to
    /// verify the guard rejects actions during a scan without launching the app.
    @discardableResult
    internal func guardPortOwner(_ action: String) -> Bool {
        guard portOwner == .idle else {
            lastError = "\(action) rejected: port is controlled by active scan"
            log("error \(action): rejected — portOwner=\(portOwner)")
            return false
        }
        return true
    }

    @discardableResult
    internal func guardConnected(_ action: String) -> Bool {
        guard isConnected, scanlight != nil else {
            lastError = "\(action) rejected: Scanlight is not connected"
            log("error \(action): rejected — not connected")
            return false
        }
        return true
    }

    private func refreshTelemetry() {
        ledTempString = scanlight?.lastTempC.map { String(format: "%.2f °C", $0) } ?? "—"
        vbusString = scanlight?.lastVBUSmv.map { "\($0) mV" } ?? "—"
    }

    private func attempt(_ name: String, _ block: () throws -> Void) {
        do {
            try block()
            lastError = ""
            refreshTelemetry()
            log("\(name): ok")
        } catch {
            if let message = serialDisconnectMessage(action: name, error: error) {
                markDisconnectedAfterSerialFailure(message)
            } else {
                lastError = "\(name): \(error)"
                log("error \(name): \(error)")
            }
        }
    }

    private func markDisconnectedAfterSerialFailure(_ message: String) {
        scanlight?.close()
        scanlight = nil
        isConnected = false
        connectionStatusString = "disconnected"
        firmwareString = "—"
        hardwareString = "—"
        ledTempString = "—"
        vbusString = "—"
        activeChannel = nil
        lastError = message
        log("error \(message)")
    }

    private func serialDisconnectMessage(action: String, error: Error) -> String? {
        if let openError = error as? SerialPortTransport.OpenError {
            switch openError {
            case .writeFailed(let errno):
                if Self.isDisconnectErrno(errno) {
                    return "\(action): Scanlight disconnected while writing (errno \(errno)). Reconnect the light."
                }
            case .readFailed(let errno):
                if Self.isDisconnectErrno(errno) {
                    return "\(action): Scanlight disconnected while reading (errno \(errno)). Reconnect the light."
                }
            case .openFailed, .configureFailed, .noPortDiscovered:
                break
            }
        }

        if let scanlightError = error as? ScanlightError,
           scanlightError == .transportClosed {
            return "\(action): Scanlight connection closed. Reconnect the light."
        }

        return nil
    }

    private static func isDisconnectErrno(_ errno: Int32) -> Bool {
        [ENXIO, EIO, EBADF, ENODEV, EPIPE].contains(errno)
    }

    private func log(_ message: String) {
        let ts = Date().formatted(.iso8601.time(includingFractionalSeconds: true))
        logLines.insert("\(ts) \(message)", at: 0)
        if logLines.count > 200 {
            logLines.removeLast(logLines.count - 200)
        }
    }
}
