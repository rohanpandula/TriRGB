// AccessibilityIDs — the contract between the SwiftUI app and any
// automation that drives it (XCTest UI, cua-driver, AX-tree-based agents).
//
// Every interactive SwiftUI element in the app sets one of these as its
// `accessibilityIdentifier`. Reading state goes through the same IDs:
// a status label exposes its current value via its accessibility label
// or value, queryable by ID. AI agents looking at a snapshot of the AX
// tree can:
//
//   1. Find any control by its stable ID (these strings never change
//      without a version bump and a doc update in `docs/automation.md`).
//   2. Click / type / read without depending on layout, label text, or
//      window title — all of which can drift.
//   3. Cross-reference what they see against the CLI surface: every
//      action available in the GUI has a `scanlight-swift-cli`
//      equivalent. The set of GUI IDs and the set of CLI commands are
//      intentionally a 1-to-1 mapping, documented in
//      `docs/automation.md` § "GUI ↔ CLI mapping table".
//
// When adding a new control to the app, add the ID here first. When
// renaming an ID, bump the schema version and update the doc.

import Foundation

public enum AccessibilityID {

    /// Version of the AX-ID schema. AI agents and external tests should
    /// check this matches what they expect before relying on individual
    /// IDs. Bump when an existing ID is renamed or removed.
    public static let schemaVersion = "1"

    // MARK: - Connection

    public static let connectButton          = "btn-connect"
    public static let disconnectButton       = "btn-disconnect"
    public static let portTextField          = "field-port"

    // MARK: - Status display (read-only labels)

    public static let connectionStatusLabel  = "lbl-connection-status"
    public static let firmwareLabel          = "lbl-firmware"
    public static let hardwareLabel          = "lbl-hardware"
    public static let ledTempLabel           = "lbl-led-temp"
    public static let vbusLabel              = "lbl-vbus"

    // MARK: - Channel controls

    public static let redSlider              = "slider-red"
    public static let greenSlider            = "slider-green"
    public static let blueSlider             = "slider-blue"
    public static let whiteSlider            = "slider-white"

    public static let redOnButton            = "btn-red-on"
    public static let greenOnButton          = "btn-green-on"
    public static let blueOnButton           = "btn-blue-on"
    public static let whiteOnButton          = "btn-white-on"

    public static let allChannelsOffButton   = "btn-off"
    public static let setAllRGBButton        = "btn-set-rgb"

    // MARK: - Shutter trigger

    public static let pulseMsTextField       = "field-pulse-ms"
    public static let firePulseButton        = "btn-fire-pulse"

    // MARK: - Diagnostic / dev

    public static let lastErrorLabel         = "lbl-last-error"
    public static let logScrollView          = "scroll-log"
    public static let clearLogButton         = "btn-clear-log"
}
