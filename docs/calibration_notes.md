# Calibration notes

Per-stock R/G/B Scanlight levels and shutter speeds. Fill this in during the optical dry run for each film stock you scan, then load the values into `triplet-capture` before a roll.

A stock's "working levels" are the ones where, viewed on the camera's RGB histogram:
- The film base (clear/dense unexposed area, fully developed) sits near 255 but doesn't clip.
- The deepest image shadows (densest part of the negative) sit comfortably above 0.
- The mid-tones of the histogram occupy the right half of 0–255.

Levels drift over time as the LEDs age and as you vary lens/aperture/distance. Re-check whenever results look off.

## Working levels by stock

| Stock | Lens | Aperture | Shutter | Level R | Level G | Level B | Verified date | Notes |
|---|---|---|---|---|---|---|---|---|
| _example_ Portra 400 | Sony FE 90mm Macro | f/8 | 1/4 s | 180 | 200 | 240 | YYYY-MM-DD | blue boost compensates for orange mask |
| _example_ Ektar 100 | Sony FE 90mm Macro | f/8 | 1/4 s | 160 | 200 | 235 | YYYY-MM-DD | |
| _example_ HP5+ | Sony FE 90mm Macro | f/8 | 1/4 s | 220 | 220 | 220 | YYYY-MM-DD | B&W — use equal levels |
| Portra 800 | | | | | | | | |
| Tri-X | | | | | | | | |
| Velvia 50 | | | | | | | | (slide — different workflow) |

## Per-channel sanity expectations

- **R level (665 nm)**: usually the highest of the three for color negatives because the orange mask is densest in the blue-absorbing layer, but for color-neg film the *blue* channel typically needs the highest level — the orange mask absorbs blue light.
- **G level (525 nm)**: usually middle.
- **B level (455 nm)**: usually the highest for color negatives (orange mask absorbs blue), the lowest for B&W (no mask).

If you find yourself wanting to push a single channel above 240, drop shutter speed instead — keeps highlights from clipping.

## Sensor temperature note

LED color shifts as junction temperature rises. Keep an eye on `scanlightctl status` during a long session. If LED temp exceeds ~60 °C on the red channel during continuous use, pause between frames or drop the level by ~10%.

## When recalibration is needed

- New film stock you haven't scanned before
- Switched lens or magnification
- Changed the column / Valoi stack height
- Scans suddenly look color-shifted or one channel clips/crushes
- LED ages noticeably (after ~hundreds of hours of use)
