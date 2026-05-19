// Scanlight v4 driver — Swift port of phase1/scanlightctl/scanlight/device.py.
// Same design: one transport, one background reader, request/response
// pairing by header byte, telemetry cached on the instance.

import Dispatch
import Foundation

public final class Scanlight {

    // MARK: - public surface

    public init(transport: ScanlightTransport) {
        self.transport = transport
        self.readerQueue.async { [weak self] in self?.readerLoop() }
    }

    deinit {
        close()
    }

    public func close() {
        lock.lock()
        let alreadyStopped = stopped
        stopped = true
        // Wake any waiter so callers don't hang on a closed transport.
        if let pending = pendingFWVersion {
            pending(.failure(ScanlightError.transportClosed))
            pendingFWVersion = nil
        }
        if let pending = pendingDefaultRGB {
            pending(.failure(ScanlightError.transportClosed))
            pendingDefaultRGB = nil
        }
        lock.unlock()
        if !alreadyStopped {
            transport.close()
        }
    }

    public var lastTempC: Double? {
        lock.lock(); defer { lock.unlock() }
        return lastTempCStored
    }

    public var lastVBUSmv: Int32? {
        lock.lock(); defer { lock.unlock() }
        return lastVBUSmvStored
    }

    /// Set R, G, B, W channels. `save=true` writes the values to NVM as
    /// power-on defaults — use sparingly (finite write cycles).
    public func setColor(
        r: Int = 0, g: Int = 0, b: Int = 0, w: Int = 0, save: Bool = false
    ) throws {
        if w > 0 && (r > 0 || g > 0 || b > 0) {
            // Firmware enforces this too; surface the clear error here.
            throw ScanlightError.whiteWithRGB
        }
        let pkt = try ScanlightProtocol.encodeSetColor(r: r, g: g, b: b, w: w, save: save)
        try transport.write(pkt)
    }

    public func off() throws {
        try setColor()
    }

    /// Fire the 3.5mm shutter trigger output for `pulseMs` milliseconds.
    /// Must be a multiple of 10 in [10, 2550]. Default 100 ms matches
    /// the canonical app_bsl Vue web app's `shutterPulseLength = 0.1`.
    /// Only safe with the camera tether on Wi-Fi (USB tether closes a
    /// ground loop through the 3.5mm cable).
    public func pulseShutter(pulseMs: Int = 100) throws {
        let pkt = try ScanlightProtocol.encodeShutterPulse(pulseMs: pulseMs)
        try transport.write(pkt)
    }

    public func getFWVersion(timeout: TimeInterval = 2.0) throws -> (fw: UInt16, hw: UInt16) {
        return try request(
            h2dHeader: ScanlightProtocol.h2dGetFWVersion,
            d2hHeader: ScanlightProtocol.d2hFWVersion,
            timeout: timeout,
            decode: { try ScanlightProtocol.decodeFWVersion($0) }
        )
    }

    public func getDefaultRGB(timeout: TimeInterval = 2.0) throws -> (r: UInt8, g: UInt8, b: UInt8) {
        return try request(
            h2dHeader: ScanlightProtocol.h2dGetDefaultRGB,
            d2hHeader: ScanlightProtocol.d2hDefaultRGB,
            timeout: timeout,
            decode: { try ScanlightProtocol.decodeDefaultRGB($0) }
        )
    }

    // MARK: - internals

    private let transport: ScanlightTransport
    private let readerQueue = DispatchQueue(label: "scanlight.reader", qos: .userInitiated)
    private let lock = NSLock()
    private var stopped = false
    private var buffer = Data()
    private var lastTempCStored: Double?
    private var lastVBUSmvStored: Int32?
    private var pendingFWVersion: ((Result<Data, Error>) -> Void)?
    private var pendingDefaultRGB: ((Result<Data, Error>) -> Void)?

