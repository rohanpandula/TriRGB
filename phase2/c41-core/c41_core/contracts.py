"""Versioned data contracts for the v1.2 narrowband C-41 inversion pipeline.

Why this module exists
----------------------
Milestone v1.2 introduces five concurrent Wave-2 phases (09–13) that must each
define their own test data and production logic while sharing stable interface
shapes.  Without explicit contracts, every phase would invent its own dict/tuple
representation of "a calibration result" or "inversion parameters" — leading to
silent incompatibilities discovered only at integration time.

This module defines the four canonical types that act as the stable seams:

  BaseRegionDescriptor  — pixel bbox + measured per-channel base in the rebate
  ChannelCalibration    — per-channel LED/gain/black-level calibration record
  CalibrationResult     — aggregates all three ChannelCalibration + base region
  InversionParams       — numeric knobs the inversion compositor consumes

Each type is a frozen dataclass (zero external dependency, matches FFCMaps
convention in phase2/rgb-composite/rgb_composite/ffc.py) carrying a
schema_version int field that increments when fields are added.  Each
round-trips through JSON via the JsonContract mixin so Phase 14 (SwiftUI)
can consume CalibrationResult across the Python/Swift boundary.

Serialization notes
-------------------
- tuple fields (base_rgb, tone_curve_params) are coerced in __post_init__
  via object.__setattr__ to survive the JSON tuple→list round-trip (Pitfall 1).
- CalibrationResult.from_json overrides the base mixin to reconstruct nested
  ChannelCalibration / BaseRegionDescriptor objects from the dicts that
  json.loads returns (Pitfall 2).  All other types use the mixin default.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Literal

import numpy as np


__version__ = "0.1.0"

__all__ = [
    "JsonContract",
    "BaseRegionDescriptor",
    "ChannelCalibration",
    "CalibrationResult",
    "InversionParams",
]


# ---------------------------------------------------------------------------
# JsonContract mixin
# ---------------------------------------------------------------------------

class JsonContract:
    """Mixin: JSON serialization for frozen dataclasses.

    Provides to_json() and from_json() using dataclasses.asdict + a
    NumpyEncoder that coerces numpy scalar types to Python natives.

    Subclasses with tuple fields MUST coerce those fields in __post_init__
    via object.__setattr__ to survive the JSON list→tuple round-trip.

    Subclasses with nested dataclass fields (CalibrationResult) MUST
    override from_json to reconstruct nested types manually.
    """

    class _NumpyEncoder(json.JSONEncoder):
        def default(self, obj: object) -> object:
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), cls=self._NumpyEncoder)

    @classmethod
    def from_json(cls, s: str) -> "JsonContract":
        return cls(**json.loads(s))


# ---------------------------------------------------------------------------
# BaseRegionDescriptor
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class BaseRegionDescriptor(JsonContract):
    """Pixel bounding box and measured raw per-channel base in the rebate region.

    Fields
    ------
    x, y          : top-left corner of the rebate bbox, pixels (>= 0).
    w, h          : width/height of the rebate bbox, pixels (> 0).
    base_rgb      : (R, G, B) mean raw values measured in the rebate (no-WB).
                    Channel index locked: R=0, G=1, B=2.
                    JSON list is coerced back to tuple in __post_init__.
    uniformity_cv : coefficient of variation (%) of the rebate region, 0–100.
                    Lower = more uniform base.
    source        : "auto" (spatial picker) or "manual" (user-drawn).
    schema_version: bumped when fields are added.  Default 1.
    """

    x: int
    y: int
    w: int
    h: int
    base_rgb: tuple[float, float, float]
    uniformity_cv: float
    source: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0:
            raise ValueError(f"x, y must be >= 0, got x={self.x}, y={self.y}")
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"w, h must be > 0, got w={self.w}, h={self.h}")
        if len(self.base_rgb) != 3 or any(v < 0 for v in self.base_rgb):
            raise ValueError("base_rgb must be a 3-tuple with all values >= 0")
        if not 0.0 <= self.uniformity_cv <= 100.0:
            raise ValueError(f"uniformity_cv must be 0–100, got {self.uniformity_cv}")
        if self.source not in ("auto", "manual"):
            raise ValueError(f"source must be 'auto' or 'manual', got {self.source!r}")
        # Pitfall 1: coerce from JSON list to tuple (object.__setattr__ escape hatch)
        object.__setattr__(self, "base_rgb", tuple(float(v) for v in self.base_rgb))


# ---------------------------------------------------------------------------
# ChannelCalibration
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ChannelCalibration(JsonContract):
    """Per-channel LED/gain/black-level calibration record.

    Fields
    ------
    channel      : "R", "G", or "B" — which LED channel this calibrates.
    led_level    : integer brightness level sent to Scanlight, 0–255
                   (matches scanlightctl protocol.py channel range).
    black_level  : per-channel black offset (raw counts) to subtract
                   before applying FFC.  >= 0.
    gain         : calibrated exposure gain.  > 0.
    clip_fraction: fraction of pixels at or above clipping threshold in
                   the rebate, 0.0–1.0.
    schema_version: default 1.
    """

    channel: Literal["R", "G", "B"]
    led_level: int
    black_level: float
    gain: float
    clip_fraction: float
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.channel not in ("R", "G", "B"):
            raise ValueError(f"channel must be 'R', 'G', or 'B', got {self.channel!r}")
        if not 0 <= self.led_level <= 255:
            raise ValueError(f"led_level must be 0–255, got {self.led_level}")
        if self.black_level < 0:
            raise ValueError(f"black_level must be >= 0, got {self.black_level}")
        if self.gain <= 0:
            raise ValueError(f"gain must be > 0, got {self.gain}")
        if not 0.0 <= self.clip_fraction <= 1.0:
            raise ValueError(f"clip_fraction must be 0–1, got {self.clip_fraction}")


# ---------------------------------------------------------------------------
# CalibrationResult
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class CalibrationResult(JsonContract):
    """Aggregated per-channel calibration and rebate-region descriptor.

    This is the primary record that crosses the Python/Swift boundary in
    Phase 14: CalibrationResult.to_json() produces the JSON that the
    SwiftUI wizard deserialises via a matching Codable struct.

    ffc_cal_dir is a path reference only — FFC maps are NOT embedded as
    arrays (that would make the JSON multi-MB).  The existing
    load_ffc_maps(cal_dir) by-path pattern in ffc.py is preserved.

    OVERRIDE NOTE: from_json is overridden here because dataclasses.asdict
    recurses into nested dataclasses, and json.loads returns plain dicts.
    The default mixin from_json would store dicts for r/g/b/base_region —
    this override reconstructs ChannelCalibration and BaseRegionDescriptor
    explicitly (Pitfall 2).
    """

    r: ChannelCalibration
    g: ChannelCalibration
    b: ChannelCalibration
    base_region: BaseRegionDescriptor
    ffc_cal_dir: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.r, ChannelCalibration):
            raise TypeError(f"r must be ChannelCalibration, got {type(self.r).__name__}")
        if not isinstance(self.g, ChannelCalibration):
            raise TypeError(f"g must be ChannelCalibration, got {type(self.g).__name__}")
        if not isinstance(self.b, ChannelCalibration):
            raise TypeError(f"b must be ChannelCalibration, got {type(self.b).__name__}")
        if not isinstance(self.base_region, BaseRegionDescriptor):
            raise TypeError(f"base_region must be BaseRegionDescriptor, got {type(self.base_region).__name__}")

    @classmethod
    def from_json(cls, s: str) -> "CalibrationResult":
        """Reconstruct CalibrationResult from JSON string.

        NOTE: Unknown top-level keys in the JSON are silently dropped (forward-compat).
        Unknown keys inside nested ChannelCalibration / BaseRegionDescriptor dicts
        will raise TypeError (strict). This asymmetry is intentional — top-level
        new fields from v2+ schemas are ignored, but nested-field additions require a
        coordinated schema bump.
        """
        d = json.loads(s)
        return cls(
            r=ChannelCalibration(**d["r"]),
            g=ChannelCalibration(**d["g"]),
            b=ChannelCalibration(**d["b"]),
            base_region=BaseRegionDescriptor(**d["base_region"]),
            ffc_cal_dir=d["ffc_cal_dir"],
            schema_version=d.get("schema_version", 1),
        )


# ---------------------------------------------------------------------------
# InversionParams
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class InversionParams(JsonContract):
    """Numeric knobs for the inversion compositor (Phase 11).

    Consumed by invert_composite(triplet, descriptor, params) to perform:
      1. Base neutralization: force base R=G=B at base_target level.
      2. Per-channel black/white point mapping.
      3. Tone curve application (id + params).
      4. Gamma output encoding.

    tone_curve_params is a variable-length float tuple (empty for "linear").
    JSON round-trip coerces the list back to tuple in __post_init__
    (Pitfall 1, same as base_rgb).

    Fields derived from Phase 11 success criteria.  Phase 11 may add
    fields with a schema_version bump.
    """

    base_target: float
    black_point_r: float
    black_point_g: float
    black_point_b: float
    white_point_r: float
    white_point_g: float
    white_point_b: float
    tone_curve_id: str
    tone_curve_params: tuple[float, ...]
    gamma: float
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.gamma <= 0:
            raise ValueError(f"gamma must be > 0, got {self.gamma}")
        # WR-01: raw pixel counts are non-negative — reject negative black points and base_target
        for name, val in (
            ("base_target", self.base_target),
            ("black_point_r", self.black_point_r),
            ("black_point_g", self.black_point_g),
            ("black_point_b", self.black_point_b),
        ):
            if val < 0:
                raise ValueError(f"{name} must be >= 0, got {val}")
        # CR-02: white_point must exceed black_point per channel to prevent
        # division-by-zero in the inversion formula (pixel-black)/(white-black)
        for ch, bp, wp in (
            ("r", self.black_point_r, self.white_point_r),
            ("g", self.black_point_g, self.white_point_g),
            ("b", self.black_point_b, self.white_point_b),
        ):
            if wp <= bp:
                raise ValueError(
                    f"white_point_{ch} ({wp}) must be > black_point_{ch} ({bp})"
                )
        # Pitfall 1: coerce from JSON list to tuple
        object.__setattr__(
            self, "tone_curve_params",
            tuple(float(v) for v in self.tone_curve_params),
        )
