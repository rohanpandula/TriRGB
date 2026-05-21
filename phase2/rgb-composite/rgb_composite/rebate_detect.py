"""Automatic and manual film-rebate detector for narrowband C-41 scans.

Overview
--------
This module locates the film rebate (the clear, unexposed orange-base strip
at the frame edge / inter-frame gap) in a demosaiced scan and returns a
validated Phase 08 ``BaseRegionDescriptor``.

Two entry points are provided:

``detect_rebate(img)``
    Fully automatic.  Runs a vectorised score map over the green channel
    (index 1) of the input image:

      score = brightness_norm - cv_norm - detail_norm

    where all three components are normalised to [0, 1] with equal implicit
    weight (no tunable constants).  The window that maximises this score is
    the rebate.  Detection is performed on a ~1500 px long-edge downsampled
    proxy for speed; the winning bbox is then mapped back to full-resolution
    coordinates.  ``np.argmax`` on the flat score map provides a
    deterministic top-left tie-break (row-major = topmost row first,
    leftmost column within that row).

``manual_picker(img, row, col)``
    Operator spatial picker.  Centres a neighbourhood window on the given
    (row, col) click coordinate, clamps it to image bounds, and measures
    ``base_rgb`` + ``uniformity_cv`` on that region.  Returns a
    ``BaseRegionDescriptor`` with ``source="manual"``.  Only raises on
    structurally invalid input (click out of image bounds); patchy or
    suboptimal regions are silently returned with their ``uniformity_cv``
    value so the downstream wizard can surface the quality numerically.

Color blindness / zero color discrimination
-------------------------------------------
The operator is colorblind.  **All detection and scoring logic operates
exclusively on the green channel (index 1) for brightness.**  ``base_rgb``
is a purely *numeric readout* of per-channel means measured in the found
region — it is never used as a branch condition or color-based decision.
This invariant is enforced throughout the module (NFR-11).

Algorithm details (``detect_rebate``)
--------------------------------------
1. Downsample the green channel to at most ``long_edge_target`` pixels on
   the long edge using ``cv2.resize(INTER_AREA)`` (area-averaging,
   deterministic).  The ``else`` branch (``scale == 1.0``) explicitly sets
   ``new_h, new_w = H, W`` to avoid a ``NameError`` on small/fixture inputs.
2. Window size ``win = max(3, int(min(new_h, new_w) * window_frac))``.
   Exposed as ``window_frac`` kwarg (default 0.05) for Phase 14 / M2 tuning
   without a code change.
3. Windowed brightness map: ``scipy.ndimage.uniform_filter`` (O(N/pixel)).
4. Windowed CV via E[x²] − E[x]²  in float64 to avoid FP cancellation at
   uint16 magnitudes.  ``np.maximum(..., 0.0)`` clamp prevents ``sqrt`` of
   tiny negatives.
5. Edge/detail density via ``cv2.Laplacian``.  Input is clipped to uint16
   because ``cv2.Laplacian(float32, CV_64F)`` raises on OpenCV 4.13 macOS.
6. Normalise each component to [0, 1] (guard max ≥ 1e-9) and combine.
7. ``np.argmax`` flat → ``np.unravel_index`` → (best_y_s, best_x_s).
8. Map bbox to full-resolution space and clamp to image bounds.
   Actual per-axis ratios ``ry = H/new_h`` and ``rx = W/new_w`` are used
   (not the scalar ``scale``) to avoid sub-pixel drift from ``int()``
   flooring.  The winning pixel is the CENTER of its scored window, so
   ``(best_y_s + 0.5) * ry`` gives the full-res window center; the bbox
   is built centered on that coordinate.
9. Measure ``base_rgb`` on the *full-resolution* region (not the proxy).
10. Measure ``uniformity_cv`` via ``_measure_uniformity_cv`` (reuses
    ``_box_filter_2d`` from ``ffc.py``, NFR-14).
11. Return ``BaseRegionDescriptor(source="auto")``.

References
----------
- Phase 08 ``BaseRegionDescriptor`` contract (``c41_core.contracts``).
- ``_box_filter_2d`` in ``ffc.py`` (NFR-14 reuse — byte-identical to
  ``uniformity_score()`` in ``scripts/inspect-calibration.py``).
- Phase 09 RESEARCH.md, Patterns 1–5 + Pitfalls 1–6.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import uniform_filter

from c41_core import BaseRegionDescriptor

from .ffc import _box_filter_2d


# ---------------------------------------------------------------------------
# Module constant
# ---------------------------------------------------------------------------

#: Target long-edge size for the scoring proxy (pixels).
_LONG_EDGE_TARGET: int = 1500


# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------

def _measure_uniformity_cv(green_region_2d: np.ndarray) -> float:
    """Compute uniformity CV (%) for a single-channel region, clamped to 100.

    Mirrors ``uniformity_score()`` in ``scripts/inspect-calibration.py``
    exactly, reusing ``_box_filter_2d`` from ``ffc.py`` (NFR-14):

      kernel = max(3, int(min(h, w) * 0.05))
      smoothed = _box_filter_2d(region.astype(float32), kernel)
      cv = 100 * std(smoothed) / max(mean(smoothed), 1.0)
      return min(cv, 100.0)   # BaseRegionDescriptor rejects values > 100

    Args:
        green_region_2d: HxW single-channel 2-D array (any numeric dtype).

    Returns:
        Uniformity CV in [0.0, 100.0].
    """
    h, w = green_region_2d.shape
    kernel = max(3, int(min(h, w) * 0.05))
    smoothed = _box_filter_2d(green_region_2d.astype(np.float32), kernel)
    mean_val = float(np.mean(smoothed))
    raw_cv = 100.0 * float(np.std(smoothed)) / max(mean_val, 1.0)
    return min(raw_cv, 100.0)  # contract: BaseRegionDescriptor rejects > 100.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_rebate(
    img: np.ndarray,
    long_edge_target: int = _LONG_EDGE_TARGET,
    window_frac: float = 0.05,
) -> BaseRegionDescriptor:
    """Automatically locate the film rebate in a demosaiced scan.

    Uses a vectorised score map on the green channel (index 1) to find the
    window that is simultaneously brightest, most uniform (low CV), and
    least detailed (low Laplacian response).  All three components receive
    equal weight — no magic constants.

    The scoring operates on a downsampled proxy (at most ``long_edge_target``
    pixels on the long edge) for performance; the winning bbox is then mapped
    back to full-resolution space and the base measurements are taken on the
    original image.

    Detection is fully color-blind: no branch condition or algorithm step
    depends on the relationship between R, G, and B channels.  ``base_rgb``
    is a numeric readout only (NFR-11).

    Args:
        img:               HxWx3 uint16 array in linear ProPhoto-RGB (no WB),
                           as produced by ``demosaic_linear``.
                           Channel index: R=0, G=1, B=2.
        long_edge_target:  Downsample the green channel so the long edge is
                           at most this many pixels before scoring.
                           Default 1500.  A 60 MP a7CR frame (9600×6376) is
                           reduced to ~996×1500.
        window_frac:       Score-window size as a fraction of the shorter
                           downsampled dimension.  Default 0.05 (= 5 %).
                           Exposed as a kwarg so Phase 14 / M2 tuning can
                           adjust without a code change.

    Returns:
        ``BaseRegionDescriptor`` with ``source="auto"`` and
        ``schema_version=1``.
    """
    # Guard: fail closed on wrong dtype or shape.  detect_rebate expects the
    # output of demosaic_linear, which is always uint16 HxWx3.  A float32 or
    # wrong-shape input would silently produce meaningless values after the
    # uint16 clip in step 5 (Laplacian) or the normalisation step.
    if img.ndim != 3 or img.shape[2] < 3:
        raise ValueError(
            f"img must be HxWx3 (or deeper), got shape {img.shape!r}"
        )
    if img.dtype != np.uint16:
        raise ValueError(
            f"img must be uint16 (demosaic_linear output), got dtype {img.dtype!r}"
        )

    H, W, _ = img.shape  # HxWx3 uint16

    # Guard: degenerate all-zero green channel cannot produce a meaningful score.
    # Fail closed (consistent with project "fail closed" pattern) so callers get
    # a diagnosable error rather than a silently wrong descriptor at (0, 0).
    if int(img[:, :, 1].max()) == 0:
        raise ValueError(
            "green channel is all-zero; cannot locate rebate"
        )

    # 1. Downsample green channel for efficient scoring.
    #    The else branch MUST set new_h, new_w (not just green_s) — fixture
    #    images are 128x192 so scale == 1.0 on every fixture test; omitting
    #    new_h, new_w here causes NameError at step 2.
    scale = min(1.0, long_edge_target / max(H, W))
    if scale < 1.0:
        new_h, new_w = int(H * scale), int(W * scale)
        green_s = cv2.resize(
            img[:, :, 1], (new_w, new_h), interpolation=cv2.INTER_AREA
        ).astype(np.float32)
    else:
        new_h, new_w = H, W
        green_s = img[:, :, 1].astype(np.float32)

    # 2. Score-window size (configurable via window_frac kwarg).
    win = max(3, int(min(new_h, new_w) * window_frac))

    # 3. Windowed brightness map (O(N) via uniform_filter).
    mean_map = uniform_filter(green_s, size=win, mode="nearest").astype(np.float64)

    # 4. Windowed CV via E[x^2] - E[x]^2 in float64 to avoid FP cancellation
    #    at uint16 magnitudes.  np.maximum clamp prevents sqrt of tiny negatives.
    mean_sq = uniform_filter(
        (green_s ** 2).astype(np.float64), size=win, mode="nearest"
    )
    var_map = np.maximum(mean_sq - mean_map ** 2, 0.0)
    cv_map = (np.sqrt(var_map) / np.maximum(mean_map, 1.0)).astype(np.float32)

    # 5. Windowed edge/detail via Laplacian -- MUST pass uint16 input.
    #    cv2.Laplacian(float32, CV_64F) raises on OpenCV 4.13 macOS (Pitfall 1).
    lap = cv2.Laplacian(green_s.clip(0, 65535).astype(np.uint16), cv2.CV_64F)
    detail_map = uniform_filter(
        np.abs(lap).astype(np.float32), size=win, mode="nearest"
    )

    # 6. Normalise all three to [0, 1] (equal weights -- no tunable constants).
    #    Guard denominator with max(..., 1e-9) consistent with lines below.
    b = (mean_map.astype(np.float32) / max(float(mean_map.max()), 1e-9)).clip(0, 1)
    c = (cv_map / max(float(cv_map.max()), 1e-9)).clip(0, 1)
    d = (detail_map / max(float(detail_map.max()), 1e-9)).clip(0, 1)
    score = b - c - d  # higher = brighter, more uniform, less detail

    # 7. Top-left tiebreak: argmax first occurrence in C/row-major order =
    #    topmost row first, then leftmost column -- deterministic, no extra code.
    best_y_s, best_x_s = np.unravel_index(int(np.argmax(score)), score.shape)

    # 8. Map bbox to full-resolution space + clamp to image bounds.
    #
    #    Two precision fixes vs the naive ``coord / scale`` approach:
    #
    #    (a) CENTER correction: uniform_filter scores each pixel as the CENTER
    #        of its win×win window, so the winning pixel (best_y_s, best_x_s)
    #        is the CENTER of the best window in downsampled space, NOT its
    #        top-left.  Add 0.5 to convert pixel-index to pixel-center before
    #        scaling, then build a centered bbox in full-res space.
    #
    #    (b) PER-AXIS ratio: new_h = int(H*scale) and new_w = int(W*scale)
    #        both floor, so H/new_h and W/new_w can differ slightly from
    #        1/scale on non-divisible sizes (up to ~1–2 px drift).  Use the
    #        actual integer dimensions to compute per-axis ratios.
    ry = H / float(new_h)   # actual full-res pixels per downsampled pixel, y
    rx = W / float(new_w)   # actual full-res pixels per downsampled pixel, x
    cy = (best_y_s + 0.5) * ry   # full-res center of the winning window
    cx = (best_x_s + 0.5) * rx
    win_h = max(1, int(round(win * ry)))
    win_w = max(1, int(round(win * rx)))
    x = max(0, min(W - win_w, int(round(cx - win_w / 2.0))))
    y = max(0, min(H - win_h, int(round(cy - win_h / 2.0))))
    w = win_w
    h = win_h

    # 9. Measure base_rgb on the FULL-RESOLUTION region (not the proxy).
    #    Pitfall 2: INTER_AREA changes means near region boundaries.
    region = img[y : y + h, x : x + w, :]
    base_rgb = (
        float(region[:, :, 0].mean()),
        float(region[:, :, 1].mean()),
        float(region[:, :, 2].mean()),
    )

    # 10. Uniformity CV on full-res region (reuses _box_filter_2d, NFR-14).
    uniformity_cv = _measure_uniformity_cv(region[:, :, 1])

    # 11. Construct and return the descriptor.
    return BaseRegionDescriptor(
        x=x,
        y=y,
        w=w,
        h=h,
        base_rgb=base_rgb,
        uniformity_cv=uniformity_cv,
        source="auto",
    )


def manual_picker(
    img: np.ndarray,
    row: int,
    col: int,
    neighborhood_frac: float = 0.05,
) -> BaseRegionDescriptor:
    """Return a descriptor for the rebate region at a user-clicked coordinate.

    Centres a square neighbourhood of size
    ``max(3, int(min(H, W) * neighborhood_frac))`` on ``(row, col)``,
    clamps it to image bounds (so an edge/corner click returns a smaller but
    valid region), and measures ``base_rgb`` + ``uniformity_cv`` there.

    Raises ``ValueError`` **only** on structurally invalid input (click
    coordinates outside the image).  A patchy, high-CV region is still
    returned — the descriptor carries ``uniformity_cv`` so the downstream
    wizard can surface the quality numerically without applying any
    color-based judgment (NFR-11).

    Args:
        img:                HxWx3 uint16 demosaiced array (same space as
                            ``detect_rebate``).
        row:                Row coordinate of the user click (0-indexed).
        col:                Column coordinate of the user click (0-indexed).
        neighborhood_frac:  Neighbourhood size as a fraction of the shorter
                            image dimension.  Default 0.05 (= 5 %).
                            Configurable per Phase 09 CONTEXT Area 2.

    Returns:
        ``BaseRegionDescriptor`` with ``source="manual"`` and
        ``schema_version=1``.

    Raises:
        ValueError: if ``(row, col)`` is outside ``[0, H) x [0, W)``.
    """
    H, W, _ = img.shape

    # 0. Validate click coordinates against ORIGINAL (pre-truncation) values.
    #    Negative fractional inputs like -0.1 truncate to 0 under int() and
    #    would be silently accepted as in-bounds; checking BEFORE truncation
    #    catches them.  The error message shows the raw input for diagnosability.
    #    Phase 14 SwiftUI bridge may pass mouse-click coordinates as floats;
    #    an in-bounds float (e.g. 5.7) passes this check and is then safely
    #    truncated below.
    if row < 0 or row >= H or col < 0 or col >= W:
        raise ValueError(
            f"click ({row}, {col}) is out of bounds for {H}x{W} image"
        )

    # 1. Coerce float coords to int (truncation-toward-zero) now that we know
    #    the original values are in [0, H) × [0, W).
    row = int(row)
    col = int(col)

    # 2. Compute neighbourhood size; clamp window to image bounds.
    nh = nw = max(3, int(min(H, W) * neighborhood_frac))
    half_h, half_w = nh // 2, nw // 2
    y0 = max(0, row - half_h)
    y1 = min(H, row + half_h + 1)
    x0 = max(0, col - half_w)
    x1 = min(W, col + half_w + 1)
    x, y, w, h = x0, y0, x1 - x0, y1 - y0
    # For an in-bounds (row, col) this guarantees w >= 1 and h >= 1.

    # 3. Measure base_rgb on the actual (possibly edge-shrunk) region.
    #    No color decision -- purely a numeric readout.
    region = img[y : y + h, x : x + w, :]
    base_rgb = (
        float(region[:, :, 0].mean()),
        float(region[:, :, 1].mean()),
        float(region[:, :, 2].mean()),
    )

    # 4. Uniformity CV on the green channel (reuses _box_filter_2d, NFR-14).
    uniformity_cv = _measure_uniformity_cv(region[:, :, 1])

    # 5. Construct and return the descriptor.
    return BaseRegionDescriptor(
        x=x,
        y=y,
        w=w,
        h=h,
        base_rgb=base_rgb,
        uniformity_cv=uniformity_cv,
        source="manual",
    )
