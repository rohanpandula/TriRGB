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
    raise NotImplementedError("detect_rebate body will be implemented in Task 2")


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
    raise NotImplementedError("manual_picker body will be implemented in Task 3")
