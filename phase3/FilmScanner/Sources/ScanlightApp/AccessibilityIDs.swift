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
    public static let schemaVersion = "2"

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

    // MARK: - Settings view

    public static let settingsRollNameField      = "field-roll-name"
    public static let settingsPickOutputBtn      = "btn-pick-output"
    public static let settingsOutputPathLabel    = "lbl-output-path"
    public static let settingsTriggerModePicker  = "picker-trigger-mode"
    public static let settingsPickInboxBtn       = "btn-pick-inbox"
    public static let settingsInboxPathLabel     = "lbl-inbox-path"
    public static let settingsLevelRSlider       = "slider-level-r"
    public static let settingsLevelGSlider       = "slider-level-g"
    public static let settingsLevelBSlider       = "slider-level-b"
    public static let settingsSettleStepper      = "stepper-settle-ms"
    public static let settingsPickFfcBtn         = "btn-pick-ffc"
    public static let settingsFfcPathLabel       = "lbl-ffc-path"
    public static let settingsCameraModelPicker  = "picker-camera-model"
    public static let settingsStreamToggle       = "toggle-stream-composite"
    public static let settingsCompositeFormat    = "picker-composite-format"
    public static let settingsSaveBtn            = "btn-save-settings"

    // MARK: - Calibration Wizard (Phase 14)

    // Progress indicator (always rendered)
    public static let wizardStep1Indicator      = "indicator-wizard-step-1"
    public static let wizardStep2Indicator      = "indicator-wizard-step-2"
    public static let wizardStep3Indicator      = "indicator-wizard-step-3"
    public static let wizardStep4Indicator      = "indicator-wizard-step-4"

    // Navigation buttons (always rendered in footer)
    public static let wizardBackBtn             = "btn-wizard-back"
    public static let wizardNextBtn             = "btn-wizard-next"
    public static let wizardRerunBtn            = "btn-wizard-rerun"

    // Rig Check (Step 1)
    public static let rigCheckLightLabel        = "lbl-rig-light"
    public static let rigCheckFirmwareLabel     = "lbl-rig-firmware"
    public static let rigCheckCameraLabel       = "lbl-rig-camera"
    public static let rigCheckFolderLabel       = "lbl-rig-folder"

    // Exposure (Step 2)
    public static let exposureClipR             = "lbl-exp-clip-r"
    public static let exposureClipG             = "lbl-exp-clip-g"
    public static let exposureClipB             = "lbl-exp-clip-b"
    public static let exposureLevelR            = "lbl-exp-level-r"
    public static let exposureLevelG            = "lbl-exp-level-g"
    public static let exposureLevelB            = "lbl-exp-level-b"
    public static let exposureVerdictR          = "lbl-exp-verdict-r"
    public static let exposureVerdictG          = "lbl-exp-verdict-g"
    public static let exposureVerdictB          = "lbl-exp-verdict-b"
    public static let exposureOverall           = "lbl-exp-overall"
    public static let rebatePicker              = "picker-rebate"
    public static let rebateClearBtn            = "btn-rebate-clear"

    // Flat Field (Step 3)
    public static let ffcFalloffR               = "lbl-ffc-falloff-r"
    public static let ffcFalloffG               = "lbl-ffc-falloff-g"
    public static let ffcFalloffB               = "lbl-ffc-falloff-b"
    public static let ffcUniformityR            = "lbl-ffc-uniformity-r"
    public static let ffcUniformityG            = "lbl-ffc-uniformity-g"
    public static let ffcUniformityB            = "lbl-ffc-uniformity-b"
    public static let ffcVerdictR               = "lbl-ffc-verdict-r"
    public static let ffcVerdictG               = "lbl-ffc-verdict-g"
    public static let ffcVerdictB               = "lbl-ffc-verdict-b"
    public static let ffcOverall                = "lbl-ffc-overall"
    public static let ffcFramesLabel            = "lbl-ffc-frames"
    public static let ffcUseBtn                 = "btn-ffc-use"

    // Results (Step 4)
    public static let resultsShiftRG            = "lbl-results-shift-rg"
    public static let resultsShiftGB            = "lbl-results-shift-gb"
    public static let resultsRegVerdict         = "lbl-results-reg-verdict"
    public static let resultsBaseDeviation      = "lbl-results-base-dev"
    public static let resultsBaseVerdict        = "lbl-results-base-verdict"
    public static let resultsGainR              = "lbl-results-gain-r"
    public static let resultsGainG              = "lbl-results-gain-g"
    public static let resultsGainB              = "lbl-results-gain-b"
    public static let resultsRollVerdict        = "lbl-results-roll-verdict"

    // MARK: - Scan view (Phase 07)

    public static let scanStartBtn               = "btn-start-scan"
    public static let scanStopBtn                = "btn-stop-scan"
    public static let scanCaptureFrameBtn        = "btn-capture-frame"
    public static let scanRetakeBtn              = "btn-retake-frame"
    public static let scanFrameCounterLabel      = "lbl-frame-counter"
    public static let scanFrameStatusList        = "list-frame-status"
    public static let scanCompositeQueueLabel    = "lbl-composite-queue"
    public static let scanLightLockedLabel       = "lbl-light-locked"
    public static let scanReconnectLightBtn      = "btn-reconnect-light"
}
