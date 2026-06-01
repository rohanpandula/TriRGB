# Calibration notes

Per-stock R/G/B Scanlight levels and shutter speeds. Current SDK calibration can solve these automatically from the selected film-base/rebate ROI: ISO stays at 100, the lens stays at f/8/manual focus, and the app only changes Sony shutter speed plus Scanlight LED level per channel.

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

- **R level (665 nm)**: usually the *lowest* of the three for color negatives (the orange mask passes red freely, so red needs the least drive to reach the base target), and roughly equal to the others for B&W (no mask).
- **G level (525 nm)**: usually middle.
- **B level (455 nm)**: usually the highest for color negatives (orange mask absorbs blue), the lowest for B&W (no mask).

If the solver would need a single channel above about 240, it slows that channel's shutter and trims the final value with LED brightness. In manual/IED fallback modes, set shutter on the camera yourself and only trust the returned LED levels.

## Sensor temperature note

LED color shifts as junction temperature rises. Keep an eye on `scanlightctl status` during a long session. If LED temp exceeds ~60 °C on the red channel during continuous use, pause between frames or drop the level by ~10%.

## When recalibration is needed

- New film stock you haven't scanned before
- Switched lens or magnification
- Changed the column / Valoi stack height
- Scans suddenly look color-shifted or one channel clips/crushes
- LED ages noticeably (after ~hundreds of hours of use)
