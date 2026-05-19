// Transport abstraction — separates the wire from the protocol logic so the
// driver is testable with an in-memory fake. The real implementation
// (SerialPortTransport.swift) hits a POSIX file descriptor; the tests
// inject their own.

import Foundation

public protocol ScanlightTransport: AnyObject {
    /// Synchronously write all bytes. Throws if the link is closed or the
    /// underlying file descriptor errors.
    func write(_ data: Data) throws

    /// Block briefly (~50–100ms) and return any bytes available, or an
    /// empty `Data` if the timeout elapsed. Must be safe to call from a
    /// dedicated reader thread/queue.
    func readAvailable() throws -> Data

    /// Tear down. After `close()` further reads return empty / throw.
    func close()
}

public enum ScanlightError: Error, Equatable {
    case whiteWithRGB
    case requestTimeout(headerExpected: UInt8)
    case transportClosed
    case malformedPayload(description: String)
}
