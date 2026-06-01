"""Per-channel exposure auto-calibration for the shared capture loop.

Drives the Orchestrator to capture one dark frame (LED levels 0), measure
per-channel black levels, then independently solves each RGB channel from a
small number of RAW probes.  Calibration treats LED PWM and shutter time as one
linear exposure variable: one safe probe estimates the channel response, shutter
is chosen from the fastest viable camera ladder step, and LED PWM is used as
the fine adjustment.  In IED-backed modes the app cannot control camera shutter
speed, so it uses the same proportional solve with LED only. Returns a
``CalibrationResult`` with three ``ChannelCalibration`` records.

Design notes
------------
- REUSES the Orchestrator capture path via single-channel calibration captures
  and the ``sony_capture_runner=`` injection seam (NFR-14 — do NOT bypass
  the Orchestrator abstraction).
- Hardware-free in tests: inject ``demosaic_factory`` (a callable that
  accepts the Orchestrator and returns a ``Callable[[Path], np.ndarray]``);
  the closure binds to the LIVE Orchestrator so ``orch.settings.level_*``
  is current at demosaic call time.  No rawpy is imported in that path
  (Pitfall 5).
- Warmup sleep is injectable (default ``time.sleep``); tests pass
  ``lambda _: None`` for instant, deterministic runs (NFR-12).
- Single-channel calibration captures do not advance the Orchestrator frame
  counter — they are not part of the roll sequence (Pitfall 4).
- Black-subtracted ROI is ``np.clip(..., 0, None)`` before percentile
  (Pitfall 3) to prevent negative values distorting the calibration metric.
- Channel index LOCKED: R=0, G=1, B=2 (project convention; Pitfall 2).
- SDK calibration uses p99.9 of the selected base patch and targets a configurable
  fraction of the usable RAW range.  This leaves headroom for shutter quantization and avoids
  the old 95%-target hunt that bounced between underexposure and clipping.
- Per-channel probe count is intentionally capped: one safe probe, one verify,
  and at most one proportional correction in the normal path.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from c41_core import ChannelCalibration, CalibrationResult
from c41_core.contracts import BaseRegionDescriptor
from .orchestrator import Orchestrator, CaptureSettings

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_CLIP_CEILING: int = 65535
_SATURATION_VALUE: int = 65400
_CH_IDX: dict[str, int] = {"R": 0, "G": 1, "B": 2}
_CH_LEVEL_FIELD: dict[str, str] = {"R": "level_r", "G": "level_g", "B": "level_b"}
_CH_SHUTTER_FIELD: dict[str, str] = {"R": "shutter_r", "G": "shutter_g", "B": "shutter_b"}
# Minimum p99 (counts) after black-subtraction before we consider the signal real.
# Exact-zero equality misses near-zero signals from ADC offset / read noise / hot pixels
# on real hardware.  10 counts is comfortably above typical dark-frame read noise.
_MIN_SIGNAL_P99: float = 10.0
_MAX_CLIP_FRACTION: float = 0.0001
_RAW_SATURATION_MARGIN: int = 16
_DEFAULT_SHUTTER_SPEED: str = "1/40"
_PROBE_SHUTTER_SPEED: str = "1/15"
_PROBE_LED_LEVEL: int = 200
_PWM_MIN: int = 40
_PWM_MAX: int = 255
_SHUTTER_CANDIDATES: tuple[str, ...] = (
    "1/250", "1/200", "1/160", "1/125", "1/100", "1/80", "1/60",
    "1/50", "1/40", "1/30", "1/25", "1/20", "1/15", "1/13",
    "1/10", "1/8", "1/6", "1/5", "1/4", "1/3", "0.4", "1/2",
    "0.6", "0.8", "1",
)
_MAX_CALIBRATION_SHUTTER_SECONDS: float = 1.0
_FAST_SHUTTER_ACCEPTABLE_FRACTION: float = 0.85
_PREFERRED_LED_MIN: int = 245
_PREFERRED_LED_MAX: int = 255
_LED_PENALTY_PER_LEVEL: float = 50.0
_SHUTTER_SECONDS_PENALTY: float = 100_000.0
_UNDER_FLOOR_PENALTY: float = 20.0


def _shutter_seconds(shutter_speed: str) -> float:
    label = shutter_speed.strip().lower().replace("sec", "").replace("s", "").replace('"', "")
    if "/" in label:
        num, den = label.split("/", 1)
        return float(num) / float(den)
    return float(label)


def _parse_supported_shutters(detail: str) -> list[str]:
    """Extract shutter labels from ``sony-capture --list-shutter-speeds`` output."""
    supported: set[str] = set()
    for raw_line in str(detail).splitlines():
        line = raw_line.strip()
        if not line or "raw=" not in line or "current=" in line:
            continue
        label = line.split(" ", 1)[0].strip()
        try:
            _shutter_seconds(label)
        except (TypeError, ValueError):
            continue
        supported.add(label)
    return sorted(supported, key=_shutter_seconds)


def _candidate_shutters(initial: Optional[str], supported: Optional[list[str]] = None) -> list[str]:
    source = list(supported or _SHUTTER_CANDIDATES)
    candidates: list[str] = []
    for label in source:
        try:
            seconds = _shutter_seconds(label)
        except (TypeError, ValueError):
            continue
        if 0.0 < seconds <= _MAX_CALIBRATION_SHUTTER_SECONDS:
            candidates.append(label)

    if initial and initial not in candidates:
        try:
            if 0.0 < _shutter_seconds(initial) <= _MAX_CALIBRATION_SHUTTER_SECONDS:
                candidates.append(initial)
        except (TypeError, ValueError):
            pass

    return sorted(set(candidates), key=_shutter_seconds)


def _nearest_shutter(label: str, candidates: list[str]) -> str:
    if label in candidates:
        return label
    target = _shutter_seconds(label)
    return min(candidates, key=lambda c: abs(_shutter_seconds(c) - target))


def _at_or_slower_shutter(required_seconds: float, candidates: list[str]) -> Optional[str]:
    for candidate in candidates:
        if _shutter_seconds(candidate) >= required_seconds:
            return candidate
    return candidates[-1] if candidates else None


def _at_or_faster_shutter(required_seconds: float, candidates: list[str]) -> Optional[str]:
    eligible = [c for c in candidates if _shutter_seconds(c) <= required_seconds]
    return eligible[-1] if eligible else (candidates[0] if candidates else None)


def _initial_shutter(settings: CaptureSettings, ch: str) -> str:
    return getattr(settings, _CH_SHUTTER_FIELD[ch]) or _DEFAULT_SHUTTER_SPEED


def _adjacent_shutter(
    *,
    current: str,
    candidates: list[str],
    led_level: int,
    best_p99: float,
    clip_fraction: float,
    target: int,
    tolerance: int,
    converged: bool,
) -> Optional[str]:
    try:
        idx = candidates.index(current)
    except ValueError:
        return None

    target_floor = float(target) * _FAST_SHUTTER_ACCEPTABLE_FRACTION
    current_seconds = _shutter_seconds(current)

    if clip_fraction > _MAX_CLIP_FRACTION:
        return None

    if best_p99 < target_floor and led_level >= 255 and idx < len(candidates) - 1:
        required = current_seconds * (target_floor / max(best_p99, _MIN_SIGNAL_P99))
        next_shutter = _at_or_slower_shutter(required, candidates[idx + 1 :])
        return next_shutter or candidates[idx + 1]

    if best_p99 >= target_floor and idx > 0:
        if led_level < _PREFERRED_LED_MIN:
            required = current_seconds * (float(led_level) / 255.0)
        elif converged:
            required = current_seconds * (target_floor / max(best_p99, _MIN_SIGNAL_P99))
        else:
            return None

        faster_candidates = candidates[:idx]
        next_shutter = _at_or_slower_shutter(required, faster_candidates)
        if next_shutter is None:
            next_shutter = _at_or_faster_shutter(required, faster_candidates)
        if next_shutter and _shutter_seconds(next_shutter) < current_seconds:
            return next_shutter
    return None


def _attempt_score(attempt: dict[str, object], target: int) -> float:
    led = int(attempt["led_level"])
    p99 = float(attempt["p99"])
    clip_fraction = float(attempt.get("clip_fraction", 0.0))
    sensor_clip_fraction = float(attempt.get("sensor_clip_fraction", 0.0))
    effective_clip_fraction = max(clip_fraction, sensor_clip_fraction)
    led_penalty = float(max(0, _PREFERRED_LED_MAX - led)) * _LED_PENALTY_PER_LEVEL

    target_floor = float(target) * _FAST_SHUTTER_ACCEPTABLE_FRACTION
    signal_penalty = abs(p99 - target) * 0.1
    if p99 < target_floor:
        signal_penalty += (target_floor - p99) * _UNDER_FLOOR_PENALTY

    clip_penalty = 0.0
    if effective_clip_fraction > _MAX_CLIP_FRACTION:
        clip_penalty = (
            float(target) * 100.0
            + (effective_clip_fraction - _MAX_CLIP_FRACTION) * float(target) * 1000.0
        )

    shutter_penalty = 0.0
    shutter_speed = str(attempt.get("shutter_speed", ""))
    if shutter_speed:
        try:
            shutter_penalty = _shutter_seconds(shutter_speed) * _SHUTTER_SECONDS_PENALTY
        except ValueError:
            shutter_penalty = 0.0

    return signal_penalty + led_penalty + clip_penalty + shutter_penalty


def _exposure_status(
    *,
    p99: float,
    target: int,
    clip_fraction: float,
    tolerance: int,
) -> str:
    if clip_fraction > _MAX_CLIP_FRACTION:
        return "clipped"
    if abs(p99 - float(target)) <= float(tolerance):
        return "target"
    if p99 < float(target):
        return "under"
    return "hot"


def _next_probe_level(
    *,
    probe_level: int,
    p99: float,
    target: int,
    clip_fraction: float,
    clean_lower_bound: Optional[int],
    clipped_upper_bound: Optional[int],
) -> int:
    """Choose the next LED probe without walking slowly through clipped levels."""
    probe_level = int(np.clip(probe_level, 1, 255))

    if clip_fraction > _MAX_CLIP_FRACTION:
        if clean_lower_bound is not None and clean_lower_bound < probe_level:
            midpoint = (int(clean_lower_bound) + probe_level) // 2
            return int(np.clip(midpoint, 1, probe_level - 1))
        return int(np.clip(round(probe_level * 0.75), 1, probe_level - 1))

    if p99 < _MIN_SIGNAL_P99:
        next_level = 255
    else:
        next_level = int(round(float(probe_level) * float(target) / float(p99)))
        next_level = int(np.clip(next_level, 1, 255))

    if clipped_upper_bound is not None and next_level >= clipped_upper_bound:
        upper = int(clipped_upper_bound)
        if probe_level < upper:
            next_level = (probe_level + upper) // 2
        else:
            next_level = upper - 1
        next_level = int(np.clip(next_level, 1, 255))

    if next_level == probe_level:
        if p99 < target and probe_level < 255:
            next_level = min(255, probe_level + 1)
        elif p99 > target and probe_level > 1:
            next_level = max(1, probe_level - 1)

    return int(np.clip(next_level, 1, 255))


def _metric_tolerance(target: int, tolerance: int) -> int:
    """Use a practical ±5% exposure band unless the caller asks for wider."""
    return max(int(tolerance), int(round(float(target) * 0.05)))


def _target_for_black_level(black_level: float, target_fraction: float) -> int:
    usable_range = max(1.0, float(_CLIP_CEILING) - float(black_level))
    return int(round(float(target_fraction) * usable_range))


def _calibrated_base_region(
    base_region: BaseRegionDescriptor,
    ch_cals: dict[str, ChannelCalibration],
) -> BaseRegionDescriptor:
    """Return the selected ROI with the calibrated per-channel base signal.

    The incoming descriptor is only a crop coordinate carrier in the app route.
    During exposure calibration the real base signal is the black-subtracted
    p99.9 used to solve each channel. Store that measured signal so downstream
    checks and stock profiles do not evaluate the placeholder orange-mask tuple.
    """
    base_rgb = (
        float(ch_cals["R"].p99),
        float(ch_cals["G"].p99),
        float(ch_cals["B"].p99),
    )
    return BaseRegionDescriptor(
        x=base_region.x,
        y=base_region.y,
        w=base_region.w,
        h=base_region.h,
        base_rgb=base_rgb,
        uniformity_cv=base_region.uniformity_cv,
        source=base_region.source,
        schema_version=base_region.schema_version,
    )


def _choose_probe_shutter(candidates: list[str]) -> str:
    if not candidates:
        return _DEFAULT_SHUTTER_SPEED
    return _nearest_shutter(_PROBE_SHUTTER_SPEED, candidates)


def _halved_probe_shutter(current: str, candidates: list[str]) -> Optional[str]:
    """Return the nearest writable shutter that is about one stop faster."""
    if not candidates:
        return None
    try:
        current_seconds = _shutter_seconds(current)
    except (TypeError, ValueError):
        return None
    faster_candidates = [c for c in candidates if _shutter_seconds(c) < current_seconds]
    if not faster_candidates:
        return None
    target_seconds = current_seconds / 2.0
    next_shutter = _at_or_faster_shutter(target_seconds, faster_candidates)
    return next_shutter or faster_candidates[-1]


def _next_slower_shutter(current: str, candidates: list[str]) -> Optional[str]:
    """Return the next slower writable shutter than ``current``."""
    if not candidates:
        return None
    try:
        current_seconds = _shutter_seconds(current)
    except (TypeError, ValueError):
        return None
    slower = [c for c in candidates if _shutter_seconds(c) > current_seconds]
    return slower[0] if slower else None


def _solve_pwm_shutter_pair(
    *,
    signal: float,
    probe_level: int,
    probe_shutter: str,
    target: int,
    candidates: list[str],
) -> tuple[int, str, str]:
    """Solve exposure from one clean probe.

    Returns ``(led_level, shutter_speed, status_hint)``.  ``status_hint`` is
    advisory; the verification capture still decides the final channel status.
    """
    if signal < _MIN_SIGNAL_P99:
        return (_PWM_MAX, candidates[-1] if candidates else probe_shutter, "under")

    probe_seconds = _shutter_seconds(probe_shutter)
    if probe_seconds <= 0:
        return (_PWM_MAX, candidates[-1] if candidates else probe_shutter, "under")

    k = float(signal) / (float(probe_level) * probe_seconds)
    if k <= 0:
        return (_PWM_MAX, candidates[-1] if candidates else probe_shutter, "under")

    nearest_boundary: Optional[tuple[float, int, str, str]] = None
    for shutter in candidates:
        shutter_seconds = _shutter_seconds(shutter)
        if shutter_seconds <= 0:
            continue
        pwm_needed = float(target) / (k * shutter_seconds)
        if _PWM_MIN <= pwm_needed <= _PWM_MAX:
            return (int(np.clip(round(pwm_needed), _PWM_MIN, _PWM_MAX)), shutter, "candidate")

        bounded_pwm = int(np.clip(round(pwm_needed), _PWM_MIN, _PWM_MAX))
        predicted = k * shutter_seconds * float(bounded_pwm)
        log_error = abs(np.log(max(predicted, _MIN_SIGNAL_P99) / max(float(target), 1.0)))
        hint = "floor" if pwm_needed < _PWM_MIN else "under"
        boundary = (float(log_error), bounded_pwm, shutter, hint)
        if nearest_boundary is None or boundary[0] < nearest_boundary[0]:
            nearest_boundary = boundary

    if nearest_boundary is not None:
        _score, led_level, shutter, hint = nearest_boundary
        return (led_level, shutter, hint)
    return (_PWM_MAX, candidates[-1] if candidates else probe_shutter, "under")


def _solve_bracket_refinement(
    *,
    clean_signal: float,
    clean_level: int,
    clean_shutter: str,
    clipped_shutter: str,
    target: int,
    candidates: list[str],
) -> Optional[tuple[int, str, str]]:
    """Choose one shutter inside a clean/clipped bracket.

    Real camera runs can have a one-stop gap where the slower solved shutter
    clips hard, while the one-stop-faster retry is clean but dim even at LED
    255. Sony exposes intermediate third-stop labels, so try the best
    intermediate instead of giving up at the fast edge of the bracket.
    """
    if clean_signal < _MIN_SIGNAL_P99 or clean_level <= 0:
        return None

    try:
        clean_seconds = _shutter_seconds(clean_shutter)
        clipped_seconds = _shutter_seconds(clipped_shutter)
    except (TypeError, ValueError):
        return None

    if clean_seconds <= 0 or clipped_seconds <= clean_seconds:
        return None

    bracket = [
        shutter for shutter in candidates
        if clean_seconds < _shutter_seconds(shutter) < clipped_seconds
    ]
    if not bracket:
        return None

    k = float(clean_signal) / (float(clean_level) * clean_seconds)
    if k <= 0:
        return None

    target_floor = float(target) * _FAST_SHUTTER_ACCEPTABLE_FRACTION
    fallback: Optional[tuple[float, int, str, str]] = None
    for shutter in bracket:
        seconds = _shutter_seconds(shutter)
        pwm_needed = float(target) / (k * seconds)
        if _PWM_MIN <= pwm_needed <= _PWM_MAX:
            return (
                int(np.clip(round(pwm_needed), _PWM_MIN, _PWM_MAX)),
                shutter,
                "bracket-target",
            )

        bounded_pwm = int(np.clip(round(pwm_needed), _PWM_MIN, _PWM_MAX))
        predicted = k * seconds * float(bounded_pwm)
        if predicted >= target_floor:
            log_error = abs(np.log(max(predicted, _MIN_SIGNAL_P99) / max(float(target), 1.0)))
            candidate = (float(log_error), bounded_pwm, shutter, "bracket-floor")
            if fallback is None or candidate[0] < fallback[0]:
                fallback = candidate

    if fallback is not None:
        _score, led_level, shutter, hint = fallback
        return (led_level, shutter, hint)

    # Still below the acceptable floor throughout the bracket. Use the slowest
    # intermediate at max LED; it is the brightest clean candidate we have not
    # tried yet, while still staying below the known clipped shutter.
    slowest = bracket[-1]
    return (_PWM_MAX, slowest, "bracket-under")


def _correct_led_level(*, current_level: int, signal: float, target: int) -> int:
    if signal < _MIN_SIGNAL_P99:
        return _PWM_MAX
    corrected = int(round(float(current_level) * (float(target) / float(signal))))
    return int(np.clip(corrected, _PWM_MIN, _PWM_MAX))


def _raw_sensor_clip_fraction(
    path: Path,
    channel: str,
    base_region: BaseRegionDescriptor,
) -> float:
    """Return source-Bayer clip fraction for the selected channel's photosites.

    The normal calibration histogram is measured after rawpy demosaic/color
    conversion. Real a7CR captures can have clipped source red photosites while
    the demosaiced ProPhoto red channel remains below the demosaiced saturation
    threshold, so we also inspect the raw Bayer samples before demosaic/color
    conversion. The returned value is the worse of the selected rebate ROI and
    the full channel plane, so a clean corner ROI cannot hide frame-wide source
    clipping.
    """
    import rawpy  # pragma: no cover - exercised only with real ARWs

    with rawpy.imread(str(path)) as raw:  # pragma: no cover
        img = raw.raw_image_visible
        white = raw.white_level or int(np.max(img))
        if white <= 0:
            return 0.0

        pattern = np.asarray(raw.raw_pattern)
        ph, pw = pattern.shape
        color_desc = raw.color_desc.decode("ascii", errors="ignore")
        target = channel.upper()
        threshold = max(0, int(white) - _RAW_SATURATION_MARGIN)

        def clip_fraction_for(sample: np.ndarray, origin_x: int, origin_y: int) -> float:
            clipped = 0
            total = 0
            for py in range(ph):
                for px in range(pw):
                    color_index = int(pattern[py, px])
                    if color_index >= len(color_desc):
                        continue
                    if color_desc[color_index].upper() != target:
                        continue
                    row_start = (py - origin_y) % ph
                    col_start = (px - origin_x) % pw
                    plane = sample[row_start::ph, col_start::pw]
                    if plane.size:
                        clipped += int(np.count_nonzero(plane >= threshold))
                        total += int(plane.size)
            if total <= 0:
                return 0.0
            return float(clipped) / float(total)

        x0 = max(0, int(base_region.x))
        y0 = max(0, int(base_region.y))
        x1 = min(img.shape[1], x0 + max(0, int(base_region.w)))
        y1 = min(img.shape[0], y0 + max(0, int(base_region.h)))
        roi = img[y0:y1, x0:x1]
        roi_clip = clip_fraction_for(roi, x0, y0) if roi.size else 0.0
        frame_clip = clip_fraction_for(img, 0, 0)
        return float(np.clip(max(roi_clip, frame_clip), 0.0, 1.0))


def calibrate_exposure(
    scanlight,
    settings: CaptureSettings,
    base_region: BaseRegionDescriptor,
    *,
    ffc_cal_dir: str = "",
    max_iterations: int = 7,
    target_fraction: float = 0.85,
    tolerance: int = 2000,
    warmup_s: float = 2.0,
    sony_capture_runner: Optional[Callable[[str, Path, int], int]] = None,
    sleep: Callable[[float], None] = time.sleep,
    demosaic_factory: Optional[Callable[["Orchestrator"], Callable[[Path], np.ndarray]]] = None,
    sensor_clip_factory: Optional[
        Callable[["Orchestrator"], Callable[[Path, str, BaseRegionDescriptor], float]]
    ] = None,
    orchestrator: Optional["Orchestrator"] = None,
    seed_recipe: Optional[dict[str, tuple[int, str]]] = None,
    call_id: Optional[str] = None,
) -> CalibrationResult:
    """Auto-tune each LED channel's exposure referenced to the rebate histogram.

    Drives the shared Orchestrator capture loop to:
      1. Capture one dark frame (all LEDs at 0) to measure per-channel black levels.
      2. For each channel (R, G, B) independently, capture one safe probe,
         analytically solve the fastest viable shutter plus LED PWM trim, then
         verify against the rebate's p99.9 (black-subtracted) at the configured
         fraction of the usable RAW range.
      3. Assemble and return a CalibrationResult whose base_region.base_rgb is
         the measured calibrated base signal for the selected ROI.

    Blue ends up highest automatically because the orange mask attenuates blue
    ~2-4 stops.  No by-eye color judgment is performed.

    Args:
        scanlight:         Scanlight instance (already connected).
        settings:          CaptureSettings; used for Orchestrator construction.
                           Shutter and LED levels are mutated during calibration via
                           update_settings() but the original settings object
                           is not modified (dataclasses.replace semantics).
        base_region:       BaseRegionDescriptor for the rebate strip ROI.
                           Produced by Phase 09 detect_rebate / manual picker.
        ffc_cal_dir:       Path reference for FFC calibration data (stored in
                           CalibrationResult; not used internally).
        max_iterations:    Legacy guard kept for API compatibility. The analytic
                           solver normally uses 2-3 captures per channel and
                           still requires max_iterations >= 1.
        target_fraction:   Target rebate p99.9 as fraction of usable RAW range
                           above black level.
        tolerance:         Minimum convergence tolerance in raw counts. The
                           solver uses at least a practical +/-5% band because
                           real RAW pulls have shot noise and transfer variance.
        warmup_s:          Seconds to sleep after Orchestrator construction
                           before the dark frame.  Inject sleep=lambda _: None
                           for instant tests.
        sony_capture_runner: Injected runner for the Orchestrator (same kwarg
                           as Orchestrator.__init__).  Tests pass a stub.
        sleep:             Injectable sleep callable; default time.sleep.
                           Tests pass lambda _: None for instant warmup.
        demosaic_factory:  Optional factory callable — called as
                           ``demosaic_factory(orch)`` after Orchestrator
                           construction to produce a ``Callable[[Path], np.ndarray]``
                           demosaic function that closes over the LIVE
                           ``orch.settings``.  Tests inject this seam for
                           hardware-free operation.  When None, the default
                           ``_demosaic_cal_frame`` from ``rgb_composite.ffc``
                           is used via lazy import (so rawpy is never touched
                           when a factory is injected — Pitfall 5).
        sensor_clip_factory:
                           Optional factory for source-Bayer clipping checks.
                           Tests inject this to stay hardware-free. In real RAW
                           runs, the default uses rawpy lazily and checks the
                           selected photosites before demosaic/color conversion.
        orchestrator:      Optional live Orchestrator to reuse instead of
                           constructing a new one at STEP 0.  When provided,
                           the route handler's shared Orchestrator (and its
                           ``_lock``) is reused so calibration captures cannot
                           interleave with a concurrent /api/capture scan.
                           Default None preserves all Phase-12 behaviour.
        seed_recipe:       Optional prior per-stock RGB recipe. Each channel maps
                           to ``(led_level, shutter_speed)`` and is tried before
                           the generic probe. A clean, target seed can finish the
                           channel in one capture; otherwise it seeds the linear
                           solve.
        call_id:           Optional per-request calibration ID. When supplied, it
                           is attached to every scan_log event so UI progress can
                           distinguish the active run from prior calibration runs
                           in the same backend session.

    Returns:
        CalibrationResult with three ChannelCalibration records (r/g/b),
        the passed base_region, and ffc_cal_dir.

    Raises:
        RuntimeError: if any capture (dark frame or calibration frame) fails.
        ValueError: if max_iterations < 1, or if a channel produces no signal
            after black-level subtraction (dark frame brighter than signal or
            black_level miscalibrated).
    """
    # Guard: caller must allow at least one iteration
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")

    # ------------------------------------------------------------------
    # STEP 0: Warmup sleep + construct ONE Orchestrator (NFR-14 pattern)
    # When an orchestrator is injected (e.g. by the Flask calibrate route)
    # reuse it directly so calibration captures share the existing _lock
    # with /api/capture and cannot interleave with a live scan.
    # ------------------------------------------------------------------
    sleep(warmup_s)

    if orchestrator is not None:
        orch = orchestrator
    else:
        orch = Orchestrator(
            scanlight,
            settings,
            sony_capture_runner=sony_capture_runner,
            sleep=sleep,
        )
    original_settings = {
        "level_r": orch.settings.level_r,
        "level_g": orch.settings.level_g,
        "level_b": orch.settings.level_b,
        "shutter_r": orch.settings.shutter_r,
        "shutter_g": orch.settings.shutter_g,
        "shutter_b": orch.settings.shutter_b,
    }

    def restore_original_settings() -> None:
        orch.update_settings(**original_settings)

    # ------------------------------------------------------------------
    # STEP 0b: Resolve demosaic seam (Pitfall 5 — lazy import)
    # ------------------------------------------------------------------
    if demosaic_factory is not None:
        dem = demosaic_factory(orch)
    else:
        def dem(path: Path) -> np.ndarray:  # pragma: no cover
            from rgb_composite.ffc import _demosaic_cal_frame
            return _demosaic_cal_frame(path)

    if sensor_clip_factory is not None:
        sensor_clip_fraction_for = sensor_clip_factory(orch)
    elif demosaic_factory is None:
        sensor_clip_fraction_for = _raw_sensor_clip_fraction
    else:
        def sensor_clip_fraction_for(
            _path: Path,
            _channel: str,
            _base_region: BaseRegionDescriptor,
        ) -> float:
            return 0.0

    def calibration_log(event: str, **kwargs) -> None:
        log_path = orch.settings.output_folder / "scan_log.jsonl"
        if call_id:
            kwargs["call_id"] = call_id
        Orchestrator._append_log(
            log_path,
            event,
            frame=orch.settings.frame_number,
            roll=orch.settings.roll_name,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # STEP 1: Dark frame — measure per-channel black levels
    # Level 0 is valid for CaptureSettings (0 <= v <= 255); all LEDs off.
    # ------------------------------------------------------------------
    shutter_control_enabled = orch.settings.trigger_mode == "sdk"
    supported_shutters: list[str] = []
    if shutter_control_enabled:
        shutter_ok, shutter_message = orch.sdk_shutter_control_preflight()
        if not shutter_ok:
            raise RuntimeError(shutter_message)
        supported_shutters = _parse_supported_shutters(shutter_message)

    sdk_default_candidates = _candidate_shutters(_DEFAULT_SHUTTER_SPEED, supported_shutters)
    sdk_initial_shutter = _nearest_shutter(_DEFAULT_SHUTTER_SPEED, sdk_default_candidates)
    initial_shutters = {
        ch: (sdk_initial_shutter if shutter_control_enabled else _initial_shutter(orch.settings, ch))
        for ch in ("R", "G", "B")
    }
    cal_prefix = f"{orch.settings.roll_name}_Frame{orch.settings.frame_number:03d}_Cal"
    dark_updates = {"level_r": 0, "level_g": 0, "level_b": 0}
    if shutter_control_enabled:
        dark_updates.update({
            "shutter_r": initial_shutters["R"],
            "shutter_g": initial_shutters["G"],
            "shutter_b": initial_shutters["B"],
        })
    orch.update_settings(**dark_updates)
    dark_result = orch.capture_channel(
        "R",
        level=0,
        shutter_speed=initial_shutters["R"] if shutter_control_enabled else None,
        out_path=orch.settings.output_folder / f"{cal_prefix}_Dark.ARW",
        label="dark-frame",
    )
    if not dark_result.success:
        restore_original_settings()
        raise RuntimeError(f"dark-frame capture failed: {dark_result.error}")

    dark_img = dem(dark_result.files["R"])                          # HxWx3 uint16
    black_level: dict[str, float] = {}
    for ch, ch_idx in _CH_IDX.items():
        dark_ch = dark_img[..., ch_idx].astype(np.float64)           # HxW
        black_level[ch] = float(np.median(dark_ch))  # median: robust to hot pixels / stuck columns

    # ------------------------------------------------------------------
    # STEP 2: Per-channel analytic calibration
    # ------------------------------------------------------------------
    ch_cals: dict[str, ChannelCalibration] = {}

    def is_clean_attempt(attempt: dict[str, object]) -> bool:
        return float(attempt.get("clip_fraction", 0.0)) <= _MAX_CLIP_FRACTION

    def is_measurable_attempt(attempt: dict[str, object]) -> bool:
        return float(attempt["p99"]) >= _MIN_SIGNAL_P99

    def is_target_attempt(attempt: dict[str, object], target_signal: int, metric_tolerance: int) -> bool:
        return (
            is_clean_attempt(attempt)
            and is_measurable_attempt(attempt)
            and abs(float(attempt["p99"]) - float(target_signal)) <= float(metric_tolerance)
        )

    def capture_probe(
        ch: str,
        *,
        led_level: int,
        shutter_speed: str,
        target_signal: int,
        phase: str,
    ) -> dict[str, object]:
        ch_idx = _CH_IDX[ch]
        level_field = _CH_LEVEL_FIELD[ch]
        shutter_field = _CH_SHUTTER_FIELD[ch]
        other_fields = {_CH_LEVEL_FIELD[c]: 0 for c in ("R", "G", "B") if c != ch}

        led_level = int(np.clip(int(led_level), 0, 255))
        updates: dict[str, object] = {level_field: led_level, **other_fields}
        if shutter_control_enabled:
            updates[shutter_field] = shutter_speed
        orch.update_settings(**updates)

        result = orch.capture_channel(
            ch,
            level=led_level,
            shutter_speed=shutter_speed if shutter_control_enabled else None,
            out_path=orch.settings.output_folder / f"{cal_prefix}_{ch}.ARW",
            label=f"exposure-{ch}",
        )
        if not result.success:
            restore_original_settings()
            raise RuntimeError(
                f"calibration capture failed for channel {ch}: {result.error}"
            )

        img = dem(result.files[ch])
        roi_raw = img[
            base_region.y : base_region.y + base_region.h,
            base_region.x : base_region.x + base_region.w,
            ch_idx,
        ]
        roi_f32 = np.clip(
            roi_raw.astype(np.float32) - black_level[ch], 0.0, None
        )
        p99 = float(np.percentile(roi_f32, 99))
        p999 = float(np.percentile(roi_f32, 99.9))
        metric = p999
        output_clip_fraction = float(np.mean(roi_raw >= _SATURATION_VALUE))
        output_clip_fraction = float(np.clip(output_clip_fraction, 0.0, 1.0))
        sensor_clip_fraction = sensor_clip_fraction_for(result.files[ch], ch, base_region)
        sensor_clip_fraction = float(np.clip(sensor_clip_fraction, 0.0, 1.0))
        clip_fraction = max(output_clip_fraction, sensor_clip_fraction)
        status = _exposure_status(
            p99=metric,
            target=target_signal,
            clip_fraction=clip_fraction,
            tolerance=_metric_tolerance(target_signal, tolerance),
        )

        calibration_log(
            "calibration_probe",
            channel=ch,
            phase=phase,
            level=led_level,
            shutter_speed=shutter_speed if shutter_control_enabled else "",
            metric="p99.9",
            p99=round(p99, 2),
            p999=round(p999, 2),
            clip_fraction=round(clip_fraction, 6),
            output_clip_fraction=round(output_clip_fraction, 6),
            sensor_clip_fraction=round(sensor_clip_fraction, 6),
            target=target_signal,
            max_clip_fraction=_MAX_CLIP_FRACTION,
            exposure_status=status,
        )

        return {
            "channel": ch,
            "shutter_speed": shutter_speed if shutter_control_enabled else "",
            "led_level": led_level,
            "p99": metric,
            "p999": p999,
            "p99_raw": p99,
            "clip_fraction": clip_fraction,
            "output_clip_fraction": output_clip_fraction,
            "sensor_clip_fraction": sensor_clip_fraction,
            "exposure_status": status,
            "phase": phase,
            "roi_raw": roi_raw.copy(),
        }

    for ch in ("R", "G", "B"):
        target_signal = _target_for_black_level(black_level[ch], target_fraction)
        metric_tolerance = _metric_tolerance(target_signal, tolerance)
        candidates = _candidate_shutters(initial_shutters[ch], supported_shutters)
        if not candidates:
            candidates = [_DEFAULT_SHUTTER_SPEED]
        probe_shutter = _choose_probe_shutter(candidates) if shutter_control_enabled else ""
        attempts: list[dict[str, object]] = []
        best_attempt: Optional[dict[str, object]] = None

        def usable_probe_after_clip_retry(probe: dict[str, object]) -> dict[str, object]:
            if is_clean_attempt(probe):
                return probe

            retry_shutter = (
                _halved_probe_shutter(str(probe["shutter_speed"]), candidates)
                if shutter_control_enabled else None
            )
            if retry_shutter is not None:
                faster_probe = capture_probe(
                    ch,
                    led_level=int(probe["led_level"]),
                    shutter_speed=retry_shutter,
                    target_signal=target_signal,
                    phase=f"{probe['phase']}-faster",
                )
                attempts.append(faster_probe)
                if is_clean_attempt(faster_probe) and is_measurable_attempt(faster_probe):
                    return faster_probe

            fallback_shutter = retry_shutter or str(probe["shutter_speed"])
            floor_probe = capture_probe(
                ch,
                led_level=_PWM_MIN,
                shutter_speed=fallback_shutter,
                target_signal=target_signal,
                phase=f"{probe['phase']}-floor",
            )
            attempts.append(floor_probe)
            return floor_probe

        seed = (seed_recipe or {}).get(ch)
        if seed is not None:
            seed_level, seed_shutter = seed
            if shutter_control_enabled:
                seed_shutter = _nearest_shutter(seed_shutter, candidates) if seed_shutter else probe_shutter
            else:
                seed_shutter = ""
            seed_probe = capture_probe(
                ch,
                led_level=seed_level,
                shutter_speed=seed_shutter,
                target_signal=target_signal,
                phase="seed",
            )
            attempts.append(seed_probe)
            seed_probe = usable_probe_after_clip_retry(seed_probe)
            if is_target_attempt(seed_probe, target_signal, metric_tolerance):
                best_attempt = seed_probe
            else:
                probe = seed_probe
        else:
            probe = capture_probe(
                ch,
                led_level=_PROBE_LED_LEVEL,
                shutter_speed=probe_shutter,
                target_signal=target_signal,
                phase="probe",
            )
            attempts.append(probe)
            probe = usable_probe_after_clip_retry(probe)

        if best_attempt is None and float(probe["p99"]) < _MIN_SIGNAL_P99 and shutter_control_enabled:
            boost_probe = capture_probe(
                ch,
                led_level=_PWM_MAX,
                shutter_speed=candidates[-1],
                target_signal=target_signal,
                phase="probe-boost",
            )
            attempts.append(boost_probe)
            probe = usable_probe_after_clip_retry(boost_probe)

        if best_attempt is None and float(probe["p99"]) < _MIN_SIGNAL_P99:
            restore_original_settings()
            raise ValueError(
                f"channel {ch}: rebate signal is zero after black subtraction at every "
                f"probe level — dark frame (black_level={black_level[ch]:.1f}) is "
                f"brighter than the calibration signal, or black_level is miscalibrated"
            )

        if best_attempt is None:
            if shutter_control_enabled:
                solved_led, solved_shutter, solve_hint = _solve_pwm_shutter_pair(
                    signal=float(probe["p99"]),
                    probe_level=int(probe["led_level"]),
                    probe_shutter=str(probe["shutter_speed"]),
                    target=target_signal,
                    candidates=candidates,
                )
            else:
                solved_led = _correct_led_level(
                    current_level=int(probe["led_level"]),
                    signal=float(probe["p99"]),
                    target=target_signal,
                )
                solved_shutter = ""
                solve_hint = "candidate"

            calibration_log(
                "calibration_solve",
                channel=ch,
                source_level=int(probe["led_level"]),
                source_shutter=str(probe["shutter_speed"]),
                source_p999=round(float(probe["p99"]), 2),
                next_level=solved_led,
                next_shutter=solved_shutter,
                target=target_signal,
                solve_hint=solve_hint,
            )

            verify = capture_probe(
                ch,
                led_level=solved_led,
                shutter_speed=solved_shutter,
                target_signal=target_signal,
                phase="verify",
            )
            attempts.append(verify)

            clipped_verify_shutter: Optional[str] = None
            if not is_clean_attempt(verify):
                clipped_verify_shutter = str(verify["shutter_speed"])
                retry_shutter = (
                    _halved_probe_shutter(str(verify["shutter_speed"]), candidates)
                    if shutter_control_enabled else None
                )
                if retry_shutter is not None:
                    faster_verify = capture_probe(
                        ch,
                        led_level=int(verify["led_level"]),
                        shutter_speed=retry_shutter,
                        target_signal=target_signal,
                        phase="verify-faster",
                    )
                    attempts.append(faster_verify)
                    if is_clean_attempt(faster_verify):
                        verify = faster_verify

            post_verify_attempt = verify
            corrected_level = _correct_led_level(
                current_level=int(verify["led_level"]),
                signal=float(verify["p99"]),
                target=target_signal,
            )
            should_correct = (
                float(verify["clip_fraction"]) > _MAX_CLIP_FRACTION
                or abs(float(verify["p99"]) - float(target_signal)) > float(metric_tolerance)
            )
            if should_correct and corrected_level != int(verify["led_level"]):
                correction = capture_probe(
                    ch,
                    led_level=corrected_level,
                    shutter_speed=str(verify["shutter_speed"]),
                    target_signal=target_signal,
                    phase="correct",
                )
                attempts.append(correction)
                post_verify_attempt = correction

            if (
                shutter_control_enabled
                and clipped_verify_shutter is None
                and is_clean_attempt(post_verify_attempt)
                and int(post_verify_attempt["led_level"]) >= _PWM_MAX
                and float(post_verify_attempt["p99"]) < float(target_signal) - float(metric_tolerance)
            ):
                current_shutter = str(post_verify_attempt["shutter_speed"])
                try:
                    current_seconds = _shutter_seconds(current_shutter)
                    current_signal = max(float(post_verify_attempt["p99"]), _MIN_SIGNAL_P99)
                    required_seconds = current_seconds * (float(target_signal) / current_signal)
                    try:
                        idx = candidates.index(current_shutter)
                    except ValueError:
                        idx = -1
                    slower_candidates = candidates[idx + 1 :] if idx >= 0 else candidates
                    slower_shutter = _at_or_slower_shutter(required_seconds, slower_candidates)
                    if slower_shutter is None:
                        slower_shutter = _next_slower_shutter(current_shutter, candidates)
                    if slower_shutter is not None:
                        slower_seconds = _shutter_seconds(slower_shutter)
                        k = current_signal / (
                            float(post_verify_attempt["led_level"]) * current_seconds
                        )
                        slower_level = int(np.clip(
                            round(float(target_signal) / max(k * slower_seconds, 1.0)),
                            _PWM_MIN,
                            _PWM_MAX,
                        ))
                        calibration_log(
                            "calibration_shutter_escalate",
                            channel=ch,
                            source_level=int(post_verify_attempt["led_level"]),
                            source_shutter=current_shutter,
                            source_p999=round(float(post_verify_attempt["p99"]), 2),
                            next_level=slower_level,
                            next_shutter=slower_shutter,
                            target=target_signal,
                        )
                        slower_probe = capture_probe(
                            ch,
                            led_level=slower_level,
                            shutter_speed=slower_shutter,
                            target_signal=target_signal,
                            phase="shutter-escalate",
                        )
                        attempts.append(slower_probe)
                        post_verify_attempt = slower_probe
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

            if (
                shutter_control_enabled
                and clipped_verify_shutter is not None
                and is_clean_attempt(post_verify_attempt)
                and int(post_verify_attempt["led_level"]) >= _PWM_MAX
                and float(post_verify_attempt["p99"]) < float(target_signal) - float(metric_tolerance)
            ):
                bracket_solution = _solve_bracket_refinement(
                    clean_signal=float(post_verify_attempt["p99"]),
                    clean_level=int(post_verify_attempt["led_level"]),
                    clean_shutter=str(post_verify_attempt["shutter_speed"]),
                    clipped_shutter=clipped_verify_shutter,
                    target=target_signal,
                    candidates=candidates,
                )
                if bracket_solution is not None:
                    bracket_level, bracket_shutter, bracket_hint = bracket_solution
                    calibration_log(
                        "calibration_bracket_refine",
                        channel=ch,
                        source_level=int(post_verify_attempt["led_level"]),
                        source_shutter=str(post_verify_attempt["shutter_speed"]),
                        source_p999=round(float(post_verify_attempt["p99"]), 2),
                        clipped_shutter=clipped_verify_shutter,
                        next_level=bracket_level,
                        next_shutter=bracket_shutter,
                        target=target_signal,
                        solve_hint=bracket_hint,
                    )
                    bracket_probe = capture_probe(
                        ch,
                        led_level=bracket_level,
                        shutter_speed=bracket_shutter,
                        target_signal=target_signal,
                        phase="bracket",
                    )
                    attempts.append(bracket_probe)

            def attempt_rank(attempt: dict[str, object]) -> tuple[int, float, float]:
                clip = float(attempt.get("clip_fraction", 0.0))
                signal = float(attempt["p99"])
                clean_rank = 0 if clip <= _MAX_CLIP_FRACTION else 1
                error = abs(np.log(max(signal, _MIN_SIGNAL_P99) / max(float(target_signal), 1.0)))
                # Tethered scanning is not handheld photography: once exposure is
                # clean, signal quality matters more than picking the fastest
                # shutter among near-ties. The shutter ladder is only the coarse
                # knob; LED PWM is the trim.
                return (clean_rank, error, 0.0)

            clean_attempts = [attempt for attempt in attempts if is_clean_attempt(attempt)]
            if clean_attempts:
                best_attempt = min(clean_attempts, key=attempt_rank)
            else:
                best_attempt = min(attempts, key=attempt_rank)

        best_p99 = float(best_attempt["p99"])
        best_clip_fraction = float(best_attempt.get("clip_fraction", 0.0))
        converged_level = int(best_attempt["led_level"])

        if best_p99 < _MIN_SIGNAL_P99:
            restore_original_settings()
            raise ValueError(
                f"channel {ch}: rebate signal is zero after black subtraction at every "
                f"probe level — dark frame (black_level={black_level[ch]:.1f}) is "
                f"brighter than the calibration signal, or black_level is miscalibrated"
            )

        if best_clip_fraction > _MAX_CLIP_FRACTION:
            restore_original_settings()
            raise ValueError(
                f"channel {ch}: calibration histogram is clipped "
                f"({best_clip_fraction:.2%} of rebate pixels/photosites are saturated). "
                "Use a faster shutter, lower ISO, or reduce LED level before scanning."
            )

        if converged_level == _PWM_MAX and best_p99 < target_signal - metric_tolerance:
            _logger.warning(
                "channel %s: calibration reached max LED level (255) at shutter %s but "
                "could not reach the exposure target (best p99.9 = %.0f, target = %d); "
                "clip_fraction will be near 0 — frame will be under-exposed for this channel",
                ch,
                best_attempt["shutter_speed"] or "camera-current",
                best_p99,
                target_signal,
            )

        clip_fraction = float(np.clip(best_clip_fraction, 0.0, 1.0))
        gain = float(target_signal) / max(converged_level, 1)
        saw_clipped_probe = any(
            float(attempt.get("clip_fraction", 0.0)) > _MAX_CLIP_FRACTION
            for attempt in attempts
        )
        saw_source_clipped_probe = any(
            float(attempt.get("sensor_clip_fraction", 0.0)) > _MAX_CLIP_FRACTION
            for attempt in attempts
        )
        saw_output_clipped_probe = any(
            float(attempt.get("output_clip_fraction", 0.0)) > _MAX_CLIP_FRACTION
            for attempt in attempts
        )
        status = _exposure_status(
            p99=best_p99,
            target=target_signal,
            clip_fraction=clip_fraction,
            tolerance=metric_tolerance,
        )
        if saw_clipped_probe and status == "under":
            status = "source_limited" if saw_source_clipped_probe else "clip_limited"

        calibration_log(
            "calibration_channel_complete",
            channel=ch,
            level=converged_level,
            shutter_speed=str(best_attempt["shutter_speed"]),
            p99=round(best_p99, 2),
            metric="p99.9",
            clip_fraction=round(clip_fraction, 6),
            target=target_signal,
            max_clip_fraction=_MAX_CLIP_FRACTION,
            exposure_status=status,
            limit_reason=(
                "source_raw"
                if status == "source_limited"
                else ("demosaic_output" if status == "clip_limited" and saw_output_clipped_probe else "")
            ),
        )

        ch_cals[ch] = ChannelCalibration(
            channel=ch,  # type: ignore[arg-type]
            led_level=converged_level,
            black_level=black_level[ch],
            gain=gain,
            clip_fraction=clip_fraction,
            shutter_speed=str(best_attempt["shutter_speed"]),
            p99=round(best_p99, 2),
            target=float(target_signal),
            exposure_status=status,
        )

    if shutter_control_enabled:
        orch.update_settings(
            level_r=ch_cals["R"].led_level,
            level_g=ch_cals["G"].led_level,
            level_b=ch_cals["B"].led_level,
            shutter_r=ch_cals["R"].shutter_speed,
            shutter_g=ch_cals["G"].shutter_speed,
            shutter_b=ch_cals["B"].shutter_speed,
        )

    measured_base_region = _calibrated_base_region(base_region, ch_cals)

    calibration_log(
        "calibration_complete",
        levels={
            "R": ch_cals["R"].led_level,
            "G": ch_cals["G"].led_level,
            "B": ch_cals["B"].led_level,
        },
        shutters={
            "R": ch_cals["R"].shutter_speed,
            "G": ch_cals["G"].shutter_speed,
            "B": ch_cals["B"].shutter_speed,
        },
        statuses={
            "R": ch_cals["R"].exposure_status,
            "G": ch_cals["G"].exposure_status,
            "B": ch_cals["B"].exposure_status,
        },
        base_rgb={
            "R": round(measured_base_region.base_rgb[0], 2),
            "G": round(measured_base_region.base_rgb[1], 2),
            "B": round(measured_base_region.base_rgb[2], 2),
        },
        target_fraction=round(float(target_fraction), 4),
    )

    # ------------------------------------------------------------------
    # STEP 3: Assemble and return CalibrationResult
    # ------------------------------------------------------------------
    return CalibrationResult(
        r=ch_cals["R"],
        g=ch_cals["G"],
        b=ch_cals["B"],
        base_region=measured_base_region,
        ffc_cal_dir=ffc_cal_dir,
    )
