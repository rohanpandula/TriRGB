// Real POSIX-serial transport for the Scanlight. Hits a /dev/cu.usbmodem*
// path directly via open(2)/read(2)/write(2) — no third-party dependency.
//
// HW-VERIFICATION GATE: this file compiles and runs without a real
// Scanlight attached, but the read/write path has only been exercised
// against an in-memory fake (see ScanlightSwiftTests). The first time a
// device plugs in, walk through `scripts/diagnose.py` (Python side) first
// to confirm VID:PID and the serial path before relying on this Swift
// implementation. The Pico CDC PID is 0x000A; if the device shows up
// under a different node, pass the explicit path.

import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

public final class SerialPortTransport: ScanlightTransport {

    public enum OpenError: Error, Equatable {
        case openFailed(path: String, errno: Int32)
        case configureFailed(errno: Int32)
        case readFailed(errno: Int32)
        case writeFailed(errno: Int32)
        case noPortDiscovered
    }

    private let fd: Int32
    private let lock = NSLock()
    private var closed = false

    /// Open by explicit device path (e.g. `/dev/cu.usbmodem1101`).
    public init(devicePath: String) throws {
        let fd = open(devicePath, O_RDWR | O_NOCTTY | O_NONBLOCK)
        if fd < 0 {
            throw OpenError.openFailed(path: devicePath, errno: errno)
        }
        self.fd = fd
        do {
            try Self.configure(fd: fd)
        } catch {
            close()
            throw error
        }
    }

    /// Best-effort auto-discovery: scan `/dev/cu.usbmodem*`. Returns the
    /// only match if there's exactly one. The Python diagnose.py is the
    /// preferred discovery path the first time a device is plugged in.
    public static func discoverAndOpen() throws -> SerialPortTransport {
        let dir = "/dev"
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: dir) else {
            throw OpenError.noPortDiscovered
        }
        let candidates = entries
            .filter { $0.hasPrefix("cu.usbmodem") }
            .map { "\(dir)/\($0)" }
        guard candidates.count == 1 else {
            throw OpenError.noPortDiscovered
        }
        return try SerialPortTransport(devicePath: candidates[0])
    }

    // MARK: - ScanlightTransport

    public func write(_ data: Data) throws {
        lock.lock(); let isClosed = closed; lock.unlock()
        if isClosed { throw OpenError.writeFailed(errno: EBADF) }
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Void in
            guard let base = raw.baseAddress else { return }
            var written = 0
            // Bounded retry budget for EAGAIN/EWOULDBLOCK from the
            // O_NONBLOCK fd. With ~10 byte H2D packets and a typical
            // USB-CDC buffer, contention is rare; 100 × 1 ms = 100 ms
            // worst-case before we give up. Codex flagged the original
            // implementation as fatal-on-EAGAIN, which would hard-fail
            // on a busy device instead of waiting briefly.
            var eagainRetries = 0
            let maxEagainRetries = 100
            while written < data.count {
                let n = Darwin.write(fd, base.advanced(by: written), data.count - written)
                if n < 0 {
                    let e = errno
                    if e == EINTR { continue }
                    if e == EAGAIN || e == EWOULDBLOCK {
                        if eagainRetries >= maxEagainRetries {
                            throw OpenError.writeFailed(errno: e)
                        }
                        eagainRetries += 1
                        // 1 ms sleep — short enough to feel synchronous,
                        // long enough to let the kernel drain its CDC
                        // output buffer.
                        usleep(1000)
                        continue
                    }
                    throw OpenError.writeFailed(errno: e)
                }
                written += n
            }
        }
    }

    public func readAvailable() throws -> Data {
        lock.lock(); let isClosed = closed; lock.unlock()
        if isClosed { throw OpenError.readFailed(errno: EBADF) }
        var buf = [UInt8](repeating: 0, count: 256)
        // Use a short poll-style read: O_NONBLOCK is set, so read returns
        // immediately with EAGAIN when nothing is available. Sleep briefly
        // to keep CPU low — matches Python's `timeout=0.1` behavior.
        var data = Data()
        for _ in 0..<5 {
            let n = buf.withUnsafeMutableBufferPointer { ptr -> Int in
                return Darwin.read(fd, ptr.baseAddress, ptr.count)
            }
            if n > 0 {
                data.append(contentsOf: buf[0..<n])
            } else if n < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK {
                    // No data right now; brief sleep before another peek.
                    usleep(20_000)  // 20ms
                    continue
                }
                if errno == EINTR { continue }
                throw OpenError.readFailed(errno: errno)
            } else {
                break
            }
            if !data.isEmpty { return data }
        }
        return data
    }

    public func close() {
        lock.lock(); defer { lock.unlock() }
        if !closed {
            _ = Darwin.close(fd)
            closed = true
        }
    }

    // MARK: - private

    /// Configure the tty for raw 8N1 binary I/O. USB CDC ignores baud
    /// rate in practice (it's a virtual concept) but we set a sane value.
    private static func configure(fd: Int32) throws {
        var tio = termios()
        if tcgetattr(fd, &tio) != 0 {
            throw OpenError.configureFailed(errno: errno)
        }
        cfmakeraw(&tio)
        cfsetspeed(&tio, speed_t(B115200))
        tio.c_cflag |= UInt(CS8 | CLOCAL | CREAD)
        tio.c_cflag &= ~UInt(PARENB | CSTOPB)
        // Make read() return as soon as any byte is available.
        withUnsafeMutablePointer(to: &tio.c_cc) { ccPtr in
            ccPtr.withMemoryRebound(to: UInt8.self, capacity: Int(NCCS)) { cc in
                cc[Int(VMIN)] = 0
                cc[Int(VTIME)] = 1   // 100ms inter-byte timeout (deciseconds)
            }
        }
        if tcsetattr(fd, TCSANOW, &tio) != 0 {
            throw OpenError.configureFailed(errno: errno)
        }
    }
}
