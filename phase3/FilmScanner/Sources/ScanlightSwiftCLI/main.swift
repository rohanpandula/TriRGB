// scanlight-swift-cli — automatable terminal client for the Swift
// Scanlight driver.
//
// Two execution modes:
//   1. Real serial port: same as `scanlightctl` (Python). Requires
//      hardware. Used on plug-in day to verify the Swift port works.
//   2. `--fake` transport: in-process FakeTransport (no hardware).
//      Used by `selftest` and by external test harnesses that want to
//      assert end-to-end behavior of the CLI without a device.
//
// All commands accept `--json` to switch from human-readable text to a
// single JSON object on stdout. JSON mode is intended for AI agents and
// CI harnesses that need to parse the result reliably. Exit codes are
// stable across both output modes:
//   0 success, 1 operational failure, 2 bad arguments.
//
// Usage examples:
//   scanlight-swift-cli status
//   scanlight-swift-cli status --json
//   scanlight-swift-cli on r --level 200
//   scanlight-swift-cli off
//   scanlight-swift-cli pulse 100
//   scanlight-swift-cli set --r 200 --g 180 --b 160
//   scanlight-swift-cli selftest --json    # no hardware required

import Foundation
import ScanlightSwift


// MARK: - JSON output helpers

/// Tiny ad-hoc JSON encoder to avoid pulling in Codable boilerplate for
/// what's essentially a flat dict of primitives. Keeps the CLI hermetic.
func encodeJSON(_ obj: [String: Any]) -> String {
    var out = "{"
    var first = true
    // Stable key order so external tests can pattern-match the output
    // without depending on Foundation's dict iteration.
    for key in obj.keys.sorted() {
        if !first { out += "," }
        first = false
        out += "\(jsonString(key)):\(jsonValue(obj[key]!))"
    }
    out += "}"
    return out
}

private func jsonString(_ s: String) -> String {
    var escaped = "\""
    for ch in s {
        switch ch {
        case "\"": escaped += "\\\""
        case "\\": escaped += "\\\\"
        case "\n": escaped += "\\n"
        case "\r": escaped += "\\r"
        case "\t": escaped += "\\t"
        default: escaped.append(ch)
        }
    }
    escaped += "\""
    return escaped
}

private func jsonValue(_ v: Any) -> String {
    if let s = v as? String { return jsonString(s) }
    if let b = v as? Bool { return b ? "true" : "false" }
    if let i = v as? Int { return String(i) }
    if let i = v as? Int32 { return String(i) }
    if let i = v as? UInt8 { return String(i) }
    if let i = v as? UInt16 { return String(i) }
    if let d = v as? Double { return String(d) }
    if v is NSNull { return "null" }
    if let arr = v as? [Any] {
        return "[" + arr.map(jsonValue).joined(separator: ",") + "]"
    }
    if let dict = v as? [String: Any] { return encodeJSON(dict) }
    // Fallback — describe + escape. Shouldn't happen if callers use known types.
    return jsonString(String(describing: v))
}


// MARK: - argv parsing

struct ParsedArgs {
    var command: String
    var subject: String?          // e.g., "r"/"g"/"b"/"w" for `on`, or pulse ms
    var port: String?
    var level: Int = 255
    var r: Int = 0
    var g: Int = 0
    var b: Int = 0
    var useFake: Bool = false
    var json: Bool = false
}

enum ArgError: Error { case usage(String) }

func parseArgs(_ argv: [String]) throws -> ParsedArgs {
    var args = argv
    guard !args.isEmpty else { throw ArgError.usage("missing command") }
    var p = ParsedArgs(command: args.removeFirst())

    // First positional may be the subject (`on r`, `pulse 100`).
    if !args.isEmpty && !args[0].hasPrefix("-") {
        p.subject = args.removeFirst()
    }

    while !args.isEmpty {
        let flag = args.removeFirst()
        // Boolean flags
        switch flag {
        case "--fake":
            p.useFake = true
            continue
        case "--json":
            p.json = true
            continue
        default:
            break
        }
        guard !args.isEmpty else {
            throw ArgError.usage("flag \(flag) requires a value")
        }
        let value = args.removeFirst()
        switch flag {
        case "--port": p.port = value
        case "--level":
            guard let v = Int(value), (0...255).contains(v) else {
                throw ArgError.usage("--level must be 0..255, got \(value)")
            }
            p.level = v
        case "--r":
            guard let v = Int(value), (0...255).contains(v) else {
                throw ArgError.usage("--r must be 0..255, got \(value)")
            }
            p.r = v
        case "--g":
            guard let v = Int(value), (0...255).contains(v) else {
                throw ArgError.usage("--g must be 0..255, got \(value)")
            }
            p.g = v
        case "--b":
            guard let v = Int(value), (0...255).contains(v) else {
                throw ArgError.usage("--b must be 0..255, got \(value)")
            }
            p.b = v
        default:
            throw ArgError.usage("unknown flag: \(flag)")
        }
    }
    return p
}


