# Optical dry run — pre-software verification

> Phase 1 / Deliverable 1C. Do this once *before* any Phase 1 software is run, and re-do it any time the optical stack changes (new lens, new film holder, new mounting orientation).
> The Phase 2/3 engineer needs to know this happened — if any step fails, software won't save the scan.

## Why this exists

Software inversion can hide a lot of optical problems. By the time you're looking at a TIFF in FilmLab, you've already paid for three exposures and a compositing run, and you can't tell from the positive whether the negative was actually exposed cleanly. This protocol catches the failures that color math can't recover from:

- Highlight clipping in any channel
- Shadow crush in the film base
- Vignetting outside the film gate
- Newton rings from the holder
- Misaligned film plane

Pass this once per setup, then trust the software.

## Setup

1. **Power.** Scanlight RIGHT port → wall PSU (≥5V/2A). Mac → Scanlight LEFT port. Camera → Mac via USB-C with the dummy battery in.
2. **Mount.** Valoi 360 seated on the Scanlight v4 adapter. Camera on column or copy stand directly above, lens parallel to film plane.
3. **Lens.** Sony FE 90mm Macro mounted, set to **f/5.6 or f/8** (manual aperture). IBIS off. Manual focus.
4. **Camera body.**
   - PASM dial: M (manual exposure)
   - ISO 100
   - Drive: single shot
   - White balance: any fixed value (Daylight is fine — WB is irrelevant for narrowband captures, but fix it so it doesn't drift)
   - File format: **RAW (lossless compressed)**
   - Save destination: Host (will be set programmatically in Phase 1; verify the menu option exists)
   - All in-camera corrections disabled: lens compensation off, noise reduction off, DRO/HDR off, creative styles neutral
   - Electronic shutter or EFCS
   - Body-side focus peaking on (it shows only on the EVF/LCD; the Mac live view doesn't carry it)
5. **Film.** Insert a **known good** strip — something you've scanned successfully before, or a fresh Portra/Ektar/HP5 strip with normal density. Don't dry-run with a problem strip.

## Procedure

For each step, the failure mode is in italics. **If a step fails, stop and fix the optics before running software.**

### 1. Frame and focus

1. Open the [Scanlight web app](https://jackw01.github.io/scanlight) in Chrome/Edge and connect.
2. Set white channel to ~200 (RGB to 0). *If the white channel doesn't come on at all, the device is on a Mac USB port instead of wall power — fix that first.*
3. Frame the film in the camera. The film gate should occupy ~95% of the frame, with a thin black border on all sides — this is your evidence that the film plane is roughly parallel and you're not cropping into the image area.
4. Manually focus on **grain**, not on the image. Use peaking + 10× magnification on the EVF/LCD. Grain is the highest spatial frequency in a film negative; if grain is in focus, the image is in focus.
   - *If you can't find sharp grain anywhere in the frame, the film plane is bowed or the holder is tilted — re-seat the strip.*

### 2. Per-channel exposure check

For each of R, G, B (in that order — magenta light is the most fatiguing, save it for last):

1. In the Scanlight web app, turn off white, turn the channel to **128** (mid-range; we want headroom to go both ways).
2. Set camera shutter speed to a guess: 1/4s is a reasonable starting point for f/8, ISO 100, mid-range LED.
3. Take a single exposure with the camera shutter button (not the Scanlight jack — we don't use that, ever).
4. Review the exposure on the camera's LCD using the **histogram + RGB highlight blink** display.

   The relevant channel's histogram should look roughly like this for a frame containing both unexposed film base and the densest image area:

   ```
   highlights ───┐                    ┌─── film base shows up here, near right
                 │   ████             │
                 │ ██████████         │
                 │██████████████      │
                 │█████████████████   │
                 │██████████████████ ◄┘
   ──────────────┴────────────────────┴────── 0                                255
        crush                              clip
   ```

   **Pass criteria:**
   - No highlight blink anywhere in the frame (film base is *not* clipped at 255).
   - Shadow side of the histogram does not stack against 0 (the densest part of the image is *not* crushed).
   - Histogram occupies at least the right half of the range — if it's bunched into the bottom third, the LED level is too low or the shutter speed too short; bump one and retry.

   **Failure modes:**
   - *Blown highlights in this channel only:* drop the LED level for this channel. Don't drop shutter speed unless all three channels are too hot.
   - *Crushed shadows in this channel:* raise the LED level OR lengthen shutter speed. Note that raising the level on red specifically can heat the LED — watch `scanlightctl status` once Phase 1A is in use.
   - *Histogram looks identical to a different channel's:* the LED isn't switching — verify in the web app, check the USB cable.

5. Note the working level for this channel in `docs/calibration_notes.md` against the film stock. These are starting points for Phase 2's per-stock presets.

### 3. Vignetting check

With the green channel on at the level you settled on in step 2:

1. Take an exposure of a clear (unexposed, fully developed — i.e. fully dense orange-mask) frame from the leader of the same roll.
2. Open in any RAW viewer. Look for:
   - **Symmetric darkening at the four corners** → lens vignetting. Mitigate by stopping down to f/8 (already there), or accept it as a flat-field correction problem to solve in a later phase. Note the magnitude; >0.5 EV is a problem, <0.25 EV is fine.
   - **Asymmetric darkening (one edge much darker)** → the lens optical axis isn't centered over the film gate. Re-align the column.
   - **Hot center, dark periphery** → light source not diffused evenly. Check the Scanlight is fully seated under the Valoi.

### 3.5. Per-channel narrowband vignette inspection

> **Why this matters:** Under narrowband-RGB illumination, each color channel has its own vignette profile — not just brightness falloff, but a *wavelength-dependent tint shift* toward the corners. White-light scanning hides this; narrowband doesn't. Multiple users on the Negative Lab Pro forum (Big Scanlight thread, Jan–Mar 2026) report this as the single biggest workflow surprise. The Valoi 360 may or may not sit at the right distance from the diffuser to keep it clean; this step is where you find out.

Do this **with no film in the holder** (blank-light captures):

1. Run `scripts/capture-calibration.sh` to capture an R-lit, G-lit, B-lit triplet automatically. (Or, with the web app: switch to R only, capture; G only, capture; B only, capture. Same outcome, more clicks.)
2. Run the new quantitative inspector:

   ```bash
   python3 scripts/inspect-calibration.py ~/.scanlight/calibration/$(date +%Y-%m-%d)
   ```

   It reports, per channel:
   - **Corner-to-center falloff (%)** — should be under ~30% for a usable scan. Above that, FFC starts amplifying noise more than it lifts signal. Negative falloff (corners brighter than center) flags a *hotspot* — the scanlight isn't centered under the carrier; re-seat before recapturing.
   - **Saturation rate** — if any channel shows >1% clipped pixels, the scanlight level is too high for the cal frame; lower it via `--r-level/--g-level/--b-level` in `capture-calibration.sh` and re-cal.
   - **Implied tint at corners** — if R/G/B falloff differ by more than ~3 percentage points, you have wavelength-dependent vignetting and FFC is required (not optional). The classifier flags this at the same 3% boundary it uses for the "OK with FFC" decision.
3. Manually open each ARW and eyeball the corners. You're looking for:
   - **Cleanly symmetric falloff that's similar across R/G/B** → ideal. FFC will correct it cleanly.
   - **One channel patchy or with a tinted gradient that FFC can't flatten** → not enough air between the holder and the diffuser. Lift the Valoi 5–15 mm and re-cal.
   - **Bright central plateau ringed by sharp darkening** → light source isn't fully under the holder, or there's a partial obstruction. Re-seat the Scanlight.

**Decision points:**

- Falloff under 15% per channel + tint drift under 3% → FFC is a nice-to-have. You can scan without it.
- Falloff 15–30% per channel + tint drift 3–10% → FFC is required. Use the cal dir with `--ffc-calibration` on every composite.
- Falloff over 30% or tint drift over 10% → setup problem. Don't scan; fix the optics first (holder distance, scanlight position, lens centering).

Re-run this whenever you change holder, working distance, lens, or move the scanlight.

### 4. Newton rings check

Tilt the camera/film assembly under the room lights (or with white on at low level) and look for **rainbow interference patterns** between the film and any glass/anti-newton surface.

- *If you see rings:* film is in contact with a glass surface (Valoi shouldn't have one in the optical path, but check the adapter). Reseat. If unfixable, switch to a different holder.
- AN-glass holders should not show rings if seated correctly. Bare-glass holders without AN treatment will show rings on humid days — not appropriate for this workflow.

### 5. Repeatability sanity check

1. With levels set per step 2, take three captures of the **same frame** under the same channel, advancing nothing in between.
2. Open all three in a RAW viewer and switch between them quickly. They should be indistinguishable.
   - *If they aren't*: vibration. Tighten the column, use a delay, or switch to electronic shutter.

## Sign-off

When all five steps pass, log to `docs/calibration_notes.md` with:

- Date
- Film stock
- Lens + aperture
- Working levels: R=___, G=___, B=___
- Working shutter speed
- Any deviations from the standard setup

This is the data Phase 2's `triplet-capture` orchestrator will load as defaults.

## What this protocol does *not* check

- Color quality of the final positive — that requires running the full Phase 2 pipeline and importing into FilmLab/NLP. By design, this dry run only validates the optical capture is clean. If captures are clean here and the positive still looks wrong, the bug is in `rgb-composite` or downstream, not in the optics.
- Long-term drift — LED output can shift with temperature over a long session. Phase 1A's `scanlightctl status` exposes LED temp; spot-check it during a real scan. Don't try to characterize drift here.
- Focus across the frame — a macro lens at f/8 with a flat film holder should hold focus corner-to-corner. If grain is sharp in the center, trust it. If your scans show edge softness later, this protocol won't catch it; re-shoot a test target then.
