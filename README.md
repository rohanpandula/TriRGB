# TriRGB

**Scan, combine, and invert RGB negatives.**

TriRGB is a Mac-driven workflow for **trichromatic narrowband-RGB film scanning**:
it photographs a color negative as three sequential exposures — one each under
narrowband **red (665 nm)**, **green (525 nm)**, and **blue (455 nm)** light —
then composites the matching channel from each into a single 16-bit linear
ProPhoto-RGB TIFF (or Linear DNG) — **color-managed** so it opens with correct
color in any profile-aware app — that inverts cleanly in
[Negative Lab Pro](https://www.negativelabpro.com/) or
[FilmLab](https://www.filmlabapp.com/).

This is the same principle used by dedicated film scanners (Fuji Frontier,
Nikon Coolscan): treat the camera sensor as a **densitometer** and measure the
film one narrow band at a time, instead of fighting the orange mask under white
light.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform: macOS](https://img.shields.io/badge/platform-macOS%2013%2B-blue)
![Swift 5.9](https://img.shields.io/badge/Swift-5.9-orange)
![Python 3.12](https://img.shields.io/badge/Python-3.12%2B-blue)

> **Status:** active development, built around specific hardware (see below).
> The capture/compositing/calibration logic and the operator GUI are
> implemented and tested hardware-free; full end-to-end validation on the
> physical rig is ongoing. Treat it as a reference implementation and a
> starting point, not a turnkey product.

---

## How it works

1. **Light one band at a time.** A narrowband RGB backlight illuminates the
   negative with pure red, then green, then blue. You advance the film by hand;
   one button captures the **triplet** (three RAW frames).
2. **Extract the matching channel.** From the red-lit frame we keep only the
   sensor's red channel, green from the green-lit frame, blue from the blue-lit
   frame. This sidesteps the Bayer-sensor crosstalk that limits single-shot RGB
   capture.
3. **Composite.** The three channels are merged into one 16-bit **linear**
   RGB image. Because each channel is exposed to place the **film base** just
   below clipping, the orange mask is neutralized in the exposure (physical)
   domain — the film rebate comes out near-neutral grey.
4. **Invert.** With a neutral base and linear data, inversion is close to a
   straight linear flip — far less per-channel wrangling than white-light
   scans, and the red channel no longer clips first and bottlenecks dynamic
   range.

### Why bother (vs. white-light camera scanning)

- **More latitude:** every channel is exposed to the right independently, so
  you keep highlight and shadow headroom in all three.
- **Cleaner separation:** narrowband light + per-channel extraction mimics how
  pro RGB scanners work.
- **Simpler inversion:** a neutralized base means minimal, predictable color
  correction downstream.

---

## Hardware

TriRGB is built and verified against this rig (other Sony bodies / RGB lights
should adapt with modest changes):

| Component | Used here |
|---|---|
| Camera | **Sony a7CR** (61 MP), via Sony Camera Remote SDK (Wi-Fi PC Remote) |
| Lens | Sony FE 90mm Macro (Pentax 120mm as fallback) |
| Light | **[Scanlight v4 by jackw01](https://github.com/jackw01/scanlight)** — narrowband 665/525/455 nm RGB + 5000K 95-CRI white, USB-CDC serial control |
| Film transport | Valoi 360 Advancer (manual, one frame at a time) |
| Host | Mac (Apple Silicon), macOS 13+ |

Power and tether wiring matter (ground-loop avoidance, light power isolation) —
see the [project brief](PROJECT.md) and [operator guide](docs/OPERATING.md).

---

## Repository structure

The system is three cooperating layers:

```
phase1/   Hardware control (plug-in-day primitives)
  sony-capture/     C++ CLI over the Sony Camera Remote SDK (capture + live view)
  scanlightctl/     Python serial control for the Scanlight LEDs

phase2/   Imaging pipeline (Python, hardware-free + testable)
  c41-core/         Versioned data contracts shared across the pipeline
  rgb-composite/    Channel extraction, flat-field correction, rebate detection,
                    linear inversion, color-managed output (embedded ICC
                    profiles), and numeric QA checks
  triplet-capture/  Capture orchestrator + Flask backend; per-roll exposure &
                    flat-field calibration
  batch-composite/  Batch compositing of a whole roll

phase3/   Operator GUI
  FilmScanner/      SwiftUI macOS app — a workflow sidebar (Set up → Calibrate →
                    Scan → Develop) over the Python backend, with an always-on
                    readiness strip and the guided calibration wizard

docs/     PROJECT brief, operator guide, automation contract, AX-ID reference
scripts/  Calibration capture/inspection and diagnostics helpers
```

The Swift app talks to the Python orchestrator over HTTP; the orchestrator
shells out to the C++ `sony-capture` tool and the Scanlight serial driver.

---

## Requirements

- **macOS 13+** on Apple Silicon
- **Swift 5.9+** (Xcode toolchain) for the app
- **Python 3.12+** for the imaging pipeline
- **Sony Camera Remote SDK** (v1.10+) to build `phase1/sony-capture` — obtain it
  from Sony; it is not redistributed here
- A C++ toolchain + CMake for `phase1/sony-capture`
- Python imaging deps: `numpy`, `rawpy`, `opencv-python` (per-package
  `pyproject.toml`)

---

## Build & test

```bash
# --- Imaging pipeline (Python) ---
python -m venv .venv && source .venv/bin/activate
pip install -e phase2/c41-core -e phase2/rgb-composite \
            -e phase2/triplet-capture -e phase2/batch-composite
pytest                       # full suite (hardware-free)

# --- Operator app (Swift) ---
cd phase3/FilmScanner
swift build
swift test                   # unit + UI-surface tests, no hardware needed
swift run scanlight-app      # launch the macOS app

# --- Sony capture CLI (C++, requires the Sony SDK) ---
cd phase1/sony-capture
cmake -B build && cmake --build build
```

The Python and Swift suites are designed to run **without any hardware**
attached (capture and demosaic seams are injectable), so you can develop and
validate the full pipeline on any Mac.

---

## Usage

The short version of a session:

1. **Verify the rig** — app checks the camera connection and Scanlight serial
   link.
2. **Calibrate once per roll** — the guided wizard solves a per-channel LED
   level + shutter so each channel's film base lands near 85% of range without
   clipping (this is what neutralizes the orange mask), then captures a
   flat-field reference.
3. **Scan** — advance the film by hand; one button captures each R/G/B triplet.
   In manual (Imaging Edge) mode the app shows a per-channel prompt — "Fire R
   now", then G, then B — so there is no guesswork about which exposure to
   trigger. In Sony-SDK mode the capture path holds **one camera session open
   for the whole roll** (no per-frame reconnect). RAWs auto-download and are
   named by roll/frame.
4. **Composite** — the backend merges each triplet into a 16-bit linear
   TIFF/DNG. Each output carries an embedded color profile (ICC on the TIFF,
   DNG colorimetry tags) so it reads with correct color — not assumed sRGB —
   in Lightroom, NLP, Preview, and the in-app Develop preview.
5. **Invert** — import to Negative Lab Pro / FilmLab and apply a linear
   inversion.

The complete, field-tested operator how-to (hardware setup, exact commands,
gotchas) lives in **[docs/OPERATING.md](docs/OPERATING.md)**. AI agents and CI
runners should start at **[docs/automation.md](docs/automation.md)**, which
documents every automatable CLI, the app's accessibility-ID schema, and the
JSON/exit-code contracts.

---

## Calibration, in brief

Per-roll RGB calibration is the heart of quality here, and it mirrors how
practitioners do it by hand: expose each channel so the **film base reads equal
across R/G/B** (neutral grey), just short of clipping. TriRGB automates that —
it drives each channel's rebate toward a common target, prefers LED PWM as the
fine trim with shutter as the coarse step, and guards against both demosaiced
and source-Bayer clipping. See
[docs/calibration_notes.md](docs/calibration_notes.md).

---

## Credits & prior art

TriRGB stands on a lot of community work:

- **[jackw01](https://github.com/jackw01/scanlight)** — the Scanlight hardware
  and the original narrowband-RGB scanning write-up that inspired this project.
- **seklerek** and the **[r/AnalogCommunity](https://www.reddit.com/r/AnalogCommunity/)**
  trichromatic-scanning discussions — the manual per-roll process this
  automates (the toneLight / toneCarrier work).
- **Barbara Flückiger et al., ETH Zürich (2018)** — research on film-material /
  scanner interaction.
- **[Negative Lab Pro](https://www.negativelabpro.com/)** and
  **[FilmLab](https://www.filmlabapp.com/)** — the inversion tools this pipeline
  feeds.

---

## License

[MIT](LICENSE) © 2026 Rohan Pandula

The Sony Camera Remote SDK is proprietary to Sony and is **not** included in or
covered by this license; obtain it directly from Sony.
