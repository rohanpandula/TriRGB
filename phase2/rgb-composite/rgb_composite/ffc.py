"""Per-channel Flat Field Correction for narrowband-RGB scanning.

Why this exists
---------------
Under narrowband RGB illumination, **each color channel has its own
vignette profile**. White-light scanning hides this — all wavelengths fall
off together, you correct once with a luminance map, done. Narrowband
doesn't: the 665nm red, 525nm green, and 455nm blue LEDs each have
slightly different spatial intensity, the lens has slightly different
falloff per wavelength, and the diffuser's transmission is wavelength-
dependent too. The result is a *tint shift* toward the corners — red
flaring in the NLP conversion that you can't get rid of by white-balancing.

The NLP forum thread on Big Scanlight scanning (Jan–Mar 2026) is the
canonical write-up. Multiple users hit this on Imacon and similar holders
that put the film close to the source. The fix is FFC, done **per
channel**, not as a single luminance correction.

How this module does it
-----------------------
Calibration: one blank-light triplet (no film, scanlight at scanning
brightness, captured under R then G then B). Saved as:

    <cal_dir>/R.ARW
    <cal_dir>/G.ARW
    <cal_dir>/B.ARW

For each cal frame we demosaic with `DEMOSAIC_KWARGS` (same as production),
take **the matching channel** (R-lit→ch0, G-lit→ch1, B-lit→ch2), smooth
it to suppress per-pixel noise, and compute a multiplier map:

    ffc_map = reference / smoothed_cal_channel       (clipped to [0.5, 3.0])

`reference` is the mean of the brightest 10% of the smoothed cal — so the
brightest area of the image gets a multiplier of ~1.0 and dimmer corners
get >1.0 lift.

At composite time, each channel of the per-frame triplet is multiplied by
its FFC map before stacking.

Caching
-------
`load_ffc_maps` is `lru_cache`-decorated by absolute path, so calling it
repeatedly within a batch run loads from disk and computes maps exactly
once.
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import numpy as np

from c41_core import ChannelCalibration


logger = logging.getLogger("rgb-composite.ffc")


# Tunables. Conservative defaults — the FFC map should *lift* dim corners,
# not compensate for a fundamentally broken setup.
_FFC_MIN_MULTIPLIER = 0.5
_FFC_MAX_MULTIPLIER = 3.0
# Box-filter kernel size for cal-frame smoothing, as a fraction of the
# shorter image dimension. 5% on a 6336x9504 a7CR frame ≈ 317 px — wide
# enough to wash out per-pixel sensor noise without flattening real
# vignette gradients (which span 1000+ px across the frame).
_SMOOTH_KERNEL_FRAC = 0.05
# Cal-frame channel mean must be above this fraction of full-scale for
# the calibration to be considered "lit enough" to be useful. Catches
# operator-error cases like forgetting to turn on the scanlight, or the
# wrong channel name on the file.
_MIN_CAL_BRIGHTNESS_FRAC = 0.05

# Saturation guard. If too much of the cal-frame channel is clipped at
# the top of the sensor's range, the reference brightness we compute
# from "top 10%" is the *clipped* value, not the true brightness — and
# the resulting FFC map will silently fail to correct the center
# vignette. Operator must redo the cal at a lower scanlight level or
# faster shutter.
_SATURATION_THRESHOLD = 64000           # 1500 below uint16 max
_MAX_SATURATED_PIXEL_FRAC = 0.01        # >1% saturated → reject


@dataclass(frozen=True)
class FFCMaps:
    """Per-channel multiplier maps. Each map is HxW float32.

    `r` is applied to the composite's R channel (which came from the R-lit
    capture's ch0); `g` to the composite's G channel (G-lit ch1); `b` to
    the composite's B channel (B-lit ch2).
    """
    r: np.ndarray
    g: np.ndarray
    b: np.ndarray
    source: Path

    @property
    def shape(self) -> tuple[int, int]:
        return self.r.shape[:2]


class CalibrationError(ValueError):
    """Raised when a calibration directory is unusable.

    Covers: missing R/G/B files, dimension mismatch between cal frames,
    cal frame too dark to be useful, etc.
    """


def _box_filter_2d(arr: np.ndarray, kernel_size: int) -> np.ndarray:
    """Separable box filter via cumulative sums — O(N) and pure numpy.

    Equivalent to scipy.ndimage.uniform_filter(arr, kernel_size) with
    mode='nearest' (constant edge padding). Edges of the cal frame
    extend the vignette pattern; they don't introduce sharp
    discontinuities, so edge padding is the right boundary condition.
    """
    if kernel_size <= 1:
        return arr.astype(np.float32, copy=False)
    # Make kernel odd so the filter is centered.
    if kernel_size % 2 == 0:
        kernel_size += 1
    pad = kernel_size // 2

    def smooth_axis(a: np.ndarray, axis: int) -> np.ndarray:
        # Pad with edge values, then prepend a zero slab to the cumsum so
        # window sums work out as `cum[i+K] - cum[i]`. Without the prepended
        # zero we'd lose one position at each end of the output.
        pad_width = [(0, 0)] * a.ndim
        pad_width[axis] = (pad, pad)
        padded = np.pad(a, pad_width, mode="edge")
        cum = np.cumsum(padded, axis=axis, dtype=np.float64)
        zero_shape = list(padded.shape)
        zero_shape[axis] = 1
        cum = np.concatenate(
            [np.zeros(zero_shape, dtype=np.float64), cum],
            axis=axis,
        )
        n = a.shape[axis]
        upper = np.take(cum, np.arange(kernel_size, kernel_size + n), axis=axis)
        lower = np.take(cum, np.arange(0, n), axis=axis)
        return ((upper - lower) / kernel_size).astype(np.float32)

    out = smooth_axis(arr.astype(np.float32, copy=False), axis=0)
    out = smooth_axis(out, axis=1)
    return out


def compute_ffc_map(cal_channel: np.ndarray) -> np.ndarray:
    """Build a multiplier map from one channel of a cal frame.

    Args:
        cal_channel: HxW uint16 (or float-coerceable) array — one channel
            of a demosaiced blank-light capture.

    Returns:
        HxW float32 multiplier map, clipped to [_FFC_MIN_MULTIPLIER,
        _FFC_MAX_MULTIPLIER]. Brightest area of the cal → ~1.0,
        dimmer corners → >1.0.

    Raises:
        CalibrationError: cal frame is too dark to be useful.
    """
    if cal_channel.ndim != 2:
        raise ValueError(f"expected HxW array, got shape {cal_channel.shape}")

    h, w = cal_channel.shape
    full_scale = float(np.iinfo(cal_channel.dtype).max) if np.issubdtype(
        cal_channel.dtype, np.integer
    ) else 65535.0

    mean_frac = float(cal_channel.mean()) / full_scale
    if mean_frac < _MIN_CAL_BRIGHTNESS_FRAC:
        raise CalibrationError(
            f"cal frame mean brightness {mean_frac:.3f} of full-scale is "
            f"below the {_MIN_CAL_BRIGHTNESS_FRAC:.2f} threshold — was the "
            "scanlight off or was the wrong channel captured?"
        )

    # Saturation guard. Check the RAW channel (not the smoothed version)
    # because clipping is a per-pixel property; smoothing would average
    # clipped pixels with their unclipped neighbors and hide the issue.
    saturated_frac = float((cal_channel >= _SATURATION_THRESHOLD).mean())
    if saturated_frac > _MAX_SATURATED_PIXEL_FRAC:
        raise CalibrationError(
            f"cal frame is over-exposed: {saturated_frac:.3%} of pixels are "
            f">= {_SATURATION_THRESHOLD} (≈ {_SATURATION_THRESHOLD / full_scale:.1%} "
            f"of full-scale). The FFC reference cannot be computed accurately "
            "from clipped data. Re-capture the calibration with a lower scanlight "
            "level or shorter shutter, then retry."
        )

    kernel = max(3, int(min(h, w) * _SMOOTH_KERNEL_FRAC))
    smoothed = _box_filter_2d(cal_channel, kernel)

    # Reference = mean of the brightest 10% of the smoothed cal. This
    # tracks the central plateau of the vignette without being thrown
    # off by one or two hot pixels (which the smoothing already softens).
    flat = smoothed.reshape(-1)
    top_cutoff = np.percentile(flat, 90.0)
    reference = float(flat[flat >= top_cutoff].mean())
    if reference <= 0:
        raise CalibrationError("cal frame reference brightness is zero or negative")

    # Avoid division by zero in dark corners (clipped pixels, dead spots).
    safe = np.maximum(smoothed, reference * 0.05)
    ffc = (reference / safe).astype(np.float32)
    np.clip(ffc, _FFC_MIN_MULTIPLIER, _FFC_MAX_MULTIPLIER, out=ffc)
    return ffc


def _demosaic_cal_frame(path: Path) -> np.ndarray:
    """Demosaic a calibration ARW.

    Imported lazily so the FFC module can be unit-tested without rawpy.
    """
    from .composite import demosaic_linear
    return demosaic_linear(path)


def _resolve_cal_files(cal_dir: Path) -> tuple[Path, Path, Path]:
    """Find R/G/B cal frames in `cal_dir`. Case-insensitive on extension."""
    candidates = {p.name.upper(): p for p in cal_dir.iterdir() if p.is_file()}
    found: dict[str, Path] = {}
    for ch in ("R", "G", "B"):
        for ext in (".ARW", ".arw"):
            key = (ch + ext).upper()
            if key in candidates:
                found[ch] = candidates[key]
                break
    missing = [ch for ch in ("R", "G", "B") if ch not in found]
    if missing:
        raise CalibrationError(
            f"calibration directory {cal_dir} is missing files for "
            f"channel(s) {','.join(missing)} — expected R.ARW, G.ARW, B.ARW"
        )
    return found["R"], found["G"], found["B"]


@functools.lru_cache(maxsize=4)
def load_ffc_maps(cal_dir: Union[str, Path]) -> FFCMaps:
    """Load and cache the FFC maps for a calibration directory.

    Demosaics R.ARW, G.ARW, B.ARW (with the same `DEMOSAIC_KWARGS` as
    production), pulls the matching channel from each, computes a smoothed
    multiplier map per channel.

    Cached by absolute path so repeated calls within a batch run don't
    redo the work.
    """
    cal_dir = Path(cal_dir).resolve()
    if not cal_dir.is_dir():
        raise CalibrationError(f"not a directory: {cal_dir}")

    r_path, g_path, b_path = _resolve_cal_files(cal_dir)

    logger.info("FFC: demosaicing calibration frames from %s", cal_dir)
    r_img = _demosaic_cal_frame(r_path)
    g_img = _demosaic_cal_frame(g_path)
    b_img = _demosaic_cal_frame(b_path)

    if not (r_img.shape == g_img.shape == b_img.shape):
        raise CalibrationError(
            f"calibration shape mismatch: R={r_img.shape} G={g_img.shape} "
            f"B={b_img.shape} — recapture all three at the same crop/zoom"
        )

    logger.info("FFC: building multiplier maps (shape=%s)", r_img.shape[:2])
    r_map = compute_ffc_map(r_img[..., 0])
    g_map = compute_ffc_map(g_img[..., 1])
    b_map = compute_ffc_map(b_img[..., 2])

    return FFCMaps(r=r_map, g=g_map, b=b_map, source=cal_dir)


def apply_ffc_to_channel(channel_data: np.ndarray, ffc_map: np.ndarray) -> np.ndarray:
    """Multiply a uint16 channel by its FFC multiplier map, clip, cast back.

    Args:
        channel_data: HxW uint16 — one channel of a per-frame demosaic
            (the matching channel for which `ffc_map` was computed).
        ffc_map: HxW float32 multiplier map (from `compute_ffc_map`).

    Returns:
        HxW uint16. Clipped to [0, 65535] to handle the rare case where
        a near-saturated pixel × multiplier overflows.

    Raises:
        ValueError: shape mismatch between data and map.
    """
    if channel_data.shape != ffc_map.shape:
        raise ValueError(
            f"FFC shape mismatch: data {channel_data.shape} vs map {ffc_map.shape} — "
            "the calibration was shot at a different crop/zoom than this frame"
        )
    corrected = channel_data.astype(np.float32) * ffc_map
    np.clip(corrected, 0.0, 65535.0, out=corrected)
    return corrected.astype(np.uint16)


def clear_cache() -> None:
    """Clear the `load_ffc_maps` LRU cache. Useful for tests."""
    load_ffc_maps.cache_clear()


# ---------------------------------------------------------------------------
# Radiometric FFC path (Phase 10 R-26 — additive, beside apply_ffc)
# ---------------------------------------------------------------------------
# This is the radiometrically-correct FFC path:
#   corrected = (raw − black) / (avg_flat − black)
# per channel, averaged over an N-frame flat stack captured at working
# brightness after LED warmup.  The existing apply_ffc / compute_ffc_map /
# load_ffc_maps / FFCMaps path remains the default production path —
# apply_ffc_radiometric is PURELY ADDITIVE and does NOT replace it.

def apply_ffc_radiometric(
    raw_array: np.ndarray,
    flat_stack: np.ndarray,
    black_levels: tuple[ChannelCalibration, ChannelCalibration, ChannelCalibration],
) -> np.ndarray:
    """Apply radiometrically-correct flat-field correction to a raw HxWx3 array.

    Averages the N-frame flat stack internally, subtracts per-channel black
    levels from both raw and flat, divides, clamps sub-zero values to 0,
    and returns a uint16 result.  Reuses the ``reference * 0.05`` div-zero
    floor pattern from ``compute_ffc_map`` (ffc.py:207-209).

    Args:
        raw_array:   HxWx3 uint16 — the frame to correct.
        flat_stack:  NxHxWx3 uint16 — N blank-light frames stacked on axis 0.
                     Averaged internally to reduce per-pixel noise.
        black_levels: tuple of 3 ChannelCalibration objects indexed
                     [0]=R, [1]=G, [2]=B (locked project channel order).
                     Each ``.black_level`` is subtracted per channel before
                     dividing by the averaged flat.

    Returns:
        HxWx3 uint16.  Sub-black-level pixels clamp to 0; overflow clips
        at 65535 (single ``np.clip`` covers both, mirroring
        ``apply_ffc_to_channel``).

    Raises:
        ValueError: if raw_array or flat_stack dimensions are wrong, or
                    ``len(black_levels) != 3``.
    """
    # --- preamble validation (Pitfall 1 axis-ordering + Pitfall 3 channel indexing) ---
    if raw_array.ndim != 3 or raw_array.shape[2] != 3:
        raise ValueError(
            f"raw_array must be HxWx3, got shape {raw_array.shape}"
        )
    if flat_stack.ndim != 4 or flat_stack.shape[1:] != raw_array.shape:
        raise ValueError(
            f"flat_stack must be NxHxWx3 where HxWx3 matches raw_array "
            f"{raw_array.shape}, got {flat_stack.shape}"
        )
    if flat_stack.shape[0] < 1:
        raise ValueError(
            f"flat_stack must contain at least 1 frame on axis 0, "
            f"got shape {flat_stack.shape}"
        )
    if len(black_levels) != 3:
        raise ValueError(
            f"black_levels must be a tuple of 3 ChannelCalibration objects, "
            f"got len={len(black_levels)}"
        )

    # 1. Average all N flat frames in float32.  Shape: HxWx3
    avg_flat = np.mean(flat_stack.astype(np.float32), axis=0)

    # 2. Output buffer (same shape and dtype as raw_array)
    out = np.empty_like(raw_array)

    # 3. Per-channel radiometric correction
    for ch_idx, cal in enumerate(black_levels):
        bl = float(cal.black_level)
        raw_ch = raw_array[..., ch_idx].astype(np.float32)
        flat_ch = avg_flat[..., ch_idx]          # already float32

        raw_sub = raw_ch - bl
        flat_sub = flat_ch - bl

        # Div-zero floor — REUSE the compute_ffc_map pattern (ffc.py:207-209):
        #   safe = np.maximum(smoothed, reference * 0.05)
        # Here flat_ref is the mean of the positive (non-black-subtracted) region.
        positive = flat_sub[flat_sub > 0]
        flat_ref = float(positive.mean()) if positive.size else 1.0
        safe_flat = np.maximum(flat_sub, flat_ref * 0.05)

        # The formula outputs in black-subtracted sensor counts:
        #   corrected = (raw − bl) / (flat − bl) * flat_ref
        # where flat_ref = mean(flat_sub[flat_sub > 0]) ≈ (flat − bl) for a
        # uniform-ish flat. The output range is [0, flat_ref] before clip.
        # NOTE: output is NOT in raw sensor counts — black_level has been removed.
        # Phase 11 inversion must NOT re-subtract black_level from this output.
        # This differs from apply_ffc_to_channel, which preserves the black pedestal.
        corrected = (raw_sub / safe_flat) * flat_ref

        # Single clip covers sub-zero (negative clamp) AND overflow (mirror
        # apply_ffc_to_channel lines 298-300 — no separate np.maximum needed).
        np.clip(corrected, 0.0, 65535.0, out=corrected)
        out[..., ch_idx] = corrected.astype(np.uint16)

    return out
