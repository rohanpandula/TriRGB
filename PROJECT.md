# Film Scanner Build — Project Brief

This document is the canonical brief for an automated narrowband-RGB film scanning system. Read it fully before writing code. Reread relevant sections before each phase.

---

## Mission

Build a Mac-driven workflow that captures a film negative as three sequential exposures (red, green, blue) under a narrowband RGB light source, automatically downloads and names the three RAW files, and later composites them into a 16-bit TIFF ready for inversion in FilmLab or Negative Lab Pro. Manual film advance, one-button triplet capture. End goal is high color quality matching professional drum/dedicated film scanners, not white-light scanning.

The user is fluent in photography and software. This system is for their personal film archive. Reliability matters more than feature breadth.

---

## Hardware in the system

- **Sony a7CR** (61 MP full-frame, USB-C, supported by Sony Camera Remote SDK v1.10+). Current verified control path is Wi-Fi PC Remote via Sony Camera Remote SDK host-PC auto-download; Imaging Edge Desktop remains the manual fallback.
- **Sony NP-FZ100 dummy battery / AC coupler** powering the camera (no battery during sessions; eliminates a failure mode).
- **Sony FE 90mm Macro** as primary scanning lens. Pentax 120mm adapted as fallback.
- **Scanlight v4** by jackw01. Has **two separate USB-C ports**:
  - **Left port:** USB CDC serial control. Connected to Mac.
  - **Right port:** power. Connected to a separate USB-C wall PSU (recommended ≥5V/2A; firmware accepts anything that holds VBUS ≥4.4 V — the 2 A spec is for headroom, not a firmware gate). **Never powered from the Mac.**
  - Has nine each of 665nm red, 525nm green, 455nm blue, and 5000K 95-CRI white LEDs. The white channel is independently controllable but firmware blocks RGB+W simultaneous operation.
  - Has a 3.5mm shutter jack. **Conditional on tether transport:**
    - If the camera tethers to the Mac over **USB**, do not use the jack. Wiring the jack to the camera's trigger pin while both devices are USB-connected to the same Mac closes a ground loop.
    - If the camera tethers over **Wi-Fi** (via Sony SDK or Imaging Edge Desktop), the jack is safe — no USB closed loop. Phase 2 enables this via `--trigger-mode hw` for IED-backed capture.
- **Valoi 360 Advancer**, manually operated. Mounted on the Scanlight Valoi adapter. User advances the film by hand between frames; no motor.
- **Mac (Apple Silicon)** running macOS. Sony Camera Remote SDK in user's possession.
- **(Optional)** Sony USB-C → 3.5mm trigger cable. Used only with the Wi-Fi-tether path described above.

### Hardware architecture

Three supported configurations.

**Current direction (2026-05-22):** Sony SDK capture works over Wi-Fi using
host-PC auto-download, and the Swift app passes SDK settings through
`triplet-capture` to `sony-capture`. Configuration C (`--trigger-mode manual`)
remains the safest fallback. Configuration B is optional if the Scanlight
3.5mm shutter pulse proves reliable.

**Configuration A — Wi-Fi Sony SDK trigger/download (`sony-capture`, CLI verified)**

```
Mac
 ├── Wi-Fi ◄──► Sony a7CR (PC Remote mode; SDK capture + host-PC download)
 ├── USB-A/C ──► Scanlight v4 LEFT port (CDC serial control)
 └── Local SSD ──► output folder for RAWs and composites

Scanlight v4 RIGHT port ◄── USB-C ─── Wall PSU (5V/2A+)
Valoi 360 ──► seated on Scanlight via official v4 adapter
a7CR ◄── dummy battery ◄── wall power
```

The camera is not USB-tethered to the Mac in this configuration, so the
3.5mm ground-loop concern does not apply. The current verified command is
`sony-capture --out PATH --ip-address 10.0.0.247 --mac-address 10:32:2C:26:1A:3F --user USER --password PW`.

**Configuration B — Wi-Fi tether, hardware-fired shutter (`--trigger-mode hw`)**

```
Mac
 ├── USB-A/C ──► Scanlight v4 LEFT port (CDC serial control)
 ├── Wi-Fi ◄──► Sony a7CR (Imaging Edge Desktop, save to inbox folder)
 └── Local SSD ──► IED inbox + roll output folder

Scanlight v4 RIGHT port ◄── USB-C ─── Wall PSU (5V/2A+)
Scanlight v4 3.5mm jack ──► a7CR USB-C trigger pins (Sony USB-C→3.5mm cable)
Valoi 360 ──► seated on Scanlight via official v4 adapter
a7CR ◄── dummy battery ◄── wall power
```