// MARK: - in-process fake transport for --fake mode

/// In-memory transport mirroring the test fixture. Used by `selftest`
/// and any external harness that passes `--fake` so it can validate CLI
/// behavior without a real device.
final class FakeTransport: ScanlightTransport {
    private let lock = NSLock()
    private let cv = NSCondition()
    private var tx = Data()
    private var rx = Data()
    private var closed = false

    var transmitted: Data { lock.lock(); defer { lock.unlock() }; return tx }

    func write(_ data: Data) throws {
        if closed { throw ScanlightError.transportClosed }
        lock.lock(); tx.append(data); lock.unlock()
        // Synthesize a response for known H2D requests. This is enough
        // to let `selftest` exercise the full request/response loop.
        synthesizeResponses(for: data)
    }

    func readAvailable() throws -> Data {
        cv.lock()
        if rx.isEmpty {
            cv.wait(until: Date(timeIntervalSinceNow: 0.05))
        }
        let out = rx
        rx.removeAll(keepingCapacity: true)
        cv.unlock()
        return out
    }

    func close() {
        cv.lock(); closed = true; cv.broadcast(); cv.unlock()
    }

    private func synthesizeResponses(for data: Data) {
        guard data.count >= 3, data[data.startIndex] == ScanlightProtocol.startByte else { return }
        let header = data[data.startIndex + 1]
        if header == ScanlightProtocol.h2dGetFWVersion {
            // FW=1, HW=1 → wire word 0x00010001 BE
            let resp = Data([
                0xFE, ScanlightProtocol.d2hFWVersion, 4,
                0x00, 0x01, 0x00, 0x01,
            ])
            feed(resp)
        } else if header == ScanlightProtocol.h2dGetDefaultRGB {
            feed(Data([0xFE, ScanlightProtocol.d2hDefaultRGB, 3, 255, 200, 180]))
        }
        // Synthesize telemetry on every interaction so `status` has
        // something to show.
        let temp: Int32 = 32500   // 32.5 °C
        let tempPayload = bigEndianInt32(temp)
        feed(Data([0xFE, ScanlightProtocol.d2hLEDTemp, 4]) + tempPayload)
        let vbus: Int32 = 5050    // 5.05 V
        let vbusPayload = bigEndianInt32(vbus)
        feed(Data([0xFE, ScanlightProtocol.d2hVBUS, 4]) + vbusPayload)
    }

    private func bigEndianInt32(_ v: Int32) -> Data {
        let raw = UInt32(bitPattern: v)
        return Data([
            UInt8((raw >> 24) & 0xFF),
            UInt8((raw >> 16) & 0xFF),
            UInt8((raw >> 8) & 0xFF),
            UInt8(raw & 0xFF),
        ])
    }

    private func feed(_ data: Data) {
        cv.lock(); rx.append(data); cv.broadcast(); cv.unlock()
    }
}


// MARK: - transport open

func openTransport(_ args: ParsedArgs) throws -> ScanlightTransport {
    if args.useFake { return FakeTransport() }
    if let p = args.port {
        return try SerialPortTransport(devicePath: p)
    }
    return try SerialPortTransport.discoverAndOpen()
}


// MARK: - human / JSON reporters

func reportOK(_ args: ParsedArgs, command: String, extra: [String: Any] = [:]) {
    if args.json {
        var d: [String: Any] = ["ok": true, "command": command]
        for (k, v) in extra { d[k] = v }
        print(encodeJSON(d))
    } else if !extra.isEmpty {
        // Human mode: print the extras as key: value lines so tests can grep
        for key in extra.keys.sorted() {
            print("\(key): \(extra[key]!)")
        }
    }
}

func reportError(_ args: ParsedArgs, command: String, message: String) {
    if args.json {
        let d: [String: Any] = ["ok": false, "command": command, "error": message]
        print(encodeJSON(d))
    } else {
        FileHandle.standardError.write("scanlight-swift-cli: \(message)\n".data(using: .utf8)!)
    }
}


// MARK: - command implementations

