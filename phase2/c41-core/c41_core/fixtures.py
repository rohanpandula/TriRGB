"""Synthetic C-41 orange-mask fixture builders for the narrowband inversion pipeline.

Why synthetic, not real ARW files
----------------------------------
A real Sony a7CR ARW is ~125 MB per frame; a roll with calibration triples would
add gigabytes to the repository.  The downstream phases (09-13) need the *shapes*
and *orange-mask density ratios* to be realistic, not the actual photon counts.
Parametric builders provide that with zero binary payload.

Real-scan grounding
-------------------
Default values are derived from a real measurement:

  Camera:  Sony a7CR (ILCE-7CR), 14-bit, 9600×6376
  Film:    Kodak Ultramax 400 (C-41 negative), developed
  Path:    demosaic_linear → DEMOSAIC_KWARGS (16-bit linear ProPhoto, no WB,
           gamma=(1,1), no_auto_bright=True, output_bps=16, user_wb=[1,1,1,1])

  Base / Dmin (orange mask), no-WB p99.5 raw values:
    R = 8930   G = 12097   B = 2952

  WHY GREEN READS HIGHEST (PITFALL 4 — this is NOT a fixture bug):
  DEMOSAIC_KWARGS sets user_wb=[1,1,1,1] — no white balance correction.
  The Sony a7CR sensor green channel has ~2× native sensitivity compared to
  red.  In capture space (no WB), green reads highest.  The WB-corrected
  orange-mask transmission signature is R:G:B = 1.00:0.51:0.20 (red dominant),
  but the raw no-WB values are R=8930, G=12097, B=2952 (green highest).
  Phase 09 and Phase 13 tests must assert blue << red AND blue << green,
  NOT red > green.

  Per-channel density spans (log10 base/Dmax), real measurement:
    R ≈ 0.50D   G ≈ 0.60D   B ≈ 1.64D

  Blue attenuation: ~2.3 stops below red (WB-corrected) — confirmed against
  the real frame.  The orange mask crushes blue hardest.

Fixture API
-----------
  make_c41_negative(height, width, seed, ...)  → HxWx3 uint16
      Realistic C-41 orange-mask negative body + rebate strip.
      Channel index: R=0, G=1, B=2 (locked project convention).
      Seeded RNG → same seed produces bit-exact equal output (NFR-11).

  make_rebate_strip(height, width, seed, ...)  → HxWx3 uint16
      Uniform orange-mask base (NO density variation — every row is
      rebate-equivalent).  Used by Phase 09 picker and Phase 13 anomaly tests.

No binary files are committed.  The file at the measurement path
(/Volumes/SSD/CLE roll 1 400/Negatives/...) is NOT in the repository.
"""
from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# Module constants (real-scan-grounded, no-WB raw p99.5 from CONTEXT.md)
# ---------------------------------------------------------------------------

DEFAULT_SEED: int = 42

# No-WB raw p99.5 base values — 16-bit linear ProPhoto, user_wb=[1,1,1,1]
_BASE_R: float = 8930.0
_BASE_G: float = 12097.0  # Green highest: Sony sensor ~2x sensitivity, no WB (see Pitfall 4 above)
_BASE_B: float = 2952.0