In Configuration B the Mac issues a `PKT_H2D_SHUTTER_PULSE` to the Scanlight, the Scanlight fires the camera's shutter directly via the 3.5mm jack, and the resulting RAW arrives in IED's inbox over Wi-Fi. The orchestrator (`triplet-capture --trigger-mode hw --ied-inbox PATH`) watches the inbox and moves the file into the roll's canonical naming. The ground loop concern from Configuration A does not apply because there is no USB tether between camera and Mac to close the loop through.

**Configuration C — Wi-Fi tether, manual IED shutter (`--trigger-mode manual`)**

```
Mac
 ├── USB-A/C ──► Scanlight v4 LEFT port (CDC serial control)
 ├── Wi-Fi ◄──► Sony a7CR (Imaging Edge Desktop, save to inbox folder)
 └── Local SSD ──► IED inbox + roll output folder

Scanlight v4 RIGHT port ◄── USB-C ─── Wall PSU (5V/2A+)
Valoi 360 ──► seated on Scanlight via official v4 adapter
a7CR ◄── dummy battery ◄── wall power
```

In Configuration C the orchestrator sets R/G/B on the Scanlight and waits
for the next new RAW in IED's inbox. The operator manually fires the
camera in IED once per lit channel. This is the safest fallback if the
3.5mm shutter pulse path is not working.

---

## Critical do's and don'ts

**Do:**
- Use the Swift app's SDK trigger mode for Sony SDK Wi-Fi capture, or Imaging Edge Desktop + watched inbox as the fallback. Manual IED trigger is the safest fallback; the Scanlight 3.5mm pulse path is optional after hardware verification.
- Treat Sony SDK host-PC auto-download as the working SDK path. RemoteTransfer card contents listing is not reliable on the tested a7CR session.
- Use the Scanlight CDC serial binary protocol documented in `automation/bsl_control_interface.md` of the scanlight repo.
- Use 16-bit linear ProPhoto-RGB TIFFs as the composite output format.
- Treat the 200ms unsolicited telemetry packets (LED_TEMP, VBUS) from Scanlight as a continuous background stream. Read them in a separate thread/task and dispatch by header byte. Do not assume the next byte you read is a response to your last command.
- Capture in RAW lossless-compressed, ISO 100, manual exposure, manual focus, fixed WB, f/5.6–f/8, IBIS off, electronic shutter or EFCS, all automatic corrections disabled.

**Don't:**
- Do not use the Scanlight 3.5mm shutter jack **while the camera is USB-tethered to the same Mac.** That configuration closes a ground loop. The jack is fine in the Wi-Fi-tether path (see Configuration B above).
- Do not power the Scanlight from a Mac USB port.
- Do not set the `save_preset` flag in `PKT_H2D_SET_COLOR` unless the user explicitly asks. The Scanlight's NVM has a finite write cycle life; that flag writes the current RGB values to NVM as power-on defaults.
- Do not attempt to operate the white channel and any RGB channel at the same time. The firmware blocks it; respect the constraint cleanly in code.
- Do not invert each color channel before compositing. The pipeline is: demosaic each RAW → take the matching color channel from each → composite into one RGB image → save as TIFF → inversion happens downstream in FilmLab/NLP.
- Do not assume the Mac live view stream from the Sony SDK contains focus peaking overlays. Body-side focus peaking shows on the camera's own LCD/EVF only. Do not try to reimplement it; the user focuses by looking at the camera.
- Do not capture-to-card and import later. Always tether-capture and download direct to disk.

---

## Phase 1 — Plumbing

**Goal:** From a terminal, the operator can independently (a) switch Scanlight channels and (b) capture+download a RAW from the a7CR.

### Deliverable 1A — `scanlightctl`

Python CLI, pyserial. Implements the documented Scanlight v4 protocol.

- Packet format: `0xFE | header | length | data...`
- Required host-to-device packets:
  - `PKT_H2D_SET_COLOR` (header 0, 6 bytes): R, G, B, W, IR, save_preset. IR ignored by v4 firmware. **save_preset defaults to 0 in this CLI and is only set when an explicit flag is passed.**
  - `PKT_H2D_GET_FW_VERSION` (header 2, 0 bytes).
  - `PKT_H2D_GET_DEFAULT_RGB` (header 1, 0 bytes).