func runStatus(_ args: ParsedArgs) throws -> Int32 {
    let transport = try openTransport(args)
    let s = Scanlight(transport: transport)
    defer { s.close() }
    let (fw, hw) = try s.getFWVersion()
    let (dr, dg, db) = try s.getDefaultRGB()
    // First telemetry may take ~600 ms after a fresh plug-in (firmware
    // self-test). In --fake mode it arrives immediately.
    let deadline = Date().addingTimeInterval(1.5)
    while Date() < deadline && (s.lastTempC == nil || s.lastVBUSmv == nil) {
        Thread.sleep(forTimeInterval: 0.05)
    }
    let extra: [String: Any] = [
        "firmware_id": Int(fw),
        "hardware_id": Int(hw),
        "default_rgb": [Int(dr), Int(dg), Int(db)],
        "led_temp_c": s.lastTempC ?? NSNull(),
        "vbus_mv": s.lastVBUSmv.map { Int($0) } ?? NSNull(),
    ]
    if args.json {
        reportOK(args, command: "status", extra: extra)
    } else {
        print("firmware:     \(fw)")
        print("hardware:     \(hw)")
        print("default RGB:  \(dr), \(dg), \(db)")
        if let t = s.lastTempC {
            print(String(format: "LED temp:     %.2f °C", t))
        } else {
            print("LED temp:     (no data yet)")
        }
        if let v = s.lastVBUSmv {
            print("VBUS:         \(v) mV (\(Double(v) / 1000.0) V)")
        } else {
            print("VBUS:         (no data yet)")
        }
    }
    return 0
}

func runOn(_ args: ParsedArgs) throws -> Int32 {
    guard let channel = args.subject else {
        throw ArgError.usage("on requires a channel argument (r|g|b|w)")
    }
    let s = Scanlight(transport: try openTransport(args))
    defer { s.close() }
    switch channel {
    case "r": try s.setColor(r: args.level)
    case "g": try s.setColor(g: args.level)
    case "b": try s.setColor(b: args.level)
    case "w": try s.setColor(w: args.level)
    default:
        throw ArgError.usage("channel must be one of r,g,b,w; got \(channel)")
    }
    reportOK(args, command: "on", extra: ["channel": channel, "level": args.level])
    return 0
}

func runOff(_ args: ParsedArgs) throws -> Int32 {
    let s = Scanlight(transport: try openTransport(args))
    defer { s.close() }
    try s.off()
    reportOK(args, command: "off")
    return 0
}

func runSet(_ args: ParsedArgs) throws -> Int32 {
    let s = Scanlight(transport: try openTransport(args))
    defer { s.close() }
    try s.setColor(r: args.r, g: args.g, b: args.b)
    reportOK(args, command: "set", extra: ["r": args.r, "g": args.g, "b": args.b])
    return 0
}

func runPulse(_ args: ParsedArgs) throws -> Int32 {
    guard let subj = args.subject, let ms = Int(subj) else {
        throw ArgError.usage("pulse requires a millisecond argument (10..2550, multiple of 10)")
    }
    let s = Scanlight(transport: try openTransport(args))
    defer { s.close() }
    try s.pulseShutter(pulseMs: ms)
    reportOK(args, command: "pulse", extra: ["pulse_ms": ms])
    return 0
}

