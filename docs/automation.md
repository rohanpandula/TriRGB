# Automation architecture

This file is the contract between the project and any external automation.
AI agents (Claude, Codex) and CI runners should rely on the surfaces, schemas,
exit codes, and AX-IDs documented here. When source code and this doc
diverge, the consistency-check script (`scripts/check_docs_consistency.py`)
will fail; fix the code or the doc before merging.

---

## Contents

- [Surfaces](#surfaces)
- [JSON contracts](#json-contracts)
- [GUI ↔ CLI mapping table](#gui--cli-mapping-table)
- [AX-ID schema](#ax-id-schema)
- [Driving with cua-driver](#driving-with-cua-driver)
- [Driving with XCTest UI](#driving-with-xctest-ui)
- [Driving from Python](#driving-from-python)
- [Decision matrix](#decision-matrix)
- [Failure modes](#failure-modes)

---

## Surfaces

| Surface | Type | Use this when... | Hardware-required |
|---|---|---|---|
| `scanlightctl` | Python CLI (`phase1/scanlightctl/`) | Ad-hoc operator control; scripting that doesn't need JSON output | yes |
| `scanlight-swift-cli` | Swift CLI (`phase3/FilmScanner/Sources/ScanlightSwiftCLI/`) | Headless CI, AI agents, JSON-consuming harnesses; also `selftest` without hardware | optional (`--fake` or `selftest` needs none) |
| `rgb-composite` | Python CLI (`phase2/rgb-composite/`) | Composite a single R/G/B RAW triplet into a 16-bit TIFF or Linear DNG | no (needs ARW files, not hardware) |
| `batch-composite` | Python CLI (`phase2/batch-composite/`) | Composite an entire roll directory | no (needs ARW files, not hardware) |
| `triplet-capture` | Python Flask app + CLI (`phase2/triplet-capture/`) | Per-frame capture orchestration; provides the scanning web UI/backend used by Swift | yes (`--trigger-mode manual`, `hw`, or `sdk`) |
| `scripts/inspect-calibration.py` | Python script | Quantitative vignette/saturation check on a captured FFC calibration triplet | no (needs cal ARWs) |
| `scripts/test_swift_cli.py` | Python pytest harness | Regression CI for the Swift CLI via subprocess; canonical Python-driving example | no (`--fake` transport) |
| SwiftUI app (`phase3/FilmScanner/`) | macOS app | Unified operator surface for Light, Settings, Calibrate, and Scan tabs; driven via XCTest UI or cua-driver | yes for real scanning, no for fake-transport tests |

### CLI entry points

**scanlightctl** (Python):
```bash
# Install (from phase1/scanlightctl/):
pip install -e phase1/scanlightctl

# Status check:
scanlightctl status
# → port:         /dev/cu.usbmodemXXX
# → firmware:     1
# → hardware:     1
# → default RGB:  255, 200, 180
# → LED temp:     32.50 °C
# → VBUS:         5050 mV (5.05 V)

# Turn on red at level 200:
scanlightctl on r --level 200

# Fire shutter pulse (100 ms):
scanlightctl pulse 100

# All channels off:
scanlightctl off
```

**scanlight-swift-cli** (Swift):
```bash
# Build:
swift build --package-path phase3/FilmScanner

# Status (fake transport, JSON output):
.build/debug/scanlight-swift-cli status --fake --json
# → {"command":"status","default_rgb":[255,200,180],"firmware_id":1,"hardware_id":1,"led_temp_c":32.5,"ok":true,"vbus_mv":5050}

# Self-test (no hardware required):
.build/debug/scanlight-swift-cli selftest --json
# → {"command":"selftest","ok":true,"pass_count":8,"step_count":8,"steps":[...]}

# Turn on green at level 128 (real hardware):
scanlight-swift-cli on g --level 128 --json
# → {"channel":"g","command":"on","level":128,"ok":true}
```

**rgb-composite**:
```bash
rgb-composite \
  --r /path/to/Frame001_R.ARW \
  --g /path/to/Frame001_G.ARW \
  --b /path/to/Frame001_B.ARW \
  --out /path/to/Frame001.tif \
  --ffc-calibration ~/.scanlight/calibration/2026-05-19 \
  --format both
# Writes Frame001.tif and Frame001.dng to the output path's directory.
```

**batch-composite**:
```bash
batch-composite /Volumes/SSD/Scans/Roll001 \
  --ffc-calibration ~/.scanlight/calibration/2026-05-19 \
  --format both \
  --workers 2
# Writes composites/ into Roll001/.
```

**triplet-capture**:
```bash
triplet-capture \
  --roll-name Roll001 \
  --output-folder /Volumes/SSD/Scans \
  --trigger-mode manual \
  --ied-inbox /Volumes/SSD/_ied_inbox
# Opens web UI at http://127.0.0.1:8765

# Hardware-pulse mode (IED tether + Scanlight 3.5mm shutter pulse):
triplet-capture \
  --roll-name Roll001 \
  --output-folder /Volumes/SSD/Scans \
  --trigger-mode hw \
  --ied-inbox /Volumes/SSD/_ied_inbox \
  --shutter-pulse-ms 100 \
  --capture-timeout-s 30

# Sony SDK path (Swift SDK mode uses the same CLI boundary):
triplet-capture \
  --roll-name Roll001 \
  --output-folder /Volumes/SSD/Scans \
  --trigger-mode sdk \
  --sony-ip-address 10.0.0.247 \
  --sony-mac-address 10:32:2C:26:1A:3F \
  --sony-user USER \
  --sony-password PASSWORD
```

**scripts/inspect-calibration.py**:
```bash
python3 scripts/inspect-calibration.py ~/.scanlight/calibration/2026-05-19
# → Calibration directory: ...
# →   ch | mean      | center     | corner     | falloff | saturated
# →   ---|...
# → OK with FFC — moderate vignette ...
# Exit: 0 (usable), 1 (redo cal), 2 (files missing or undecodable)
```

---

## JSON contracts

### scanlight-swift-cli (`--json` flag)

Only `scanlight-swift-cli` emits structured JSON output. All other Python CLIs
emit human-readable text to stdout and non-zero exit codes on failure — they
are grep-friendly for operators, not JSON-typed.

#### Common envelope

Every `--json` response is a single-line JSON object with stable key sort
order (`main.swift:39`):

```
{"command": "<subcommand>", "ok": <bool>, ...command-specific fields...}
```

Keys are sorted alphabetically. The `ok` key is always present and is the
universal success/fail signal. The `command` key echoes the subcommand name
for correlation.

On failure, the body shape is (`main.swift` `reportError`):

```
{"command": "<subcommand>", "error": "<message>", "ok": false}
```

#### Per-command extras

| Subcommand | Extra JSON keys | Types | Example values |
|---|---|---|---|
| `status` | `firmware_id`, `hardware_id`, `default_rgb`, `led_temp_c`, `vbus_mv` | int, int, int[3], float\|null, int\|null | `1`, `1`, `[255,200,180]`, `32.5`, `5050` |
| `on` | `channel`, `level` | string, int | `"r"`, `255` |
| `off` | (none beyond envelope) | — | — |
| `set` | `r`, `g`, `b` | int, int, int | `200`, `180`, `160` |
| `pulse` | `pulse_ms` | int | `100` |
| `selftest` | `steps`, `step_count`, `pass_count` | array, int, int | see below |

Full `status` example (fake transport):

```json
{"command":"status","default_rgb":[255,200,180],"firmware_id":1,"hardware_id":1,"led_temp_c":32.5,"ok":true,"vbus_mv":5050}
```

Key sources: `main.swift` `runStatus` (lines around 205-210), `runOn`, `runOff`, `runSet`, `runPulse`.

#### selftest schema

`selftest` always uses `FakeTransport` regardless of the `--fake` flag
(`main.swift` `runSelftest` — `let fake = FakeTransport()`). This means it
always completes without hardware.

Extra keys on the `selftest` response:

| Key | Type | Description |
|---|---|---|
| `steps` | array of `{name: string, ok: bool, message: string}` | One entry per self-test step |
| `step_count` | int | Total steps run |
| `pass_count` | int | Steps that passed |

Current step names (8 steps; stable — locked by `test_selftest_step_names_are_stable` in `scripts/test_swift_cli.py`):

- `fw_version_request`
- `default_rgb_request`
- `set_color_packet_bytes`
- `pulse_shutter_packet_bytes`
- `pulse_shutter_rejects_invalid`
- `telemetry_led_temp`
- `telemetry_vbus`
- `white_with_rgb_rejected`

Full `selftest` example:

```json
{"command":"selftest","ok":true,"pass_count":8,"step_count":8,"steps":[{"message":"fw=1 hw=1","name":"fw_version_request","ok":true},{"message":"(255,200,180)","name":"default_rgb_request","ok":true},...]}
```

#### Python CLI exit codes (structured, not JSON)

The Python CLIs do not emit JSON but their exit codes are a stable contract:

**scanlightctl** (`phase1/scanlightctl/scanlight/cli.py:174-179`):

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Exception raised by the driver layer (message on stderr prefixed `scanlightctl:`) |
| 130 | `KeyboardInterrupt` (operator Ctrl-C) |

**scripts/inspect-calibration.py** (`scripts/inspect-calibration.py:21-23`):

| Exit code | Meaning |
|---|---|
| 0 | All channels pass usable thresholds (may include "OK with FFC" message) |
| 1 | Any channel exceeds redo-cal thresholds (optics problem; do not scan) |
| 2 | Files missing or undecodable |

---

## GUI ↔ CLI mapping table

Every interactive control in the Phase 01 SwiftUI app has a stable
`accessibilityIdentifier` (source:
`phase3/FilmScanner/Sources/ScanlightApp/AccessibilityIDs.swift`). The table
below maps each GUI control to its CLI equivalent, so an automation runner can
exercise any GUI action from a shell command.

| AccessibilityID | Display label / control type | CLI equivalent | Action |
|---|---|---|---|
| `btn-connect` | Connect / button | (GUI-only, intentional) — transport opens on first command in the CLI | Open serial port to Scanlight |
| `btn-disconnect` | Disconnect / button | (GUI-only, intentional) — transport closes on process exit in the CLI | Close serial port |
| `field-port` | Serial port / text field | `--port PATH` on any CLI subcommand | Set the serial device path |
| `lbl-connection-status` | Connection status / label | (implicit: command exited 0 means connected) | Reflect open/closed state |
| `lbl-firmware` | Firmware ID / label | `scanlight-swift-cli status --json \| jq .firmware_id` | Display firmware version integer |
| `lbl-hardware` | Hardware ID / label | `scanlight-swift-cli status --json \| jq .hardware_id` | Display hardware version integer |
| `lbl-led-temp` | LED temperature / label | `scanlight-swift-cli status --json \| jq .led_temp_c` | Display LED temperature in °C (null if no telemetry yet) |
| `lbl-vbus` | VBUS / label | `scanlight-swift-cli status --json \| jq .vbus_mv` | Display VBUS voltage in mV (null if no telemetry yet) |
| `slider-red` | Red level / slider | `--level N` on `scanlight-swift-cli on r` | Set red channel brightness (0-255) |
| `slider-green` | Green level / slider | `--level N` on `scanlight-swift-cli on g` | Set green channel brightness (0-255) |
| `slider-blue` | Blue level / slider | `--level N` on `scanlight-swift-cli on b` | Set blue channel brightness (0-255) |
| `slider-white` | White level / slider | `--level N` on `scanlight-swift-cli on w` | Set white channel brightness (0-255) |
| `btn-red-on` | Turn red on / button | `scanlight-swift-cli on r --level N` | Turn red channel on at slider value |
| `btn-green-on` | Turn green on / button | `scanlight-swift-cli on g --level N` | Turn green channel on at slider value |
| `btn-blue-on` | Turn blue on / button | `scanlight-swift-cli on b --level N` | Turn blue channel on at slider value |
| `btn-white-on` | Turn white on / button | `scanlight-swift-cli on w --level N` | Turn white channel on at slider value |
| `btn-off` | All channels off / button | `scanlight-swift-cli off` | Set all channels to 0 |
| `field-pulse-ms` | Pulse length (ms) / text field | positional `<ms>` argument to `scanlight-swift-cli pulse <ms>` | Provides the millisecond value for the next pulse |
| `btn-fire-pulse` | Fire shutter pulse / button | `scanlight-swift-cli pulse <ms>` (ms from field-pulse-ms) | Fire the 3.5mm shutter trigger output |
| `btn-sony-connect` | Check Sony SDK connection / button | `sony-capture --connect-only --ip-address IP --user USER --password PW` | Probe the Wi-Fi SDK Access Auth path without firing the shutter |
| `lbl-sony-connection-status` | Sony SDK connection status / label | `sony-capture --connect-only` exit code and stdout/stderr | Show the most recent non-shooting SDK probe result |
| `btn-sony-live-view-start` | Open Sony SDK live-view preview / button | `sony-capture --live-view-stream-out PATH --ip-address IP --user USER --password PW` | Open one SDK session and refresh preview JPEG frames without firing the shutter |
| `btn-sony-live-view-stop` | Close Sony SDK live-view preview / button | SIGTERM the `sony-capture --live-view-stream-out` process | Stop live-view streaming and release the SDK session |
| `lbl-sony-live-view-status` | Sony SDK live-view status / label | stream process state and stderr | Show whether live view is opening, open, stopped, or failed |
| `img-sony-live-view` | Sony SDK live-view image / image | JPEG refreshed by `sony-capture --live-view-stream-out PATH` | Display the latest live-view frame |
| `lbl-last-error` | Last error / label | (GUI-only convenience) — CLI surfaces errors on stderr + exit code 1 | Mirror the most recent error message |
| `scroll-log` | Log / scroll view | (GUI-only, intentional) — CLI streams to stderr per command | Per-command diagnostic log |
| `btn-clear-log` | Clear log / button | (GUI-only, intentional) — no CLI equivalent | Clear the log scroll view |

### CLI commands with no GUI equivalent (intentional)

- **`scanlightctl set-default --r N --g N --b N`** — writes NVM. Intentionally
  hidden from the GUI to prevent operator accident during a scanning session.
  The NVM default persists across power cycles. (`phase1/scanlightctl/scanlight/cli.py:36-43`)

- **`scanlight-swift-cli selftest`** — hermetic FakeTransport harness that
  exercises the full driver protocol without hardware. Intentionally CLI-only;
  the GUI operator has no need to run a transport self-test manually.
  (`phase3/FilmScanner/Sources/ScanlightSwiftCLI/main.swift` `runSelftest`)

- **`scanlight-swift-cli set --r N --g N --b N`** — mixed RGB command. Kept
  CLI-only because scan and calibration expose one channel at a time, and mixed
  RGB is not part of the normal film workflow.

---

## AX-ID schema

The contract source is
`phase3/FilmScanner/Sources/ScanlightApp/AccessibilityIDs.swift`.

`schemaVersion` is currently `"4"` (see `AccessibilityIDs.swift:30`). External
tests and AI agents should check this value before relying on individual IDs:

```swift
AccessibilityID.schemaVersion  // currently "4"
```

Rules for schema maintenance:

- **Adding a new control:** add the constant to the `AccessibilityID` enum
  first, then add a row to `docs/ax-id-reference.md` and a row to the GUI ↔
  CLI table above. The consistency-check script
  (`scripts/check_docs_consistency.py`) will fail until the reference doc is
  updated.
- **Renaming or removing an ID:** bump `schemaVersion` in
  `AccessibilityIDs.swift` and update the `**Schema version:**` line in
  `docs/ax-id-reference.md` in the same commit. The existing ID value becomes
  dead — external tests that hard-code the old string will fail loudly.

The flat reference list with display labels and control types is at
`docs/ax-id-reference.md`. The consistency-check script enforces that the set
of IDs in `ax-id-reference.md` exactly matches the set of string literals in
the Swift enum.

---

## Driving with cua-driver

cua-driver is an AX-tree-based tool for driving macOS apps. It works on any
AX-instrumented app without requiring an XCTest target or in-process
injection. This is the recommended path when an AI agent needs to observe and
interact with the Phase 01 SwiftUI app from outside the process.

The script below is a Phase 01 deliverable — it does not yet exist in the
repository. The documentation is valid once it ships:

```bash
# Run the full AX-tree self-test against the running ScanlightApp:
# (Phase 01 work-in-flight; doc remains valid once shipped)
scripts/cua-drive-swift-app.sh --selftest
```

Typical workflow for driving a GUI action via cua-driver:

1. **Snapshot the AX tree.** Launch the app and ask cua-driver to dump the
   accessibility tree. Each node has an `AXIdentifier` attribute.
2. **Look up the target ID.** Use the ID from the [AX-ID schema](#ax-id-schema)
   section above (e.g., `btn-connect`) to locate the node.
3. **Perform the action.** Call the cua-driver click / type / read API on the
   node. For buttons: click. For text fields: type the value. For labels: read
   the current `.AXValue` or `.AXLabel`.
4. **Assert state.** After the action, re-snapshot the AX tree and assert that
   the label or value of the relevant node has changed as expected (e.g.,
   `lbl-connection-status` should change from "Disconnected" to "Connected"
   after clicking `btn-connect`).

ID stability guarantee: the strings in `AccessibilityID` never change without
a `schemaVersion` bump. Any automation built on these IDs will continue to
work across app updates as long as the schema version matches.

---

## Driving with XCTest UI

XCTest UI tests run in-process and can read and write accessibility values
directly. This is the recommended path for white-box testing of the SwiftUI
app logic — it has access to the full XCTest assertion API and runs via
`swift test`.

Example: click the Connect button and assert the connection status updates.

```swift
import XCTest

class ScanlightAppUITests: XCTestCase {
    func testConnectButtonChangesStatusLabel() throws {
        let app = XCUIApplication()
        app.launch()

        // Locate the Connect button by its stable AX identifier.
        let connectBtn = app.buttons[AccessibilityID.connectButton]
        XCTAssertTrue(connectBtn.waitForExistence(timeout: 2))
        connectBtn.tap()

        // Assert the status label updated.
        let statusLabel = app.staticTexts[AccessibilityID.connectionStatusLabel]
        XCTAssertEqual(statusLabel.label, "Connected")
    }
}
```

Run via:
```bash
swift test --package-path phase3/FilmScanner
```

The `AccessibilityID.connectButton` constant equals `"btn-connect"` — the same
string that cua-driver looks up in the AX tree. Both automation paths share
the same vocabulary.

---

## Driving from Python

Driving `scanlight-swift-cli` from Python via subprocess is the recommended
approach for headless CI. No hardware is required when using `--fake`; the
`selftest` subcommand always uses FakeTransport regardless.

The canonical pattern is from `scripts/test_swift_cli.py`:

**Step 1 — Build the binary (once per session):**

```python
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWIFT_PROJECT = REPO_ROOT / "phase3" / "FilmScanner"
CLI_BINARY_NAME = "scanlight-swift-cli"

def _swift_executable_path() -> Path:
    """Resolve the built CLI binary. Expects .build/debug/ or .build/release/."""
    for build_flavor in ("debug", "release"):
        candidate = SWIFT_PROJECT / ".build" / build_flavor / CLI_BINARY_NAME
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        f"Swift CLI binary not found. Run `swift build` in {SWIFT_PROJECT} first."
    )

# In a pytest session-scoped fixture:
subprocess.run(
    ["swift", "build", "--package-path", str(SWIFT_PROJECT)],
    check=True,
    capture_output=True,
)
swift_cli = _swift_executable_path()
```

**Step 2 — Invoke commands and parse JSON:**

```python
import json

def run_cli(swift_cli: Path, *args: str, expect_rc: int = 0) -> dict:
    """Invoke the CLI, assert exit code, return parsed JSON."""
    cmd = [str(swift_cli), *args]
    if "--json" not in args:
        cmd.append("--json")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == expect_rc, (
        f"unexpected rc {proc.returncode} (expected {expect_rc})\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    stdout = proc.stdout.strip()
    if not stdout or not stdout.startswith("{"):
        return {"_raw_stdout": stdout, "_raw_stderr": proc.stderr}
    return json.loads(stdout)
```

**Step 3 — Example invocation:**

```python
# status with fake transport — no hardware required:
result = run_cli(swift_cli, "status", "--fake")
assert result["ok"] is True
assert result["firmware_id"] == 1
assert result["hardware_id"] == 1
assert result["default_rgb"] == [255, 200, 180]
assert result["led_temp_c"] == pytest.approx(32.5, abs=0.01)
assert result["vbus_mv"] == 5050
```

Expected JSON shape for `status --fake --json`:
```json
{"command":"status","default_rgb":[255,200,180],"firmware_id":1,"hardware_id":1,"led_temp_c":32.5,"ok":true,"vbus_mv":5050}
```

For the full live reference, see `scripts/test_swift_cli.py`.

---

## Decision matrix

| Goal | Recommended surface | Why |
|---|---|---|
| Run regression in CI without hardware | `scripts/test_swift_cli.py` (Python pytest harness) | Hermetic; uses `--fake` transport; subprocess-based; standard pytest runner |
| Drive the SwiftUI app from an AI agent | cua-driver | AX-tree-based; works on any AX-instrumented app; no XCTest target required |
| White-box test of SwiftUI app logic | XCTest UI tests | In-process; reads AX values directly; integrated with `swift test` |
| Headless integration test of capture pipeline | `tests/integration/` pytest (Phase 02 deliverable — work-in-flight if Phase 02 not yet landed) | Native Python; reuses existing fixtures |
| Operator-driven, ad-hoc Scanlight control | `scanlightctl` (Python) or `scanlight-swift-cli` (Swift) | Same surface, two implementations; standard Unix tool behavior |
| Quantitative calibration verification | `scripts/inspect-calibration.py` | Reports per-channel vignette, saturation, tint drift with decision tiers |
| Composite a single R/G/B triplet | `rgb-composite` | Direct: three ARWs in, one TIFF/DNG out; optional FFC |
| Composite a whole roll | `batch-composite` | Walks roll dir, parallelizes per-frame compositing, writes composites/ |
| Per-frame capture orchestration | SwiftUI app → `triplet-capture` | Swift app is the operator surface; Python backend sequences R/G/B and advances frame counter on success only |

---

## Failure modes

### scanlight-swift-cli

| Exit code | Meaning | JSON `ok` field |
|---|---|---|
| 0 | Success | `true` |
| 1 | Operational failure (port open failed, transport error, driver rejected input) | `false` with `"error"` key |
| 2 | Bad arguments (unknown flag, out-of-range `--level`, missing required argument) | `false` with `"error"` key |

In `--json` mode, every failure includes the common envelope with `"ok": false`
and an `"error"` key describing the problem.

**Hardware absent:** use `--fake` to run against `FakeTransport`; `selftest`
always uses `FakeTransport` automatically. Real-port commands will fail with
exit 1 and a port-open error if the device is not connected.

**Pulse validation:** `pulse_shutter_rejects_invalid` step in `selftest`
covers the validation. Invalid inputs (not in [10, 2550] ms, not a multiple
of 10) exit 1 from the driver layer.

### scanlightctl

Exit 1 means an exception was raised by the driver layer. The message goes
to stderr prefixed `scanlightctl:` (`phase1/scanlightctl/scanlight/cli.py:177-179`).
Exit 130 is a clean `KeyboardInterrupt`.

**Hardware absent:** `scanlightctl` requires a real serial port. There is no
`--fake` flag on the Python CLI surface. Tests inject a fake via the driver
layer, but this is not exposed on the CLI.

### scripts/inspect-calibration.py

| Exit code | Meaning |
|---|---|
| 0 | All channels pass usable thresholds (CLEAN or OK-with-FFC) |
| 1 | Any channel exceeds redo-cal thresholds — optics problem; do not scan |
| 2 | R.ARW, G.ARW, or B.ARW missing in the cal dir, or files cannot be decoded |

The script is read-only; it never modifies the calibration directory.

**Hardware absent:** the script reads ARW files, not hardware. Run it any
time after `scripts/capture-calibration.sh` has produced a calibration dir.

### triplet-capture

- A frame **aborts without advancing the frame counter** if any RAW is the
  wrong size (outside the plausible band in `orchestrator.py`
  `PLAUSIBLE_RAW_MIN_BYTES` / `PLAUSIBLE_RAW_MAX_BYTES`), or in `hw`/`manual` mode
  if no file lands in `--ied-inbox` within `--capture-timeout-s`.
- The **ambiguous-inbox guard** aborts a channel if more than one new `.ARW`
  becomes stable at the same time. Existing `.ARW` files are quarantined at
  frame start so a stale file cannot be assigned to the wrong channel.
- Stale or late files that arrive after the timeout are quarantined (moved
  aside), not processed.
- **Hardware absent:** `triplet-capture` requires hardware unless tests inject
  a fake runner or the operator deliberately pre-populates `--ied-inbox`.

### rgb-composite

Non-zero exit on:
- Dimension mismatch between R, G, B inputs (the three ARWs must be the same
  sensor size — indicates film shifted between captures).
- Calibration error during FFC (saturated cal frames, missing channel files).
- Input ARW not found.

**Hardware absent:** needs ARW files, not hardware. Accepts any path to valid
ARW files.

### batch-composite

Non-zero exit on:
- `roll_dir` is not a directory.
- Any unexpected exception during per-frame compositing (logged and re-raised).
- Empty-roll discovery is not a failure — it logs a warning and exits 0.

**Hardware absent:** needs ARW files in the roll directory, not hardware.