- Background reader thread/task continuously reads incoming packets and dispatches by header. Handles unsolicited `PKT_D2H_LED_TEMP` (header 1) and `PKT_D2H_VBUS` (header 2) every ~200ms. Responses to requests are matched by header.
- Commands to implement:
  - `scanlightctl on r [--level N]` (set R to N, G/B/W to 0)
  - `scanlightctl on g [--level N]`, `on b [--level N]`, `on w [--level N]`
  - `scanlightctl off` (all channels to 0)
  - `scanlightctl set --r N --g N --b N` (combined RGB)
  - `scanlightctl status` (read fw version, default RGB, last known temp/vbus)
  - `scanlightctl set-default --r N --g N --b N` (this is the one command that sets `save_preset=1`)
- Channel value range: 0–255 (one byte each, per protocol).
- Auto-discover the Scanlight serial device (vendor/product ID lookup or by description match). Allow `--port /dev/cu.usbmodem*` override.
- Useful library logic: a thin `Scanlight` class that owns the serial port and exposes `set_color(r,g,b,w,save=False)`, `get_fw_version()`, `last_temp_c`, `last_vbus_mv` properties. The CLI is a thin wrapper. The class will be reused in later phases.

### Deliverable 1B — `sony-capture`

CLI that drives a single capture and download via the Sony Camera Remote SDK.

- Sony's SDK is C++ on macOS. Options for the wrapper:
  1. Write a small C++ executable that takes args and emits status, build once with the SDK's CMake examples as a starting point. Cleanest.
  2. Build a Python binding (pybind11) around the bits we need. More work now, pays off later.
- Recommend **option 1 for Phase 1** to keep blast radius small. Phase 3's native Swift app links the SDK directly anyway.
- Interface: `sony-capture --out /path/to/file.ARW [--timeout 30] [--ip-address IP --user USER --password PW]`
- Behavior:
  - Enumerate devices, connect to first/only camera.
  - Set SDK save info to a scratch host-PC download folder.
  - Trigger shutter release.
  - Wait for the image-ready notification from the SDK.
  - Download the RAW to `--out`. Atomically (write to `.tmp` then rename) so partial files never look complete.
  - Exit 0 on success, nonzero on any failure with a clear stderr message.
- The camera must be set to PC Remote mode beforehand. The user handles camera settings; the CLI does not change capture settings in Phase 1. On the tested a7CR, host-PC auto-download works; RemoteTransfer contents listing returns `36101`.

### Deliverable 1C — Optical dry run protocol

Document a procedure (a short markdown file in the repo, `docs/optical_dry_run.md`) that the user follows before any Phase 1 software is run:

1. Mount Valoi over Scanlight with the v4 adapter.
2. Insert a known good film strip.
3. Set lens to f/5.6 or f/8.
4. Focus on grain using camera EVF/LCD with body-side focus peaking.
5. Manually fire R, G, B exposures via Scanlight web app + manual shutter press.
6. Confirm via in-camera review: no channel clips highlights, no channel crushes shadows of the film base, no vignetting, no Newton rings.
7. If all good, proceed with software.

This is paper-only but it's a deliverable because the engineer building Phase 2/3 needs to know this happened.

### Phase 1 exit criteria

```
scanlightctl on r
sony-capture --out Frame001_R.ARW
scanlightctl on g
sony-capture --out Frame001_G.ARW
scanlightctl on b
sony-capture --out Frame001_B.ARW
scanlightctl off
```

…produces three RAW files on disk, each ~60–80 MB. Both binaries are deterministic and have clean exit codes.

---

## Phase 2 — Capture pipeline (no live preview)

**Goal:** Scan a full roll. Push button per frame, advance Valoi by hand, push again. End of roll: a directory of three RAWs per frame, plus a directory of 16-bit TIFF composites.

### Deliverable 2A — `triplet-capture` orchestrator

Python CLI / Flask backend. The Swift app is now the primary operator
surface and starts this backend; the Flask page remains a fallback.

- State:
  - `roll_name` (e.g., `Roll001`)
  - `frame_number` (default starts at 1)
  - `output_folder` (e.g., `/Volumes/SSD/Scans/Roll001/`)
  - Per-channel exposure overrides and per-channel brightness levels (start with sensible defaults; the user calibrates by film stock).