# Density spans: B >> R/G — the orange mask gives blue a ~3x larger swing
_DENSITY_R: float = 0.50
_DENSITY_G: float = 0.60
_DENSITY_B: float = 1.64


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def make_c41_negative(
    height: int = 128,
    width: int = 192,
    seed: int = DEFAULT_SEED,
    base_r: float = _BASE_R,
    base_g: float = _BASE_G,
    base_b: float = _BASE_B,
    density_r: float = _DENSITY_R,
    density_g: float = _DENSITY_G,
    density_b: float = _DENSITY_B,
    rebate_height_frac: float = 0.1,
    noise_sigma: float = 50.0,
) -> np.ndarray:
    """Return HxWx3 uint16 synthetic C-41 orange-mask negative.

    The array models:
    - Body: per-channel random values between min_val and base, where
      min_val = base / (10.0 ** density).  Larger density → larger swing
      → darker minimum (orange mask effect: blue has the largest swing).
    - Rebate strip: the top rebate_height_frac rows have a per-channel MEAN
      value at the base level (no density variation — unexposed film base).
      The rebate strip is the BRIGHTEST REGION by mean, not per-pixel:
      individual body pixels may statistically exceed individual rebate pixels
      due to Gaussian noise tails (body sigma=noise_sigma, rebate sigma=
      noise_sigma*0.5; a 3-sigma body excursion can exceed a rebate pixel).
      Downstream code that selects "the brightest pixel" or uses per-pixel
      thresholds to locate the rebate boundary must use mean-based comparisons,
      not per-pixel maximum comparisons.

    Channel index: R=0, G=1, B=2 (locked project convention).

    Seeded with np.random.default_rng(seed) (numpy Generator API — thread-safe,
    no global state, NFR-11 determinism).  Same seed → bit-exact equal output.

    Args:
        height, width:       Array dimensions.  Defaults: 128×192 (matches
                             tests/integration/conftest.py H/W convention).
        seed:                RNG seed.  DEFAULT_SEED=42.
        base_r/g/b:          Per-channel base (Dmin / orange mask) raw value.
                             Defaults are no-WB p99.5 from real a7CR scan.
        density_r/g/b:       Per-channel density span in log10 units.
                             Larger value → larger swing → darker body minimum.
        rebate_height_frac:  Fraction of rows that form the bright rebate strip.
        noise_sigma:         Gaussian noise standard deviation (raw counts).

    Returns:
        np.ndarray: shape (height, width, 3), dtype uint16.
    """
    rng = np.random.default_rng(seed)
    img = np.zeros((height, width, 3), dtype=np.float32)

    for ch_idx, (base, density) in enumerate(
        [(base_r, density_r), (base_g, density_g), (base_b, density_b)]
    ):
        # Denser film = lower transmission = smaller minimum value
        min_val = base / (10.0 ** density)
        # Body: random values in [min_val, base] + Gaussian noise
        body = rng.uniform(min_val, base, size=(height, width)).astype(np.float32)
        noise = rng.normal(0.0, noise_sigma, size=(height, width)).astype(np.float32)
        img[:, :, ch_idx] = np.clip(body + noise, 0.0, 65535.0)

    # Rebate strip: overwrite the top rows with base-level (no density swing)
    rebate_h = max(1, int(height * rebate_height_frac))
    for ch_idx, base in enumerate([base_r, base_g, base_b]):
        rebate_noise = rng.normal(
            0.0, noise_sigma * 0.5, size=(rebate_h, width)
        ).astype(np.float32)
        img[:rebate_h, :, ch_idx] = np.clip(base + rebate_noise, 0.0, 65535.0)

    return np.clip(img, 0.0, 65535.0).astype(np.uint16)


def make_rebate_strip(
    height: int = 128,
    width: int = 192,
    seed: int = DEFAULT_SEED,
    base_r: float = _BASE_R,
    base_g: float = _BASE_G,
    base_b: float = _BASE_B,
    noise_sigma: float = 50.0,
) -> np.ndarray:
    """Return HxWx3 uint16 uniform orange-mask base (no density variation).

    Every row is rebate-equivalent: the base value + small Gaussian noise.
    There is NO density-driven variation between rows — this is the dedicated
    rebate-only fixture for Phase 09 picker tests and Phase 13 anomaly tests.

    Channel index: R=0, G=1, B=2 (locked project convention).

    Args:
        height, width:  Array dimensions.  Defaults: 128×192.
        seed:           RNG seed for determinism (NFR-11).
        base_r/g/b:     Per-channel base value (no-WB p99.5 defaults).
        noise_sigma:    Gaussian noise sigma.

    Returns:
        np.ndarray: shape (height, width, 3), dtype uint16.
    """
    rng = np.random.default_rng(seed)
    img = np.zeros((height, width, 3), dtype=np.float32)

    for ch_idx, base in enumerate([base_r, base_g, base_b]):
        noise = rng.normal(0.0, noise_sigma, size=(height, width)).astype(np.float32)
        img[:, :, ch_idx] = np.clip(base + noise, 0.0, 65535.0)

    return img.astype(np.uint16)
