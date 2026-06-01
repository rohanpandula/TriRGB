# AccessibilityID reference

Every interactive SwiftUI element in the Phase 01 app sets one of the strings
below as its `accessibilityIdentifier`. This file is the flat reference: every
constant in `phase3/FilmScanner/Sources/ScanlightApp/AccessibilityIDs.swift`
has exactly one row. For the GUI ↔ CLI mapping and the schema explanation, see
`docs/automation.md`. A consistency-check script enforces that this file and
the Swift enum agree.

**Schema version:** 4

### Connection

| ID constant | Display label | Type/control |
|---|---|---|
| btn-connect | Connect | button |
| btn-disconnect | Disconnect | button |
| field-port | Serial port | text field |

### Status display

| ID constant | Display label | Type/control |
|---|---|---|
| lbl-connection-status | Connection status | label |
| lbl-firmware | Firmware ID | label |
| lbl-hardware | Hardware ID | label |
| lbl-led-temp | LED temperature | label |
| lbl-vbus | VBUS | label |

### Channel controls

| ID constant | Display label | Type/control |
|---|---|---|
| slider-red | Red level | slider |
| slider-green | Green level | slider |
| slider-blue | Blue level | slider |
| slider-white | White level | slider |
| btn-red-on | Turn red on | button |
| btn-green-on | Turn green on | button |
| btn-blue-on | Turn blue on | button |
| btn-white-on | Turn white on | button |
| btn-off | All channels off | button |

### Shutter trigger

| ID constant | Display label | Type/control |
|---|---|---|
| field-pulse-ms | Pulse length (ms) | text field |
| btn-fire-pulse | Fire shutter pulse | button |

### Diagnostic / dev

| ID constant | Display label | Type/control |
|---|---|---|
| lbl-last-error | Last error | label |
| scroll-log | Log | scroll view |
| btn-clear-log | Clear log | button |

### Settings (Phase 06)

| ID constant | Display label | Type/control |
|---|---|---|
| field-roll-name | Roll name | text field |
| btn-pick-output | Choose output folder | button |
| lbl-output-path | Output folder path | label |
| picker-trigger-mode | Trigger mode | picker |
| btn-pick-inbox | Choose IED inbox | button |
| lbl-inbox-path | IED inbox path | label |
| stepper-settle-ms | Settle (ms) | stepper |
| btn-pick-ffc | Choose FFC calibration | button |
| lbl-ffc-path | FFC calibration path | label |
| picker-camera-model | Camera model | picker |
| field-sony-ip | Sony SDK IP address | text field |
| field-sony-mac | Sony SDK MAC address | text field |
| field-sony-user | Sony Access Auth user | text field |
| field-sony-password | Sony Access Auth password | secure field |
| btn-sony-connect | Check Sony SDK connection | button |
| lbl-sony-connection-status | Sony SDK connection status | label |
| btn-sony-live-view-start | Open Sony SDK live-view preview | button |
| btn-sony-live-view-stop | Close Sony SDK live-view preview | button |
| lbl-sony-live-view-status | Sony SDK live-view status | label |
| img-sony-live-view | Sony SDK live-view image | image |
| toggle-stream-composite | Stream composite | toggle |
| picker-composite-format | Composite format | picker |

### Scan (Phase 07)

| ID constant | Display label | Type/control |
|---|---|---|
| btn-start-scan | Start Scan | button |
| btn-stop-scan | Stop Scan | button |
| btn-capture-frame | Capture Frame | button |
| btn-retake-frame | Retake | button |
| lbl-frame-counter | Frame counter | label |
| list-frame-status | Frame status list | list |
| lbl-composite-queue | Composite queue depth | label |
| lbl-light-locked | Light locked (controlled by scan) | label |
| btn-reconnect-light | Reconnect Light | button |

### Calibration Wizard (Phase 14)