- Single primary action: **Capture Triplet**
  1. Set Scanlight R only.
  2. Sleep `settle_ms` (default 50ms; configurable).
  3. Capture/wait according to trigger mode:
     - `manual`: wait for the operator-fired IED RAW to appear in `--ied-inbox`.
     - `hw`: pulse the Scanlight shutter output, then wait for the IED RAW.
     - `sdk`: call `sony-capture` (deferred/reference path).
  4. Set Scanlight G only. Sleep. Capture/wait as `_G.ARW`.
  5. Set Scanlight B only. Sleep. Capture/wait as `_B.ARW`.
  6. Set Scanlight off (or back to W for next-frame framing).
  7. Verify all three files exist and are within plausible size range (40–200MB each; upper bound widened from 120MB after a real 121MB uncompressed a7CR capture). If not, surface a clear error and DO NOT advance the frame counter.
  8. On success, advance `frame_number`.
- Secondary actions:
  - Retake current frame (overwrite `_R/_G/_B`).
  - Set frame number manually.
  - Set roll name (resets frame to 1 unless overridden).
  - Set per-channel level/exposure overrides.
- All operations logged to `{output_folder}/scan_log.jsonl`: timestamp, action, channel, file path, file size, success/fail, error message.
- The orchestrator imports the `Scanlight` class from Phase 1 (not shelling out to `scanlightctl` for every command — that's slow and ignores telemetry continuity). It shells out to `sony-capture` in SDK mode.

### Deliverable 2B — `rgb-composite`

Offline compositor. Python, uses `rawpy` (libraw) and `numpy` and `tifffile`.

- Input: paths to three RAW files (`_R.ARW`, `_G.ARW`, `_B.ARW`) for one frame.
- Steps:
  1. Open each RAW with `rawpy`. Use a **linear** demosaic pipeline: `gamma=(1,1)`, `no_auto_bright=True`, `output_bps=16`, `use_camera_wb=False`, `user_wb=(1,1,1,1)`, `output_color=rawpy.ColorSpace.ProPhoto`.
  2. Result per file: a 16-bit linear ProPhoto-RGB array.
  3. Construct the composite: take channel 0 (R) from the R-lit demosaic, channel 1 (G) from the G-lit demosaic, channel 2 (B) from the B-lit demosaic.
  4. Save as 16-bit TIFF with `tifffile`. Embed ProPhoto-RGB ICC profile if possible; otherwise write a sidecar describing the color space.
  5. **No inversion.** The output is a positive-numbers representation of a negative image. Inversion is downstream.
- Interface: `rgb-composite --r FRAME_R.ARW --g FRAME_G.ARW --b FRAME_B.ARW --out FRAME.tif`
- Sanity check on input: confirm the three RAWs have identical dimensions. If not, abort with a clear error (this means film moved between captures).

### Deliverable 2C — `batch-composite`

Walks an `output_folder` of a scanned roll, groups files by frame number, runs `rgb-composite` per frame, writes outputs into a sibling `composites/` folder. Skips frames missing any channel, logs them. Parallelizable (one frame per worker; libraw isn't thread-safe within a frame but across frames is fine).

### Phase 2 exit criteria

Scan a 36-exposure roll. End state on disk:

```
/Volumes/SSD/Scans/Roll001/
    Roll001_Frame001_R.ARW
    Roll001_Frame001_G.ARW
    Roll001_Frame001_B.ARW
    Roll001_Frame002_R.ARW
    ...
    Roll001_Frame036_B.ARW
    scan_log.jsonl
    composites/
        Roll001_Frame001.tif
        Roll001_Frame002.tif
        ...
        Roll001_Frame036.tif
```

The 36 TIFFs are 16-bit linear ProPhoto-RGB. Imported into FilmLab or NLP, inversion produces usable positives.

---

## Phase 3 — Swift app + future live preview

**Current goal:** Use the native macOS app as the unified operator surface for
the per-roll workflow (Set up → Calibrate → Scan → Develop) while wrapping the
tested Python pipeline. Live inverted preview remains a future hardware-gated
goal.

### Architecture

Swift / SwiftUI macOS app. Apple Silicon target.

- The Sony Camera Remote SDK bridge is still CLI-based. The app launches `triplet-capture`, which routes SDK mode through the verified `sony-capture` Wi-Fi path.
- The Scanlight serial protocol is re-implemented in Swift (it's tiny — six host-to-device packets, simple framing).
- Navigation is a **workflow sidebar** (replacing the earlier 5-tab layout in the 2026-06 UX redesign): the four ordered per-roll steps above two utilities, with a persistent **readiness strip** (roll/output identity + live light/camera/backend status) across every pane.
  - **Set up:** roll/output/trigger/composite settings, Sony camera connection, and capture output. Default trigger is `manual`.
  - **Calibrate:** guided rig check, exposure calibration, FFC capture, and numeric checks.
  - **Scan:** starts/stops the Python orchestrator, captures/retakes frames, and polls composite status.
  - **Develop:** in-app positive inversion / render-look surface.
  - *Utilities* — **Diagnostics:** manual Scanlight serial control + live-view tools; **Film stocks:** saved per-stock RGB exposure recipes. (The old **Session** dashboard was removed — the readiness strip now shows that state live and always-on.)
- Future live-preview mode:
  - Scanlight set to white channel only (e.g., W=200, RGB=0).
  - A camera live-view bridge delivers frames for a Core Image invert/tone pipeline.
  - Focus remains body-side; Mac preview is for framing, alignment, and dust spotting.
- Same on-disk output as Phase 2, same naming, same compositor. The Swift app should keep wrapping the Python pipeline rather than rewriting the compositor in Swift.

### Phase 3 exit criteria

User opens the app, selects a roll name and output folder, enters framing mode, sees a live inverted color preview at ~15–30fps as they advance the film, hits Capture Triplet per frame, and at end of roll runs the compositor from the same app.

---

## Scanlight v4 protocol — concrete reference

USB CDC serial. macOS path will be something like `/dev/cu.usbmodem*`.

**Packet framing (both directions):**

```
Byte 0: 0xFE  (start byte, always)
Byte 1: header
Byte 2: data length N
Bytes 3..3+N: data
```

**Host-to-device packet headers we use:**

| Header | Name | Data len | Data bytes |
|--------|------|----------|------------|
| 0 | `PKT_H2D_SET_COLOR` | 6 | R, G, B, W, IR, save_preset (each 0–255; IR ignored on v4) |
| 1 | `PKT_H2D_GET_DEFAULT_RGB` | 0 | — |
| 2 | `PKT_H2D_GET_FW_VERSION` | 0 | — |
| 3 | `PKT_H2D_SHUTTER_PULSE` | 1 | pulse length in 10ms units — **DO NOT USE** |

**Device-to-host packet headers we receive:**

| Header | Name | Data len | Data | Frequency |
|--------|------|----------|------|-----------|
| 1 | `PKT_D2H_LED_TEMP` | 4 | LED temp in millidegrees C (int32 **BE** two's complement) | every 200ms |
| 2 | `PKT_D2H_VBUS` | 4 | VBUS voltage in millivolts (int32 **BE** two's complement) | every 200ms |
| 3 | `PKT_D2H_FW_VERSION` | 4 | fw version ID + hw version ID (u32 **BE**: low 16 bits = FW, high 16 bits = HW) | response only |
| 4 | `PKT_D2H_DEFAULT_RGB` | 3 | R, G, B (each 0–255) | response only |

> **Endianness:** All multi-byte D2H integers are **big-endian**. The published `bsl_control_interface.md` is silent on byte order, but the firmware (`automation/firmware_bsl1/protocol.c::protocol_send_packet_int32`) emits MSB-first and the canonical web app reads via `DataView.getUint32(0)` (BE default). An earlier draft of this doc said "LE" — that was wrong. Cross-checked against the firmware source and the official Vue web app on 2026-05-18.

**Firmware constraints (enforced by the device):**
- White channel and any RGB channel cannot be on at the same time. If you send a `SET_COLOR` with both, the firmware will adjust. Don't fight it; in our code, mode-switch cleanly.
- If power supply is insufficient, the device will reduce max output when multiple channels are on simultaneously. Single-channel max output is always available.
- `save_preset=1` writes the RGB values to NVM. Don't do this in normal operation — only when the user explicitly asks to update the on-power defaults.

---

## Sony Camera Remote SDK — concrete reference

- Version 1.10+ supports the Sony a7CR (confirmed via DPReview Sept 14, 2023 article).
- SDK exposes: capture/shutter release, all camera settings, live view monitoring, AF tracking sensitivity, focus position, focal length info, save-destination control.
- Live view monitoring delivers JPEG frames over USB. Resolution and fps are camera-specific; verify the actual a7CR numbers from SDK calls at runtime, not from documentation guessing.
- The SDK is C++ on macOS. Apple Silicon: confirm the dylib provided by Sony is arm64 or universal — if it's only x86_64, the Phase 1 binary needs to run under Rosetta or be x86_64 and Phase 3 needs the same. Document this in the repo when discovered.
- Standard SDK usage pattern (Sony's sample code is the reference):
  1. `Init()` the SDK.
  2. Enumerate devices, get a camera object.
  3. Connect to the camera.
  4. Set save destination to host.
  5. Trigger shutter via `SendCommand(Release)` or equivalent.
  6. Wait for `FileAdded` event.
  7. Download the file with `GetLiveViewImage` … no wait, that's live view. The capture-download path uses a different callback / file URL retrieval method. Refer to the SDK's `SampleApp` for the canonical flow.
- For live view: there's an explicit `StartLiveView` / `StopLiveView` pair. The capture flow expects live view to be stopped first on many bodies; in Phase 3 we stop, capture, restart.

---

## File and directory conventions

```
{output_root}/{roll_name}/
    {roll_name}_Frame{NNN}_R.ARW
    {roll_name}_Frame{NNN}_G.ARW
    {roll_name}_Frame{NNN}_B.ARW
    scan_log.jsonl
    composites/
        {roll_name}_Frame{NNN}.tif
```

- `roll_name` is operator-supplied, ASCII, no spaces.
- `NNN` is zero-padded 3-digit frame number.
- TIFF is 16-bit linear ProPhoto-RGB.

---

## Repository structure (suggested)

```
filmscanner/
    README.md
    PROJECT.md                 (this file)
    docs/
        optical_dry_run.md
        calibration_notes.md
    phase1/
        scanlightctl/          (Python)
            scanlight/__init__.py
            cli.py
            pyproject.toml
        sony-capture/          (C++ + CMake)
            CMakeLists.txt
            src/main.cpp
            third_party/sony_sdk/  (gitignored; user-installed)
    phase2/
        triplet-capture/       (Python)
        rgb-composite/         (Python)
        batch-composite/       (Python)
    phase3/
        FilmScanner.xcodeproj/
        FilmScanner/           (Swift sources)
        SonyBridge/            (Obj-C++ wrapping the SDK)
    tests/
```

---

## Verification checklist per phase

**Phase 1:**
- [ ] `scanlightctl on r` makes the device red.
- [ ] `scanlightctl status` returns fw version and recent telemetry.
- [ ] Background telemetry reader does not block command/response.
- [ ] `sony-capture --out test.ARW` produces a ~60–80MB file.
- [ ] All three channels can be cycled with no USB hiccups across 30 sequential captures.

**Phase 2:**
- [ ] Triplet capture produces three correctly named files.
- [ ] Frame counter advances only on success.
- [ ] Retake overwrites the current frame's files.
- [ ] Compositor produces a 16-bit TIFF with the expected dimensions and a plausible histogram (orange-mask negative, no clipping).
- [ ] Batch compositor handles 36 frames and logs any missing-channel skips.
- [ ] Imported into FilmLab/NLP, the TIFFs invert to plausible positives.

**Phase 3:**
- [ ] Framing mode shows a live inverted preview.
- [ ] Capture mode runs the triplet and returns to framing.
- [ ] Mode transitions don't drop the camera USB connection.
- [ ] Output on disk is byte-identical to Phase 2 outputs.

---

## Open items to confirm by physical test

1. End-to-end capture+download time per RAW on the user's hardware. Determines triplet duration. If above ~3s, consider capture-to-card-then-batch-download optimization in a later iteration.
2. Sony SDK live view resolution and fps for the a7CR specifically. Determines Mac preview quality.
3. Sony SDK dylib architecture on macOS (arm64 / x86_64 / universal) — affects how Phase 1 and Phase 3 are built.
4. Per-film-stock calibration: brightness levels for R, G, B that produce non-clipping captures of the film base. Empirical, per stock. Persist as presets.

---

## What's explicitly out of scope (for now)

- White-light scanning. Not the goal. The Scanlight v4 white channel is used only for live-preview illumination during framing.
- IR / dust mapping. Possible future addition; not in any current phase.
- RGB + W combined captures (luminance channel). Skipped intentionally.
- Lightroom Classic library/export automation. Treated as downstream; we hand off TIFFs.
- Motorized film advance. The Valoi 360 is operated by hand. The orchestrator never assumes it can move the film.
- Negative inversion math. Done downstream in FilmLab or NLP.