    /// Generic request/response — synchronously sends a 0-data H2D packet,
    /// blocks waiting for the matching D2H header, decodes the payload.
    private func request<T>(
        h2dHeader: UInt8,
        d2hHeader: UInt8,
        timeout: TimeInterval,
        decode: (Data) throws -> T
    ) throws -> T {
        let sem = DispatchSemaphore(value: 0)
        var stored: Result<Data, Error>?

        lock.lock()
        // Drop any stale completion (defensive — should be nil already).
        switch d2hHeader {
        case ScanlightProtocol.d2hFWVersion:
            pendingFWVersion = { r in stored = r; sem.signal() }
        case ScanlightProtocol.d2hDefaultRGB:
            pendingDefaultRGB = { r in stored = r; sem.signal() }
        default:
            lock.unlock()
            preconditionFailure("unknown D2H header in request: \(d2hHeader)")
        }
        lock.unlock()

        let pkt = try ScanlightProtocol.encodePacket(header: h2dHeader)
        do {
            try transport.write(pkt)
        } catch {
            // Codex audit: if the write throws, the pending slot we just
            // installed would otherwise linger and a stale telemetry-
            // matched D2H reply could fire a now-orphaned closure. Clear
            // the slot eagerly so the next request starts clean.
            lock.lock()
            switch d2hHeader {
            case ScanlightProtocol.d2hFWVersion: pendingFWVersion = nil
            case ScanlightProtocol.d2hDefaultRGB: pendingDefaultRGB = nil
            default: break
            }
            lock.unlock()
            throw error
        }

        let signaled = sem.wait(timeout: .now() + timeout) == .success
        // Always clear the pending slot, even on timeout.
        lock.lock()
        switch d2hHeader {
        case ScanlightProtocol.d2hFWVersion: pendingFWVersion = nil
        case ScanlightProtocol.d2hDefaultRGB: pendingDefaultRGB = nil
        default: break
        }
        lock.unlock()

        if !signaled {
            throw ScanlightError.requestTimeout(headerExpected: d2hHeader)
        }
        switch stored! {
        case .success(let data): return try decode(data)
        case .failure(let err): throw err
        }
    }

    private func readerLoop() {
        while true {
            lock.lock()
            let shouldStop = stopped
            lock.unlock()
            if shouldStop { return }

            let chunk: Data
            do {
                chunk = try transport.readAvailable()
            } catch {
                // On transport error, surface it to any pending caller and exit.
                lock.lock()
                pendingFWVersion?(.failure(error))
                pendingFWVersion = nil
                pendingDefaultRGB?(.failure(error))
                pendingDefaultRGB = nil
                lock.unlock()
                return
            }
            if chunk.isEmpty { continue }

            buffer.append(chunk)
            consumeBuffer()
        }
    }

    /// Parse as many complete packets from `buffer` as available, in place.
    private func consumeBuffer() {
        while true {
            if buffer.isEmpty { return }
            // Resync to the start byte.
            if buffer[buffer.startIndex] != ScanlightProtocol.startByte {
                if let idx = buffer.firstIndex(of: ScanlightProtocol.startByte) {
                    buffer.removeSubrange(buffer.startIndex..<idx)
                } else {
                    buffer.removeAll(keepingCapacity: true)
                    return
                }
                continue
            }
            if buffer.count < 3 { return }  // need header + length
            let length = Int(buffer[buffer.startIndex + 2])
            let total = 3 + length
            if buffer.count < total { return }  // wait for body
            let header = buffer[buffer.startIndex + 1]
            let data = buffer.subdata(in: (buffer.startIndex + 3)..<(buffer.startIndex + total))
            buffer.removeSubrange(buffer.startIndex..<(buffer.startIndex + total))
            dispatch(header: header, data: data)
        }
    }

    private func dispatch(header: UInt8, data: Data) {
        switch header {
        case ScanlightProtocol.d2hLEDTemp:
            if let temp = try? ScanlightProtocol.decodeLEDTemp(data) {
                lock.lock(); lastTempCStored = temp; lock.unlock()
            }
        case ScanlightProtocol.d2hVBUS:
            if let mv = try? ScanlightProtocol.decodeVBUS(data) {
                lock.lock(); lastVBUSmvStored = mv; lock.unlock()
            }
        case ScanlightProtocol.d2hFWVersion:
            let cb: ((Result<Data, Error>) -> Void)?
            lock.lock(); cb = pendingFWVersion; pendingFWVersion = nil; lock.unlock()
            cb?(.success(data))
        case ScanlightProtocol.d2hDefaultRGB:
            let cb: ((Result<Data, Error>) -> Void)?
            lock.lock(); cb = pendingDefaultRGB; pendingDefaultRGB = nil; lock.unlock()
            cb?(.success(data))
        default:
            // Unknown headers are silently dropped — forward-compat with
            // newer firmware revisions.
            break
        }
    }
}
