# AccessibilityID reference

Every interactive SwiftUI element in the Phase 01 app sets one of the strings
below as its `accessibilityIdentifier`. This file is the flat reference: every
constant in `phase3/FilmScanner/Sources/ScanlightApp/AccessibilityIDs.swift`
has exactly one row. For the GUI ↔ CLI mapping and the schema explanation, see
`docs/automation.md`. A consistency-check script enforces that this file and
the Swift enum agree.

**Schema version:** 1

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
| btn-set-rgb | Set RGB | button |

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

To add a new AX-ID, add the constant to `AccessibilityIDs.swift` first, then
add a row here. The consistency-check script
(`scripts/check_docs_consistency.py`, or
`tests/integration/test_docs_consistency.py` once Phase 02 lands the
integration test directory) will fail until this file matches the Swift source.
To rename or remove an AX-ID, also bump `schemaVersion` in both the Swift file
and the `**Schema version:**` line above.
