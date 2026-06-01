# Film Scanner

A Mac-driven workflow for scanning film negatives as three sequential
narrowband-RGB exposures, composited into a 16-bit linear ProPhoto-RGB
TIFF (or Linear DNG) for inversion in **Negative Lab Pro** or FilmLab.

**Canonical brief:** [`PROJECT.md`](../PROJECT.md) — read first.
**Resume from HW arrival:** [`HANDOFF.md`](../HANDOFF.md).
**Optical pre-flight:** [`docs/optical_dry_run.md`](optical_dry_run.md).
**Automation reference:** [`docs/automation.md`](automation.md) — for AI agents and CI runners.

---

## Automating this project

For AI agents (Claude, Codex) and CI runners that need to drive the
system without human intervention, the written contract lives at
[`docs/automation.md`](automation.md). It enumerates every
automatable surface (every CLI, the SwiftUI app's AX-ID schema, the
Python harness pattern), documents the JSON contract / exit codes /
failure modes for each, and ships a GUI ↔ CLI mapping table covering
every case in `AccessibilityIDs.swift`. The flat AX-ID list is at
[`docs/ax-id-reference.md`](ax-id-reference.md); a consistency
check keeps it in sync with the Swift source.

The rest of this file is the operator how-to. AI agents should start
at `docs/automation.md`; humans scanning film should keep reading.

---

## Hardware

| | |
|---|---|
| Camera | Sony a7CR with NP-FZ100 dummy battery |
| Lens | Sony FE 90mm Macro (primary), Pentax 120mm (fallback) |
| Light | Scanlight v4 (jackw01) — narrowband 665/525/455 nm + 5000K white |
| Film holder | Valoi 360 Advancer, manual |
| Host | Apple Silicon Mac |

See PROJECT.md §"Hardware architecture" for cabling. **Important:**
Scanlight LEFT port is data (Mac), RIGHT port is wall PSU — never
powered from Mac USB.

---

## Structure

```
.
├── PROJECT.md                  ← spec / brief (read first)
├── HANDOFF.md                  ← state of every deliverable + plug-in-day playbook
├── README.md                   ← this file
├── docs/
│   ├── optical_dry_run.md      ← pre-software optical checklist
│   └── calibration_notes.md    ← per-stock R/G/B level table (fill in)
├── scripts/
│   ├── diagnose.py             ← first-thing-on-plug-in HW check
│   ├── smoketest.sh            ← Phase 1 exit-criteria, one command
│   └── capture-calibration.sh  ← capture FFC blank-light triplet
├── phase1/
│   ├── scanlightctl/           ← Python CLI + driver for Scanlight v4 (USB-CDC)
│   └── sony-capture/           ← C++ Sony SDK capture CLI (Wi-Fi verified on a7CR)
├── phase2/
│   ├── triplet-capture/        ← Per-frame R/G/B orchestrator (Flask UI, disposable)
│   ├── rgb-composite/          ← 3 ARWs → 16-bit linear ProPhoto-RGB TIFF or Linear DNG, with optional per-channel FFC
│   └── batch-composite/        ← Walk a roll dir, composite every frame
└── phase3/
    └── FilmScanner/            ← Swift package/app — native control hub
```

---

## End-to-end how-to

The complete flow from cold start to final positive JPEG. Run each step
once per scanning session unless noted.

### 1. Plug in the hardware

```
Mac ──USB-A→ Scanlight LEFT port  (data / control)
Wall PSU ──USB-C→ Scanlight RIGHT port  (power)
Mac ⇄ Sony a7CR  (Sony SDK Wi-Fi, or Imaging Edge Desktop fallback)
```

The dummy battery into the a7CR's battery slot keeps it from sleeping
during long sessions.

### 2. Verify the rig (~30 seconds)

```bash
python3 scripts/diagnose.py --skip-camera
```

Checks serial enumeration, Scanlight handshake, and VBUS voltage. The
Sony SDK camera check can be skipped for IED/manual workflows. **Do not
scan until the Scanlight side is green.** If something fails, the error
tells you which assumption broke.

### 3. Optical dry run (paper, one-time per setup)

```bash
open docs/optical_dry_run.md
```

Confirm focus, framing, level, and working distance. If you swap the
lens or holder later, redo this — it's a thirty-second check that saves
hours.

### 4. Capture calibration / FFC (once per session)

**Why:** Under narrowband-RGB illumination, each color channel has its
own vignette profile — red, green, and blue fall off differently at the
corners. Without correction this shows up as red flaring in the NLP
conversion (see [NLP forum thread on Big Scanlight workflow][nlpforum]).

[nlpforum]: https://forums.negativelabpro.com/t/nlp-workflow-using-combined-rgb-light-source/