/// `selftest` — exercise the driver against a FakeTransport. Designed for
/// AI agents and CI: completes without hardware, returns JSON-checkable
/// pass/fail per step. Each step's result is recorded; overall exit
/// code reflects the first failure.
func runSelftest(_ args: ParsedArgs) throws -> Int32 {
    var steps: [[String: Any]] = []
    var allPassed = true

    func record(_ name: String, _ ok: Bool, _ message: String = "") {
        steps.append(["name": name, "ok": ok, "message": message])
        if !ok { allPassed = false }
    }

    // Use FakeTransport regardless of --fake flag — selftest is hermetic.
    let fake = FakeTransport()
    let s = Scanlight(transport: fake)
    defer { s.close() }

    // 1. FW version round-trip
    do {
        let (fw, hw) = try s.getFWVersion()
        let ok = (fw == 1 && hw == 1)
        record("fw_version_request", ok, ok ? "fw=1 hw=1" : "got fw=\(fw) hw=\(hw)")
    } catch {
        record("fw_version_request", false, "threw: \(error)")
    }

    // 2. Default RGB request
    do {
        let (r, g, b) = try s.getDefaultRGB()
        let ok = (r == 255 && g == 200 && b == 180)
        record("default_rgb_request", ok, ok ? "(255,200,180)" : "got (\(r),\(g),\(b))")
    } catch {
        record("default_rgb_request", false, "threw: \(error)")
    }

    // 3. SET_COLOR encoding
    do {
        try s.setColor(r: 100, g: 50, b: 25)
        let last = fake.transmitted.suffix(9)
        let expected: [UInt8] = [0xFE, 0, 6, 100, 50, 25, 0, 0, 0]
        let ok = Array(last) == expected
        record("set_color_packet_bytes", ok,
               ok ? "matches expected" : "got \(Array(last)), wanted \(expected)")
    } catch {
        record("set_color_packet_bytes", false, "threw: \(error)")
    }

    // 4. Pulse encoding (100 ms → byte 10)
    do {
        let before = fake.transmitted.count
        try s.pulseShutter(pulseMs: 100)
        let pulsePacket = fake.transmitted.subdata(in: before..<fake.transmitted.count)
        let expected = Data([0xFE, ScanlightProtocol.h2dShutterPulse, 1, 10])
        let ok = pulsePacket == expected
        record("pulse_shutter_packet_bytes", ok,
               ok ? "matches expected" : "got \(Array(pulsePacket)), wanted \(Array(expected))")
    } catch {
        record("pulse_shutter_packet_bytes", false, "threw: \(error)")
    }

    // 5. Pulse validation rejects out-of-range
    do {
        try s.pulseShutter(pulseMs: 7)  // not multiple of 10, below min
        record("pulse_shutter_rejects_invalid", false, "should have thrown for pulse_ms=7")
    } catch {
        record("pulse_shutter_rejects_invalid", true, "correctly rejected")
    }

    // 6. Telemetry arrives via reader loop
    let deadline = Date().addingTimeInterval(1.0)
    while Date() < deadline && (s.lastTempC == nil || s.lastVBUSmv == nil) {
        Thread.sleep(forTimeInterval: 0.05)
    }
    let tempOk = (s.lastTempC.map { abs($0 - 32.5) < 1e-6 } ?? false)
    let vbusOk = (s.lastVBUSmv == 5050)
    record("telemetry_led_temp", tempOk, tempOk ? "32.5 °C" : "got \(String(describing: s.lastTempC))")
    record("telemetry_vbus", vbusOk, vbusOk ? "5050 mV" : "got \(String(describing: s.lastVBUSmv))")

    // 7. White-with-RGB rejection
    do {
        try s.setColor(r: 200, w: 100)
        record("white_with_rgb_rejected", false, "should have thrown")
    } catch ScanlightError.whiteWithRGB {
        record("white_with_rgb_rejected", true, "correctly rejected")
    } catch {
        record("white_with_rgb_rejected", false, "wrong error: \(error)")
    }

    // Output
    if args.json {
        print(encodeJSON([
            "ok": allPassed,
            "command": "selftest",
            "steps": steps,
            "step_count": steps.count,
            "pass_count": steps.filter { ($0["ok"] as? Bool) == true }.count,
        ]))
    } else {
        for step in steps {
            let mark = ((step["ok"] as? Bool) == true) ? "✓" : "✗"
            print("  \(mark) \(step["name"]!): \(step["message"]!)")
        }
        let passed = steps.filter { ($0["ok"] as? Bool) == true }.count
        print("")
        print(allPassed ? "PASS (\(passed)/\(steps.count))" : "FAIL (\(passed)/\(steps.count))")
    }
    return allPassed ? 0 : 1
}


// MARK: - main

func printUsage() {
    let usage = """
    scanlight-swift-cli — automatable terminal client for the Swift driver.

    Usage:
      scanlight-swift-cli status [--port PATH] [--fake] [--json]
      scanlight-swift-cli on r|g|b|w [--level 0..255] [--port PATH] [--fake] [--json]
      scanlight-swift-cli off [--port PATH] [--fake] [--json]
      scanlight-swift-cli set --r N --g N --b N [--port PATH] [--fake] [--json]
      scanlight-swift-cli pulse <ms> [--port PATH] [--fake] [--json]
      scanlight-swift-cli selftest [--json]

    Modes:
      --fake   Use an in-process FakeTransport instead of a real serial
               port. Used by `selftest` and by external automation that
               wants to assert CLI behavior without hardware.
      --json   Emit a single-line JSON object on stdout. Stable schema:
               `{"ok": bool, "command": string, ...}`.

    Defaults to auto-discovery (single /dev/cu.usbmodem* device).
    Exit codes: 0 success, 1 operational failure, 2 bad arguments.
    """
    print(usage)
}

let argv = Array(CommandLine.arguments.dropFirst())
if argv.isEmpty || argv[0] == "-h" || argv[0] == "--help" {
    printUsage()
    exit(0)
}

do {
    let parsed = try parseArgs(argv)
    let rc: Int32
    switch parsed.command {
    case "status":   rc = try runStatus(parsed)
    case "on":       rc = try runOn(parsed)
    case "off":      rc = try runOff(parsed)
    case "set":      rc = try runSet(parsed)
    case "pulse":    rc = try runPulse(parsed)
    case "selftest": rc = try runSelftest(parsed)
    default:
        reportError(parsed, command: parsed.command,
                    message: "unknown command: \(parsed.command)")
        printUsage()
        exit(2)
    }
    exit(rc)
} catch let ArgError.usage(msg) {
    let dummy = ParsedArgs(command: argv.first ?? "?")
    reportError(dummy, command: dummy.command, message: msg)
    printUsage()
    exit(2)
} catch {
    let dummy = ParsedArgs(command: argv.first ?? "?")
    reportError(dummy, command: dummy.command, message: "\(error)")
    exit(1)
}
