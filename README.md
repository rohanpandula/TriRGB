# Film Scanner

A Mac-driven workflow for scanning film negatives as three sequential
narrowband-RGB exposures, composited into a 16-bit linear ProPhoto-RGB
TIFF (or Linear DNG) for inversion in **Negative Lab Pro** or FilmLab.

**Canonical brief:** [`PROJECT.md`](PROJECT.md) — read first.
**Resume from HW arrival:** [`HANDOFF.md`](HANDOFF.md).
**Optical pre-flight:** [`docs/optical_dry_run.md`](docs/optical_dry_run.md).

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
│   └── sony-capture/           ← C++ CLI for single-shot tether capture (Sony SDK)
├── phase2/
│   ├── triplet-capture/        ← Per-frame R/G/B orchestrator (Flask UI, disposable)
│   ├── rgb-composite/          ← 3 ARWs → 16-bit linear ProPhoto-RGB TIFF or Linear DNG, with optional per-channel FFC
│   └── batch-composite/        ← Walk a roll dir, composite every frame
└── phase3/
    └── FilmScanner/            ← Swift package — Scanlight protocol port (scaffold)
```

---

## End-to-end how-to

The complete flow from cold start to final positive JPEG. Run each step
once per scanning session unless noted.

### 1. Plug in the hardware

```
Mac ──USB-A→ Scanlight LEFT port  (data / control)
Wall PSU ──USB-C→ Scanlight RIGHT port  (power)
Mac ──USB-C→ Sony a7CR  (PC Remote tether)
```

The dummy battery into the a7CR's battery slot keeps it from sleeping
during long sessions.

### 2. Verify the rig (~30 seconds)

```bash
python3 scripts/diagnose.py
```

Checks serial enumeration, scanlight handshake, VBUS voltage, and that
the Sony SDK sees the camera. **Do not proceed until everything is
green.** If something fails, the error tells you which assumption broke.

### 3. Optical dry run (paper, one-time per setup)

```bash
open docs/optical_dry_run.md
```

Confirm focus, framing, level, and working distance. If you swap the
lens or holder later, redo this — it's a thirty-second check that saves
hours.

### 4. Capture FFC calibration (once per session)

**Why:** Under narrowband-RGB illumination, each color channel has its
own vignette profile — red, green, and blue fall off differently at the
corners. Without correction this shows up as red flaring in the NLP
conversion (see [NLP forum thread on Big Scanlight workflow][nlpforum]).

[nlpforum]: https://forums.negativelabpro.com/t/nlp-workflow-using-combined-rgb-light-source/

**Run:** Remove film from the holder, then:

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

Load film into the Valoi 360. Launch the orchestrator with whichever
trigger mode matches your physical setup (see PROJECT.md §Hardware
architecture for the two configurations).

**Configuration A — USB tether, SDK trigger (default):**

```bash
triplet-capture --roll-name Roll001 --output-folder /Volumes/SSD/Scans
```

**Configuration B — Wi-Fi tether, Scanlight 3.5mm trigger:**

```bash
# Start Imaging Edge Desktop first; set its save folder to the path
# you'll pass below. Then:
triplet-capture \
    --roll-name Roll001 \
    --output-folder /Volumes/SSD/Scans \
    --trigger-mode hw \
    --ied-inbox /Volumes/SSD/_ied_inbox \
    --shutter-pulse-ms 100 \
    --capture-timeout-s 30
```

Either way, opens a web UI. Per frame:
1. Click "Capture frame NNN" — the scanlight cycles R, G, B; the camera
   fires three times; three ARWs land in
   `/Volumes/SSD/Scans/Roll001/`.
2. Manually advance the Valoi to the next frame.
3. Repeat.

File naming follows
`{roll_name}_Frame{NNN}_{R|G|B}.ARW`.

### 7. Composite the roll

```bash
batch-composite /Volumes/SSD/Scans/Roll001 \
    --ffc-calibration ~/.scanlight/calibration/$(date +%Y-%m-%d) \
    --format both
```

Walks the roll directory, finds every (R, G, B) triplet, and writes a
composite per frame to `Roll001/composites/`.

Key flags:
- `--ffc-calibration <dir>` — apply per-channel Flat Field Correction
  using the calibration triplet from step 4. Strongly recommended.
- `--format <tiff|dng|both>` — output 16-bit linear ProPhoto-RGB as TIFF
  (legacy), Linear DNG (Lightroom/Capture One treat it as RAW —
  parametric Develop module works), or both side-by-side.
- `--workers N` — parallel decode. Default `min(cpu_count, 4)` to keep
  peak memory bounded; raise on a 32 GB+ Mac.

### 8. Import to Lightroom

Drag the `composites/` directory into LR. Use **Linear DNG** if you
generated it — you get the full Develop module before NLP runs.

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

# sony-capture C++ — builds clean, error-path runs
(cd phase1/sony-capture && cmake --build build && build/sony-capture --out /tmp/x.ARW --timeout 2 || true)
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
