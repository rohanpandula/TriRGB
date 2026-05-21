# HANDOFF — film scanner build, resume on hardware arrival

> **For the next Claude session:** Point me at this file when hardware arrives. It tells you (a) exactly what was built before HW, (b) what's verified vs. assumed, (c) where the assumptions can break, (d) the exact order to do things on plug-in day. Read **all** of this before touching the hardware.

Last updated: 2026-05-13. Built against PROJECT.md and the upstream `jackw01/scanlight` repo + Sony Camera Remote SDK v2.01.

---

## For AI agents / automation runners

If you are an AI agent (Claude, Codex) or a CI runner picking this
project up, your entry point is
[`docs/automation.md`](docs/automation.md), not this file. That doc
documents every automatable surface (CLIs, Swift CLI, scripts, the
Phase 01 SwiftUI app's AX-ID schema), the JSON contract / exit codes
/ failure modes for each, and the decision matrix for which surface
to use when. The flat AX-ID list is at
[`docs/ax-id-reference.md`](docs/ax-id-reference.md).

The rest of this file is for humans bringing the hardware up. It's
fine to read if you want to understand the deployment history and
the assumptions that need verification on plug-in day, but the
contracts you need to drive the system are in
`docs/automation.md`.

---

## Status by phase

### Phase 1 — Plumbing

**Goal:** from a terminal, independently (a) switch Scanlight channels and (b) capture+download a RAW from the a7CR.

| Deliverable | Path | Status | What works in software | What's HW-gated |
|---|---|---|---|---|
| 1A `scanlightctl` | `phase1/scanlightctl/` | Software-complete, 42/42 tests | Protocol codec, driver class, CLI, fake-serial coverage of every error path | Auto-discovery against real Pico VID:PID `2E8A:000A`; actual serial round-trip |
| 1B `sony-capture` | `phase1/sony-capture/` | Software-complete, builds clean, no-camera path verified | SDK loads via `@rpath`, `Init`→`EnumCameraObjects` runs, error path returns clean exit 1, in-tree SDK install (~56MB, gitignored) | `Connect`, `SetSaveInfo`, `SendCommand(Release)`, `OnCompleteDownload`, atomic rename — all need the camera |
| 1C `docs/optical_dry_run.md` | `docs/` | Complete | Paper checklist | (Operator runs it once with film + Scanlight) |
| **Phase 1 exit criteria** | — | **HW-blocked** | — | `scripts/smoketest.sh` is the literal sequence from PROJECT.md, ready to run on plug-in day |

### Phase 2 — Capture pipeline (no live preview)

**Goal:** scan a full roll; end-state is a directory of three RAWs per frame plus a directory of 16-bit TIFF composites.

| Deliverable | Path | Status | What works in software | What's HW-gated |
|---|---|---|---|---|
| 2A `triplet-capture` | `phase2/triplet-capture/` | Software-complete, 16/16 tests | Flask single-page UI, R→G→B sequencing, frame-counter advance-only-on-success, retake/overwrite semantics, JSONL action log, plausible-RAW-size guard, Flask routes | Real subprocess hand-off to `sony-capture`; settle-time tuning against actual LED step-response |
| 2B `rgb-composite` | `phase2/rgb-composite/` | Software-complete, 15/15 tests (incl. real ARW) | Channel-from-correct-input invariant, dimension-mismatch abort, TIFF writer with colorspace metadata + sidecar, rawpy params locked to PROJECT.md, **end-to-end rawpy decode verified against a real a7CR ARW** | Three actual narrowband-RGB exposures of the same frame — only achievable after Phase 1 smoke |
| 2C `batch-composite` | `phase2/batch-composite/` | Software-complete, 14/14 tests | Roll discovery (frame number + roll-name keyed), parallelism cap to bound memory, missing-channel skip, overwrite flag | — (purely orchestrational around 2B) |
| **Phase 2 exit criteria** | — | **HW-blocked** | — | 36 TIFFs from a real roll that invert correctly in FilmLab / NLP |

### Phase 3 — Live preview app

**Goal:** replace Phase 2's disposable UI with a native macOS app that adds an inverted live preview during framing.

| Deliverable | Path | Status | Why |
|---|---|---|---|
| `ScanlightSwift` package (protocol + driver) | `phase3/FilmScanner/Sources/ScanlightSwift/` | Scaffolded, 21/21 tests | Direct Swift port of the Python driver, same fake-transport test pattern. `SerialPortTransport.swift` compiles but is not HW-verified — the real round-trip needs the device. |
| SwiftUI shell (Framing/Capture modes) | `phase3/FilmScanner/App/` (not created) | **Not built** | Deferred — nothing to render until the Sony bridge can deliver JPEG live-view frames, which is HW-only. |
| Sony SDK Obj-C++ bridge | `phase3/FilmScanner/Sources/SonyBridge/` (not created) | **Not built** | Deferred — the entire reason to write the bridge is live view, which is HW-only. The capture path is already covered by 1B and the Phase 3 app can simply shell out to `sony-capture` for triplets in v1. |
| Core Image filter chain (invert + WB + tone curve) | `phase3/FilmScanner/Sources/PreviewPipeline/` (not created) | **Not built** | Deferred — the per-stock WB-neutralization matrix coefficients are derived from real Phase 2 output (a calibration step that runs after Phase 2 smoke). Writing them now would be guessing. |
| **Phase 3 exit criteria** | — | **HW + Phase 1+2 smoke required first** | Live preview at 15–30 fps with inverted color, mode-switch without dropping the USB connection, byte-identical output to Phase 2. |

### Cross-cutting tooling (built, HW-free)

| Path | Purpose |
|---|---|
| `scripts/diagnose.py` | First-thing-on-plug-in 7-step verifier — serial ports, Pico VID:PID, Scanlight handshake, telemetry, VBUS sanity, sony-capture binary, SDK camera enumeration |
| `scripts/smoketest.sh` | Phase 1 exit-criteria sequence as a single command |
| `docs/calibration_notes.md` | Per-stock R/G/B level table to fill in during the optical dry run |
| `README.md` | Project navigation |
| `PROJECT.md` | Canonical brief (untouched) |
| `HANDOFF.md` | This file |

### Open items from PROJECT.md §"to confirm by physical test"

| # | Item | Status |
|---|---|---|
| 1 | End-to-end capture+download time per RAW on this hardware | OPEN — plug-in only |
| 2 | Sony SDK live view resolution and fps for the a7CR | OPEN — plug-in only |
| 3 | Sony SDK dylib architecture on macOS | **CLOSED** — universal (arm64 + x86_64), no Rosetta needed |
| 4 | Per-film-stock R/G/B calibration | OPEN — requires film, Scanlight, optical dry run |

### Totals

- **92 tests** all green (42 + 15 + 14 + 16 + 21, including 5 real-ARW integration tests)
- **1 codex audit** done, 5 findings fixed (2 MEDIUM + 3 LOW), regression tests added
- **Real-ARW pipeline verified** — `rgb-composite` runs end-to-end against an a7CR ARW from the user's existing scan archive (`/Volumes/FilmscanWorkingDrive/.../Roll2-070.ARW`). One bug caught and fixed (rawpy 0.27 requires `user_wb` as a `list`, not a tuple).
- **Sony SDK installed in-tree** at `phase1/sony-capture/third_party/sony_sdk/`, ~56 MB, gitignored. Quarantine stripped. Binary loads dylib via `@rpath` and runs the no-camera error path cleanly.
- **3 deferred Phase 3 pieces** intentionally not built (SwiftUI shell, Sony bridge, filter chain) — each has a specific reason that resolves after Phase 1+2 smoke

See "Codex audit" below for what the review caught.

---

## Pre-flight checklist (operator side, before plug-in)

Pick a configuration first (see PROJECT.md §Hardware architecture):

- **Configuration A** — USB tether, SDK fires the shutter. No 3.5mm cable. Default.
- **Configuration B** — Wi-Fi tether via Imaging Edge Desktop, Scanlight fires the shutter via 3.5mm. Faster on the trigger side; transfer time is Wi-Fi-bound.

Verify these before powering anything:

**Both configurations:**
- [ ] `brew install cmake` (sony-capture build path; only required for Configuration A)
- [ ] Camera body settings:
  - PC Remote mode **on**
  - File format: **RAW (lossless compressed)**
  - Save destination: **PC**
  - Mode: M (manual exposure)
  - ISO 100, IBIS off, electronic shutter or EFCS, fixed WB, manual focus, all in-camera corrections off
- [ ] Wall PSU for the Scanlight RIGHT port (≥5V/2A USB-C charger; the brick from any modern phone is fine)
- [ ] Mac is on AC during a scanning session (the SDK and IED can both stutter under aggressive App Nap)
- [ ] A *known good* film strip for the optical dry run — something you've scanned successfully before, or a fresh Portra/HP5 with normal density

**Configuration A only:**
- [ ] USB-C **data** cable for camera, not charge-only. Verify by plugging the camera into a Mac you trust and confirming the body appears as a USB device, not just charging.

**Configuration B only:**
- [ ] Sony USB-C → 3.5mm trigger cable (the camera's USB-C port carries trigger pins; the cable converts them to a 3.5mm jack that mates with the Scanlight's shutter output).
- [ ] Imaging Edge Desktop installed and paired with the a7CR over Wi-Fi. Save folder set to a dedicated inbox path (e.g. `/Volumes/SSD/_ied_inbox/`).
- [ ] 5 GHz Wi-Fi between AP and camera, with the AP close enough that single-RAF transfer stays under ~6 s. Bench-test by firing one shot in IED before committing to a roll.

---

## Plug-in day — exact sequence

Run these top to bottom. Each step verifies an assumption before the next one runs.

### Step 0 — Verify the in-tree SDK install (already done)

The Sony SDK is unzipped into `phase1/sony-capture/third_party/sony_sdk/` and Gatekeeper quarantine has been stripped. `phase1/sony-capture/build/sony-capture` already builds clean and the no-camera error path runs (see `phase1/sony-capture/third_party/sony_sdk/INSTALL.md` if a clean re-install is needed).

To confirm the install survived (e.g., after a `git clean` or fresh clone where someone re-extracted the SDK), one command:

```bash
xattr -dr com.apple.quarantine phase1/sony-capture/third_party/sony_sdk
```

Idempotent — safe to run any time.

### Step 1 — Plug in only the Scanlight first

LEFT port → Mac via USB-C data cable.
RIGHT port → wall PSU via USB-C.

Run:

```bash
python3 scripts/diagnose.py --skip-camera
```

What this verifies:
1. A serial port shows up
2. It matches the Pico CDC VID:PID **2E8A:000A** (my best-guess assumption from the firmware using `pico_enable_stdio_usb` — see assumption #1 below)
3. Scanlight handshake works — `get_fw_version()` returns
4. Telemetry stream arrives within 1 s (LED_TEMP + VBUS every 200 ms)
5. VBUS reads ≥ 4500 mV (confirming the RIGHT-port wall PSU is connected)

If step 2 fails with "no Pico CDC ports found" — it's the assumption breaking, not a real problem. Look at the port list step 1 printed; the Scanlight will be one of them. Pass `--port /dev/cu.usbmodemXXX` to override.

**If diagnose passes, do one extra manual sanity check:**

```bash
scanlightctl on r --level 200    # device should turn solid red
scanlightctl on g --level 200    # solid green
scanlightctl on b --level 200    # solid blue
scanlightctl on w --level 200    # solid white
scanlightctl off                 # off
scanlightctl status              # prints fw version, temp, vbus
```

If all four colors light up, you're done with the Scanlight side.

### Step 2 — Plug in the camera

Camera USB-C → Mac. Dummy battery → wall power. Set the body to PC Remote mode (see pre-flight checklist).

Build sony-capture (or rebuild if Step 0 was the first time stripping quarantine):

```bash
cd phase1/sony-capture
# Install the SDK first if you haven't:
#   - Unzip CrSDK_v2.01_Mac.zip somewhere
#   - ln -sf /that/unpacked/path/app      third_party/sony_sdk/app
#   - ln -sf /that/unpacked/path/external third_party/sony_sdk/external
# Then:
cmake -B build && cmake --build build
```

Run the full diagnose:

```bash
python3 scripts/diagnose.py
```

Step 7 is the new one — it runs the sony-capture binary with a short timeout to see if the SDK enumerates the camera. If the camera is in PC Remote mode and the cable is data-capable, you'll see "camera enumerated by SDK" (the binary won't actually capture because the timeout is short and no shutter is fired — that's fine).

### Step 3 — Smoke test (Phase 1 exit criteria)

This is the test PROJECT.md calls the Phase 1 exit criteria:

```bash
scripts/smoketest.sh /tmp/smoke
```

It cycles `scanlightctl on r` → `sony-capture` → `on g` → `sony-capture` → `on b` → `sony-capture` → `off`, then verifies three .ARW files of plausible size (40–120 MB each) landed on disk.

This is the moment that confirms Phase 1 actually works end-to-end. If this passes, every assumption in the codebase has been validated.

**Configuration B only — additionally verify the hardware-trigger path:**

```bash
# 1. With IED running and watching /Volumes/SSD/_ied_inbox/, fire one shot
#    via the 3.5mm jack and confirm a file lands.
scanlightctl on r --level 200
scanlightctl pulse 100        # 100 ms shutter pulse
scanlightctl off
ls -la /Volumes/SSD/_ied_inbox/ # one new .ARW within ~10 s on 5 GHz Wi-Fi

# 2. End-to-end via the orchestrator (one frame):
triplet-capture \
    --roll-name SmokeHW \
    --output-folder /Volumes/SSD/_smoke_hw \
    --trigger-mode hw \
    --ied-inbox /Volumes/SSD/_ied_inbox \
    --shutter-pulse-ms 100 \
    --capture-timeout-s 30
# Click Capture once in the web UI. Confirm three files end up in
# /Volumes/SSD/_smoke_hw/SmokeHW/ with the canonical naming.
```

If the IED inbox file doesn't appear within `--capture-timeout-s` of the pulse, you have a Wi-Fi or PC Remote pairing problem; the SDK path (Configuration A) is the fallback.

### Step 4 — Optical dry run

Paper deliverable. Walk through `docs/optical_dry_run.md` with a real film strip. Note the per-channel working levels in `docs/calibration_notes.md`.

### Step 5 — First real roll

```bash
triplet-capture --roll-name TestRoll --output-folder /Volumes/SSD/Scans
# → http://127.0.0.1:8765
```

Adjust the per-channel levels in the UI to match what you noted in calibration_notes.md. Scan a few frames, advancing the Valoi by hand.

### Step 6 — Composite

```bash
batch-composite /Volumes/SSD/Scans/TestRoll
```

Open the resulting `/Volumes/SSD/Scans/TestRoll/composites/*.tif` in FilmLab or NLP. Invert. The positive should look correct in color and density. If it doesn't, the failure is *downstream* of this codebase — see the troubleshooting matrix below.

---

## Assumptions that need HW to verify

These are the places I made a best-guess that might be wrong. Each has a how-to-check.

### Assumption 1 — Scanlight USB VID:PID is `2E8A:000A` — **CONFIRMED FROM CODE (2026-05-20)**

**Resolved against the upstream firmware build** (`jackw01/scanlight` @ c8bf780):
`firmware_bsl1/CMakeLists.txt` enables USB CDC via the stock
`pico_enable_stdio_usb` on **both** build targets (`bsl1_controller`,
`sl4_controller`) and ships **no custom USB descriptor / tusb_config** (none
exists anywhere in the firmware tree). So the device enumerates with the Pico
SDK default `2E8A:000A`. `discover_port()` already matches VID `0x2E8A` + PID
`{0x000A, 0x0009}` with a name/usbmodem/override fallback chain, so it is
correct regardless. No longer a guess.

**Still worth a glance on plug-in:** diagnose.py step 2 prints the actual
VID:PID. Only a future firmware that adds a custom descriptor would change it.

### Assumption 2 — Default baud rate 115200 — **CONFIRMED FROM CODE (2026-05-20)**

The canonical web app (`app_bsl/src/protocol.js`) hardcodes
`UART_BAUD_RATE = 115200` and opens the port with `{ baudRate: 115200 }`. The
firmware uses `stdio_init_all()` over USB CDC, where baud is a virtual no-op
(ignored by the device). Our `DEFAULT_BAUDRATE = 115200` matches the web app
exactly — not a guess anymore. (If `status` ever hangs after open it's a port
or self-test-timing issue, not baud.)

### Assumption 3 — RAW file size band 40–120 MB

`triplet-capture` aborts a frame if any of the three RAWs is outside this band. PROJECT.md states 60–80 MB for the a7CR. Real a7CR lossless compressed RAWs may land at the low or high end depending on image content. If you hit a "implausible size" abort on a real capture but the file actually looks fine, widen the band in `phase2/triplet-capture/triplet_capture/orchestrator.py` (`PLAUSIBLE_RAW_MIN_BYTES` / `PLAUSIBLE_RAW_MAX_BYTES`).

### Assumption 4 — rawpy linear-demosaic produces correct color

The *rawpy invocation* is verified end-to-end against a real a7CR ARW (`/Volumes/FilmscanWorkingDrive/.../Roll2-070.ARW`) via `phase2/rgb-composite/tests/test_real_arw.py` — 5 integration tests confirm:
- Output is uint16 HxWx3
- Dimensions match the a7CR (6336×9504 active)
- Values are linear (not gamma-corrected)
- The composite pipeline writes a round-trippable 16-bit TIFF
- Channel selection is preserved through tifffile read

The *channel-from-correct-input* invariant (R from R-lit ch0, etc.) is verified by mocked tests with synthetic distinct values. The only thing left for real-HW verification is whether three *different* narrowband exposures of the same physical frame produce a colorimetrically correct composite — which requires the Scanlight.

**How to verify against narrowband captures:** after Step 3 above produces three real ARWs, run:

```bash
rgb-composite \
  --r /tmp/smoke/Smoke_Frame001_R.ARW \
  --g /tmp/smoke/Smoke_Frame001_G.ARW \
  --b /tmp/smoke/Smoke_Frame001_B.ARW \
  --out /tmp/smoke.tif
```

Open the TIFF in any 16-bit viewer (or import into FilmLab). It should look like a *positive-numbers representation of a negative*: the film base = high values, deepest image areas = low values. Inversion in FilmLab/NLP produces the final positive.

If the output looks wrong (e.g., heavy cyan cast, banding, posterization), the bug is in the rawpy invocation. The first thing to check: are the demosaic kwargs exactly what `test_demosaic_kwargs_match_project_md` says? If yes, the rawpy version may be misbehaving.

### Assumption 5 — Sony SDK lifecycle order is correct

`sony-capture` follows the order from the official `RemoteCli.cpp` + `CameraDevice.cpp` sample: Init → EnumCameraObjects → CreateCameraObjectInfo → Connect → SetSaveInfo → SendCommand(Release, Down) → SendCommand(Release, Up) → wait OnCompleteDownload → Disconnect → ReleaseDevice → Release. If a capture *almost* works (camera fires) but the file doesn't show up, look at the SetSaveInfo path — the SDK may have written to a different location than expected.

### Assumption 6 — Settle time of 50 ms between LED switch and capture

PROJECT.md says 50 ms is the starting point. If you see color shifts or low contrast on a single channel, the LED may not have reached steady-state brightness. Bump `settle_ms` in the triplet-capture UI to 100 or 150 ms.

### Assumption 7 — Swift `SerialPortTransport` works against the real device

The Swift port (Phase 3 scaffold) was tested only against an in-memory fake transport. The POSIX serial code (`open(2)` + `tcsetattr` + non-blocking `read(2)`) is straightforward but unverified.

**This doesn't matter yet** — Phase 3 is deferred. When it becomes relevant, the Python `Scanlight` class is the reference; if the Swift behavior diverges, port-by-port comparison against the Python driver is the test.

---

## Codex audit (already applied)

Independent review pass against PROJECT.md + the upstream firmware/SDK references found 5 issues, all fixed:

1. **sony-capture handle leak on OnConnected timeout** (MEDIUM) — `Connect` returning success ≠ `OnConnected` firing. Now tracks `handle_allocated` separately from `connected`.
2. **`OnError` didn't unblock waits** (LOW) — added `last_error_ != 0` to predicates so async errors return promptly.
3. **`SendCommand(Release)` return codes ignored** (LOW) — both Down and Up now checked; Up always sent.
4. **batch-composite ignored roll name when grouping** (MEDIUM) — keyed on frame number only; now `(roll, frame)`.
5. **batch-composite default worker count could OOM** (MEDIUM) — `os.cpu_count()` × 1.4 GB per worker on a 16-core box → 22 GB. Capped at 4 by default.

Regression tests added for #4 and #5.

---

## Known spec / upstream discrepancies

These are *real* discrepancies I found between PROJECT.md / spec docs and the actual upstream code. Decisions documented inline in the code:

1. **Scanlight wire endianness**: PROJECT.md and `bsl_control_interface.md` say or imply little-endian for LED_TEMP / VBUS / FW_VERSION. The firmware emits **big-endian** (MSB-first via `protocol_send_packet_uint32`). My implementation matches the firmware (ground truth). Noted in `phase1/scanlightctl/scanlight/protocol.py` docstring.

2. **FW_VERSION field order**: `bsl_control_interface.md` says bytes 0-1 = FW, 2-3 = HW. Firmware computes `FW + (HW << 16)` as big-endian → wire bytes are `[HW_hi, HW_lo, FW_hi, FW_lo]`. The low 16 bits of the word are FW; the high 16 bits are HW. My implementation matches the official web app's `dataView.getUint32(0); fw = w & 0xFFFF; hw = w >> 16`.

3. **PROJECT.md endianness claims** — corrected on 2026-05-18 after a subagent + Codex audit of the upstream firmware. PROJECT.md now reads "BE" with an explicit callout citing the firmware source.

---

## Firmware behavior gotchas (from upstream source audit, 2026-05-18)

These are real behaviors of the Scanlight v4 firmware (`automation/firmware_bsl1/main.c` in the upstream repo) that aren't in the BSL doc and could surprise an operator or trip an automated pipeline. None require code changes today; they require operator awareness or one-line doc warnings.

- **Power-cycle the Scanlight before a long session.** The firmware's `shutter_pulse_timer` uses an `int32_t` while `millis` is `uint32_t`; after ~24.8 days of continuous uptime the timer can overflow and the shutter GPIO can latch high. Practical risk is low (the device is rarely on for weeks straight) but worth a hard reset before a serious scanning day.
- **Thermal protection is unreliable when USB power is healthy.** The firmware checks LED temp every 200 ms and *intends* to enter `OperatingModeOff` at 80 °C, but the following code path overwrites `operating_mode` based on VBUS/CC unconditionally — and if VBUS is healthy (which it always is on a wall PSU), the temp-Off is silently masked, the color-zeroing branch never fires, and the LEDs stay at their current brightness. Color array is only wiped if VBUS *also* drops below 4400 mV. Practical implication: **don't rely on the scanlight to protect its own LEDs from overheating.** Watch the `LED_TEMP` telemetry stream yourself — `diagnose.py` now warns above 70 °C. If you see telemetry climbing into the 70s during a long session, back off and let it cool.
- **First telemetry packet arrives ~600 ms after power-on, not immediately.** `adc_reporting_timer` is initialized to 1, so the first ADC read happens within ~10 ms of the main loop starting. That triggers a mode transition from boot-time `Off` to a 5V/9V mode, which fires the four-LED visual self-test (4 × 150 ms `busy_wait_ms`). The first `LED_TEMP` / `VBUS` packet pair is emitted at the *end* of that same tick, after the self-test completes. So an automated tool that opens the port immediately after plug-in should be prepared to wait up to ~2 s (Pico USB enumeration + 600 ms self-test) before telemetry starts flowing.
- **Pressing the left physical button is mostly safe — except after a software "off".** Left button toggles `led_enable`. If you press it when `led_enable=1` and any channel is non-zero, it just toggles the LEDs on/off without changing the color array — so the next software command picks back up cleanly. **But** if you press it after `scanlightctl off` (which left `color[]` all zero) and *then* press it again to turn back on, the firmware substitutes the NVM RGB preset into `color[]`. So: don't reach over the device mid-roll, but specifically avoid the off-on cycle on the left button while the software thinks the light is in a known state.
- **VBUS threshold mismatch is intentional.** Firmware shuts off below 4400 mV (`USBVBUSThreshold5V`); our `diagnose.py` warns below 4500 mV. That 100 mV cushion is on purpose — if you ever hit 4450 mV on a real PSU, the firmware will still run but you're flirting with the shutoff. Don't "fix" the diagnose threshold to 4400 without removing the safety margin.
- **5 V / 2 A is a recommendation, not enforcement.** Firmware does not throttle brightness based on USB power class on v4 (`PowerLimitRGB = {0,255,255,255,255}` for all operating modes). A 5 V / 0.5 A charger will run the LEDs at full brightness until VBUS browns out below 4400 mV. The 2 A spec is for headroom and reliability, not a firmware gate.
- **Upstream web app's firmware-version table is stale.** `automation/app_bsl/src/config.js` only lists FW ID 0 → "v1.0.0", but current firmware (`automation/firmware_bsl1/config.h`) reports FW ID 1. The web app will display "Unknown firmware version" and a spurious "Update available" prompt when connected to current firmware. Ignore — FW ID 1 = v1.1.0 = current, per `bsl_control_interface.md`. We use raw integer IDs internally so this doesn't affect us.
- **Manual firmware flash** (in case the web app's DFU button is unavailable): hold the Scanlight's DFU button while plugging in the LEFT USB-C port → an `RPI-RP2` volume mounts on the Mac → run `python3 /tmp/scanlight/automation/autoflasher.py /tmp/scanlight/automation/sl4_controller_v1.1.uf2` (or copy the `.uf2` file directly onto the mounted volume). The `.uf2` lives at `automation/` in the upstream repo, not at the repo root. Use this if you ever need to recover from a bricked or development firmware.

- **`PKT_D2H_ACK` (header 0) is a dead opcode.** The upstream `protocol.h` declares it but no firmware send-site emits it (confirmed against `firmware_bsl1/main.c` + `protocol.c`). Our `protocol.py`/`Protocol.swift` now define `D2H_ACK = 0` for a complete protocol mirror, but never poll or wait for an ACK frame — you won't receive one.

---

## Protocol re-verification against jackw01/scanlight (2026-05-20, @ c8bf780)

Full byte-for-byte re-check of our serial codec against the two ground-truth
implementations — the firmware (`firmware_bsl1/main.c`, `protocol.c`) and the
canonical web app (`app_bsl/src/protocol.js`). **Every field matched; no codec
changes were needed.** Specifically verified:

- **Framing** `0xFE | header | length | data[length]` — identical in our
  `encode_packet`, the firmware RX state machine, and the web app.
- **`SET_COLOR`** payload is `R,G,B,W,IR,save` — firmware does
  `memcpy(color, data, 5)` then treats `data[5]` as the save flag
  (`main.c` ~line 265). Matches our `[r,g,b,w,0,save]`.
- **`FW_VERSION`** = `FW_ID + (HW_ID << 16)` sent big-endian — matches our
  `fw = word & 0xFFFF`, `hw = word >> 16`.
- **`SHUTTER_PULSE`** = one byte × 10 ms (`shutter_pulse_timer = millis +
  data[0] * 10`). Matches our `pulse_ms // 10`.
- **`LED_TEMP` / `VBUS`** are sent as **signed** `int32` big-endian
  (`protocol_send_packet_int32`). Matches our signed BE decode.
- **`DEFAULT_RGB`** = 3 bytes R,G,B. Matches.
- **Baud / VID:PID** — see Assumptions 1 & 2 above (both now confirmed).

**Hardware variant — confirm on plug-in.** The firmware has two build targets:
`bsl1_controller` (HW_VERSION_BSL1, id 0) and `sl4_controller`
(HW_VERSION_SL4, id 1). The Scanlight **v4 runs the SL4 build → HW id 1**. On
plug-in, `scanlightctl status` prints the HW id: **expect `hw=1`**. `hw=0`
would mean a BSL1 board (different power limits, an extra IR channel, TRIM
enabled). The wire protocol is identical either way — our code works for both;
only the device's internal behavior differs.

**Packets we intentionally don't drive (now documented in `protocol.py`):**
- **`SET_TRIM` (5) / `GET_TRIM` (6) / `D2H_TRIM` (5)** — per-channel signed NVM
  brightness trim. Compiled **only** for BSL1 (`#ifdef HW_VERSION_BSL1` in
  `main.c`); **no-ops on the v4 (SL4)**. We correct per-channel in software
  (FFC + per-channel levels), so device-side trim is unnecessary even on BSL1.
- **`DFU_MODE` (4)** — `reset_usb_boot()` into the RP2040 bootloader for
  flashing. Available on both boards; a manual/recovery operation only (see
  "Manual firmware flash"), never part of scanning.

**Two firmware behaviors worth re-stating** (both confirmed in `main.c`, both
already accounted for): every `SET_COLOR` sets `led_enable = 1` (sending a
color always turns output on), and white/RGB exclusivity is enforced
**device-side** (`update_pwm` zeros RGB when W>0) on top of our client guard.

---

## Troubleshooting matrix — what symptom maps to what bug

| Symptom | Likely cause | Where to look |
|---|---|---|
| `scanlightctl status` hangs forever | Wrong port; or auto-discovery picked a non-Scanlight port | `discover_port()` in `device.py`; try `--port` explicitly |
| `scanlightctl on r` succeeds but device doesn't light | Wrong USB descriptor matching — talking to a different device | Check VID:PID printed by diagnose.py step 1 |
| Gatekeeper dialog blocking `sony-capture` | SDK quarantine | `xattr -dr com.apple.quarantine` on the SDK tree |
| `EnumCameraObjects failed` | Camera in non-Remote mode, charge-only USB cable, no dummy battery | Verify body settings; swap cable |
| `Connect failed (CrError 0x...)` | Other host has the camera (Imaging Edge running?); body authentication enabled | Quit other tether apps; check body menu Authentication setting |
| Capture happens but no file written | Save destination on body is "Camera" or "Camera+PC" | Set to "PC" in body menu |
| Three RAWs land but composite looks wrong | rawpy params, file dimension mismatch, or rotation between captures | Check `rgb-composite` aborts on dim mismatch; verify rawpy kwargs unchanged |
| Composite has heavy cyan cast or banding | rawpy version mismatch, or wrong colorspace | Confirm `output_color=ProPhoto` and that the sidecar txt is being read by FilmLab |
| Frame counter advancing on failures | (Shouldn't happen — covered by tests) `_capture_one` should raise `TripletAbort`; check the test `test_failure_does_not_advance_frame` |
| One channel always clips / always crushes | Per-stock level mismatch | Optical dry run protocol → calibration_notes.md |
| Newton rings in scans | Film holder issue, not software | Check holder; see optical_dry_run.md |

---

## What's NOT in this codebase, intentionally

Per PROJECT.md §"What's explicitly out of scope":

- White-light scanning. White channel is only for live-preview framing.
- IR / dust mapping.
- RGB + W combined captures.
- Lightroom Classic export automation.
- Motorized film advance.
- Negative inversion (done downstream in FilmLab / NLP).

If a future phase changes any of these, update PROJECT.md first, then this file.

---

## Where to point Claude next session

When you say "look at HANDOFF.md and pick up", the next session should:

1. Read this file fully.
2. Read PROJECT.md.
3. Ask what you observed when you ran `scripts/diagnose.py` and `scripts/smoketest.sh`.
4. If both passed: move to Phase 3 (live preview app) — start from `phase3/README.md`, build out the Sony SDK Obj-C++ bridge and SwiftUI shell next.
5. If diagnose failed at a specific step: see the assumption-by-assumption section above for the right thing to change.
6. If smoketest failed but diagnose passed: the issue is in the per-channel capture cycle — most likely timing, most likely `settle_ms`.

The next session's first move should be running the tests to confirm nothing regressed:

```bash
(cd phase1/scanlightctl    && pytest)
(cd phase2/rgb-composite   && PYTHONPATH=. pytest)
(cd phase2/batch-composite && PYTHONPATH=. pytest)
(cd phase2/triplet-capture && PYTHONPATH=.:../../phase1/scanlightctl pytest)
(cd phase3/FilmScanner     && swift test)
```

87 tests, all green, baseline before any HW-driven changes.