| ID constant | Display label | Type/control |
|---|---|---|
| indicator-wizard-step-1 | Step 1 — Rig Check | step indicator circle |
| indicator-wizard-step-2 | Step 2 — Exposure | step indicator circle |
| indicator-wizard-step-3 | Step 3 — Flat Field | step indicator circle |
| indicator-wizard-step-4 | Step 4 — Results | step indicator circle |
| btn-wizard-back | Back | button |
| btn-wizard-next | Next / primary action | button |
| btn-wizard-rerun | Re-run | button |
| lbl-rig-light | Light panel status | label |
| lbl-rig-firmware | Firmware status | label |
| lbl-rig-camera | Camera reachable status | label |
| lbl-rig-folder | Output folder status | label |
| lbl-exp-clip-r | R channel clip fraction | label |
| lbl-exp-clip-g | G channel clip fraction | label |
| lbl-exp-clip-b | B channel clip fraction | label |
| lbl-exp-level-r | R channel LED level | label |
| lbl-exp-level-g | G channel LED level | label |
| lbl-exp-level-b | B channel LED level | label |
| lbl-exp-verdict-r | R channel exposure verdict | label |
| lbl-exp-verdict-g | G channel exposure verdict | label |
| lbl-exp-verdict-b | B channel exposure verdict | label |
| lbl-exp-overall | Overall exposure verdict | label |
| picker-rebate | Rebate region picker | label |
| btn-rebate-clear | Auto-detect rebate | button |
| field-stock-profile-name | Film stock profile name | text field |
| btn-stock-profile-save | Save stock RGB profile | button |
| picker-stock-profile | Saved stock RGB profile picker | picker |
| btn-stock-profile-apply | Apply saved stock RGB profile | button |
| lbl-ffc-falloff-r | R channel falloff | label |
| lbl-ffc-falloff-g | G channel falloff | label |
| lbl-ffc-falloff-b | B channel falloff | label |
| lbl-ffc-uniformity-r | R channel uniformity | label |
| lbl-ffc-uniformity-g | G channel uniformity | label |
| lbl-ffc-uniformity-b | B channel uniformity | label |
| lbl-ffc-verdict-r | R channel FFC verdict | label |
| lbl-ffc-verdict-g | G channel FFC verdict | label |
| lbl-ffc-verdict-b | B channel FFC verdict | label |
| lbl-ffc-overall | Overall FFC verdict | label |
| lbl-ffc-frames | Frames averaged | label |
| btn-ffc-use | Use this calibration | button |
| lbl-results-shift-rg | R-G registration shift | label |
| lbl-results-shift-gb | G-B registration shift | label |
| lbl-results-reg-verdict | Registration verdict | label |
| lbl-results-base-dev | Base deviation | label |
| lbl-results-base-verdict | Base neutrality verdict | label |
| lbl-results-gain-r | R channel gain | label |
| lbl-results-gain-g | G channel gain | label |
| lbl-results-gain-b | B channel gain | label |
| lbl-results-roll-verdict | Roll-level calibration verdict | label |

To add a new AX-ID, add the constant to `AccessibilityIDs.swift` first, then
add a row here. The consistency-check script
(`scripts/check_docs_consistency.py`, or
`tests/integration/test_docs_consistency.py` once Phase 02 lands the
integration test directory) will fail until this file matches the Swift source.
To rename or remove an AX-ID, also bump `schemaVersion` in both the Swift file
and the `**Schema version:**` line above.

### In-app inversion, Sony live view, and film-stock controls

Added with the Develop (positive inversion), Sony live-view, and film-stock-profile features. Rows are derived from `AccessibilityIDs.swift`; keep this file in sync via `scripts/check_docs_consistency.py`.

| ID constant | Display label | Type/control |
|---|---|---|
| btn-invert-pick-files | Invert pick files | button |
| btn-invert-pick-output | Invert pick output | button |
| btn-invert-run | Invert run | button |
| btn-scan-sony-live-view-start | Scan sony live view start | button |
| btn-scan-sony-live-view-stop | Scan sony live view stop | button |
| btn-scan-stock-profile-apply | Scan stock profile apply | button |
| btn-stock-manager-delete | Stock manager delete | button |
| btn-stock-manager-save | Stock manager save | button |
| btn-stock-manager-use-current | Stock manager use current | button |
| field-stock-manager-name | Stock manager name | text field |
| img-scan-sony-live-view | Scan sony live view | image |
| lbl-invert-output-path | Invert output path | label |
| lbl-invert-status | Invert status | label |
| lbl-next-frame | Next frame | label |
| lbl-scan-sony-live-view-status | Scan sony live view status | label |
| lbl-scan-stock-profile-status | Scan stock profile status | label |
| lbl-stock-manager-status | Stock manager status | label |
| list-stock-manager | Stock manager | list |
| picker-scan-sony-live-view-rotate | Scan sony live view rotate | picker |
| picker-scan-stock-profile | Scan stock profile | picker |
| picker-sony-live-view-rotate | Sony live view rotate | picker |
| scroll-invert-log | Invert log | scroll view |
| slider-scan-sony-live-view-zoom | Scan sony live view zoom | slider |
| slider-sony-live-view-zoom | Sony live view zoom | slider |
| toggle-scan-sony-live-view-flip | Scan sony live view flip | toggle |
| toggle-scan-sony-live-view-invert | Scan sony live view invert | toggle |
| toggle-scan-sony-live-view-mirror | Scan sony live view mirror | toggle |
| toggle-scan-sony-live-view-white | Scan sony live view white | toggle |
| toggle-sony-live-view-flip | Sony live view flip | toggle |
| toggle-sony-live-view-invert | Sony live view invert | toggle |
| toggle-sony-live-view-mirror | Sony live view mirror | toggle |
| toggle-sony-live-view-white | Sony live view white | toggle |
