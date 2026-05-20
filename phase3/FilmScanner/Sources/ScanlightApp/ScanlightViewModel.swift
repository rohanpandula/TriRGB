// ScanlightViewModel — @MainActor ObservableObject that owns all driver state
// and mutations. ScanlightView is a pure rendering layer; nothing in the View
// should call Scanlight, FakeTransport, or SerialPortTransport directly.

import Combine
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

    /// Pulse duration text field (milliseconds).
    @Published var pulseMs: String = "100"

    /// Last error message, or empty string if none.
    @Published var lastError: String = ""

    /// Log lines, newest first; capped at 200 entries.
    @Published var logLines: [String] = []

    // MARK: - Port-ownership guard (Phase 07)

    /// Set by ScanCoordinator when transitioning into/out of a scan. When not
    /// .idle, manual light actions (connect/disconnect/setChannel/pulse) are
    /// rejected with a clear error rather than racing the orchestrator for the
    /// serial port (which would cause a double-open and corrupt scans silently).
    ///
    /// ScanCoordinator is the ONLY writer of this property. The guard is
    /// intentionally minimal: it does not prevent disconnect() (which
    /// ScanCoordinator calls deliberately when starting a scan).
    var portOwner: PortOwner = .idle

    // MARK: - Init

    init(transportFactory: @escaping () -> Result<ScanlightTransport, Error>) {
        self.transportFactory = transportFactory
    }

    // MARK: - Actions

    func connect() {
        log("connect: opening transport")
        switch transportFactory() {
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

    func turnOnRed() {
        guard guardPortOwner("turnOnRed") else { return }
        attempt("turnOnRed") {
            try scanlight?.setColor(r: Int(redLevel))
        }
    }

    func turnOnGreen() {
        guard guardPortOwner("turnOnGreen") else { return }
        attempt("turnOnGreen") {
            try scanlight?.setColor(g: Int(greenLevel))
        }
    }

    func turnOnBlue() {
        guard guardPortOwner("turnOnBlue") else { return }
        attempt("turnOnBlue") {
            try scanlight?.setColor(b: Int(blueLevel))
        }
    }

    func turnOnWhite() {
        guard guardPortOwner("turnOnWhite") else { return }
        attempt("turnOnWhite") {
            try scanlight?.setColor(w: Int(whiteLevel))
        }
    }

    func allOff() {
        guard guardPortOwner("allOff") else { return }
        attempt("allOff") {
            try scanlight?.off()
        }
    }

    func setAllRGB() {
        guard guardPortOwner("setAllRGB") else { return }
        attempt("setAllRGB") {
            try scanlight?.setColor(r: Int(redLevel), g: Int(greenLevel), b: Int(blueLevel))
        }
    }

    func firePulse() {
        guard guardPortOwner("firePulse") else { return }
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
            lastError = "\(name): \(error)"
            log("error \(name): \(error)")
        }
    }

    private func log(_ message: String) {
        let ts = Date().formatted(.iso8601.time(includingFractionalSeconds: true))
        logLines.insert("\(ts) \(message)", at: 0)
        if logLines.count > 200 {
            logLines.removeLast(logLines.count - 200)
        }
    }
}