**Current app direction:** use the Swift app's Calibrate tab for exposure
level calibration, flat-field capture, and numeric checks. The wizard now
owns the backend lifecycle: it validates Settings, starts `triplet-capture`,
claims the Scanlight port as `.calibrating`, runs exposure/FFC/checks, then
stops the backend and reconnects the Light panel.

**CLI path:** `scripts/capture-calibration.sh` uses `sony-capture`. The
Swift app's SDK trigger mode now passes the same Wi-Fi host-PC path through
`triplet-capture`.

Remove film from the holder before any flat-field capture:

```bash
scripts/capture-calibration.sh
# → ~/.scanlight/calibration/<YYYY-MM-DD>/{R,G,B}.ARW
```

Re-run whenever you change holder, working distance, lens, or move the
scanlight. **Use the same lens/focus/distance as the actual scan.**

### 5. Phase 1 smoke test (one frame, ~30 seconds)

```bash
scripts/smoketest.sh /Volumes/SSD/smoke
```

Cycles R/G/B with the shutter, verifies three RAWs landed in the
expected size band. Pure sanity check — if this fails, don't waste time
scanning film.

### 6. Scan a roll

Load film into the Valoi 360. Film advance is manual; there is no
motorized advance in this project. Launch the orchestrator with whichever
trigger mode matches your physical setup.

**Configuration C — IED manual trigger (current default / safest fallback):**

Imaging Edge Desktop holds the camera connection and auto-saves each RAW
to a watched folder. The app controls only the Scanlight: it lights R,
waits for you to manually fire the camera in IED, picks up the new RAW,
then repeats for G and B. Use this when the 3.5mm shutter pulse is not
working, or when you want to avoid both the Sony SDK and Scanlight
shutter wiring.

```bash
# Start Imaging Edge Desktop first; point its save folder at --ied-inbox.
triplet-capture \
    --roll-name Roll001 \
    --output-folder /Volumes/SSD/Scans \
    --trigger-mode manual \
    --ied-inbox /Volumes/SSD/_ied_inbox \
    --capture-timeout-s 30 \
    --stream-composite \
    --ffc-calibration ~/.scanlight/calibration/$(date +%Y-%m-%d) \
    --camera-model "Sony ILCE-7CR"
```

Per frame: click Capture Frame in the Swift app or Flask UI, then watch
the Scanlight color and manually fire one IED capture for R, one for G,
and one for B. After the triplet succeeds, manually advance the Valoi to
the next frame.

**Configuration B — IED + Scanlight 3.5mm trigger (optional):**

The tether app still holds the camera connection and auto-saves each RAW
to a watched folder, but the Scanlight's 3.5mm jack fires the shutter
for each channel. Use this only after confirming the pulse cable works
reliably.

```bash
# Start the tether app first; point its save folder at --ied-inbox below.
# (Sony: Imaging Edge Desktop. Fuji: Lightroom Classic → File → Tether Capture.)
triplet-capture \
    --roll-name Roll001 \
    --output-folder /Volumes/SSD/Scans \
    --trigger-mode hw \
    --ied-inbox /Volumes/SSD/_ied_inbox \
    --shutter-pulse-ms 100 \
    --capture-timeout-s 30 \
    --stream-composite \
    --ffc-calibration ~/.scanlight/calibration/$(date +%Y-%m-%d) \
    --camera-model "Sony ILCE-7CR"   # or "FUJIFILM GFX100 II"
```

**Configuration A — Sony SDK Wi-Fi trigger/download (Swift app SDK mode):**

```bash
phase1/sony-capture/build/sony-capture \
    --out /tmp/test.ARW \
    --ip-address 10.0.0.247 \
    --mac-address 10:32:2C:26:1A:3F \
    --user USER \
    --password PW
```

> Status on 2026-05-22: the SDK authenticates over Wi-Fi, fires stills,
> and downloads ARW via host-PC auto-download. The RemoteTransfer card
> contents-list path authenticates and fires, but `GetRemoteTransferContentsInfoList`
> returns `36101` on this body/session. Keep IED manual mode as the safest
> operational fallback until the Swift app is wired to the working CLI path.

Either IED-backed mode opens a web UI and is also started by the Swift
app. Per frame:
1. Click "Capture frame NNN" — the Scanlight cycles R, G, B; three RAWs
   land in `/Volumes/SSD/Scans/Roll001/`.
2. Manually advance the Valoi to the next frame.
3. Repeat.

File naming follows `{roll_name}_Frame{NNN}_{R|G|B}.{ARW,RAF}`.

### 7. Composite the roll

**If you passed `--stream-composite` in step 6, the roll is already composited** —
each triplet was composited in the background as you shot it, so by the time
you finished the last frame the `composites/` directory was full. Skip to
step 8. (Background composites run up to 4 at a time; a failed frame is
logged but never aborts capture, and you can always re-run `batch-composite`
to fill gaps.)

**Otherwise, composite the whole roll in one batch pass:**

```bash
batch-composite /Volumes/SSD/Scans/Roll001 \
    --ffc-calibration ~/.scanlight/calibration/$(date +%Y-%m-%d) \
    --format both
```

Walks the roll directory, finds every (R, G, B) triplet, and writes a
composite per frame to `Roll001/composites/`.

Key flags (shared by `batch-composite` and the `--stream-composite` path):
- `--ffc-calibration <dir>` — apply per-channel Flat Field Correction
  using the calibration triplet from step 4. Strongly recommended.
- `--format <tiff|dng|both>` (batch) / `--composite-format` (streaming) —
  16-bit linear ProPhoto-RGB as TIFF (legacy), Linear DNG (Lightroom/Capture
  One treat it as RAW — parametric Develop module works), or both.
- `--camera-model "<model>"` — sets the DNG `UniqueCameraModel` tag so LR
  offers the matching camera profile. `"Sony ILCE-7CR"` or
  `"FUJIFILM GFX100 II"`.
- `--workers N` (batch) / `--composite-workers N` (streaming) — parallel
  decode. Default 4 to keep peak memory bounded; raise on a 32 GB+ Mac.

### 8. Import to Lightroom

Drag the `composites/` directory into LR. Use **Linear DNG** if you
generated it — you get the full Develop module before NLP runs.

**Hands-off option:** point Lightroom Classic's Auto Import (File → Auto
Import → Auto Import Settings) at the `composites/` directory. Combined
with `--stream-composite` in step 6, composites appear in your LR catalog
within seconds of each frame being captured — by the time the roll is shot,
every frame is already imported and waiting for NLP. Zero manual import.

Per the NLP forum, before running the convert:
- **Camera Profile** → "Linear" or "Adobe Standard" (not Camera-anything).
- **White Balance** → leave alone. Don't try to neutralize the orange
  mask manually; NLP needs to see it to invert correctly.
- **Tone Curve** → linear.

### 9. Convert with Negative Lab Pro

Select the imported frames → File → Plug-in Extras → Negative Lab Pro →
Convert Negatives. Per-stock settings (Portra 400 vs Ektar 100 vs CineStill,
etc.) belong in `docs/calibration_notes.md` as you tune them.

### 10. Export deliverables

Export sRGB JPEGs for sharing. Keep the DNG (or TIFF) + LR develop edits
as the archival master.

---

## Gotchas from the field

- **Holder-to-light distance matters.** Imacon-style holders that sit
  right against the scanlight produce visible WB shift in the center
  (yellow-cast snow in winslow's Jan 2026 forum post). Tone Carrier with
  a small air gap produces clean scans. The Valoi 360 lands somewhere
  between — **test on plug-in day** with `capture-calibration.sh` and
  inspect the cal frames before scanning real film. If a single channel
  is patchy or shows a tinted gradient that FFC can't flatten, you need
  more air between the holder and the diffuser.
- **Re-running calibration is cheap; re-scanning a roll is not.** When
  in doubt, re-shoot calibration.
- **Don't enable camera WB.** The pipeline locks `user_wb=(1,1,1,1)` for
  good reason — auto WB on a negative scan biases NLP's conversion. The
  test `test_demosaic_kwargs_match_project_md` prevents an accidental
  flip back.
- **FFC in LR is a fallback only.** If you do FFC in Lightroom instead
  of in `rgb-composite`, set the cal frame's color profile to
  **Monochrome** before generating the correction — otherwise LR's FFC
  applies color correction on top of light correction and overshoots.
  Doing FFC in `rgb-composite` skips this trap.

---

## Status

All software shipped and tested without hardware. FFC + Linear DNG
support added 2026-05-18 in anticipation of plug-in day. Plug-in
verification, live-preview app (Phase 3), and per-stock calibration are
HW-gated — see HANDOFF.md.

---

## Tests

```bash
# Python
(cd phase1/scanlightctl    && pytest)
(cd phase2/rgb-composite   && PYTHONPATH=. pytest)
(cd phase2/batch-composite && PYTHONPATH=. pytest)
(cd phase2/triplet-capture && PYTHONPATH=.:../../phase1/scanlightctl pytest)

# Swift
(cd phase3/FilmScanner && swift test)

# sony-capture C++ — SDK loads; no shutter fired
phase1/sony-capture/build/sony-capture --list
```

`rgb-composite` ships unit tests for:
- channel-from-correct-input selection
- FFC math (compute_ffc_map, apply_ffc_to_channel) — uniform input,
  vignette correction, error cases
- Linear DNG roundtrip (DNGVersion tag, LinearRaw photometric,
  ColorMatrix1 precision, AsShotNeutral)
- end-to-end `composite_triplet` with FFC and `--format` flags

A real-ARW integration test runs against a Sony ARW fixture
(env var `RGB_COMPOSITE_TEST_ARW`) and skips cleanly when no fixture is
available.
