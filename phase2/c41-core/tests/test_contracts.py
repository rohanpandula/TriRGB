"""Tests for c41_core.contracts — JSON round-trip, schema_version, and validation.

Each contract type gets exactly one round-trip test (NFR-13 requirement).
The CalibrationResult round-trip explicitly checks nested-type reconstruction
(Pitfall 2: from_json must rebuild ChannelCalibration and BaseRegionDescriptor
from dicts, not silently store them as plain dicts).
"""
from __future__ import annotations

import json

import pytest

from c41_core.contracts import (
    BaseRegionDescriptor,
    CalibrationResult,
    ChannelCalibration,
    FlatFieldResult,
    InversionParams,
    JsonContract,
)
import c41_core


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_brd() -> BaseRegionDescriptor:
    return BaseRegionDescriptor(
        x=10, y=20, w=50, h=30,
        base_rgb=(8930.0, 12097.0, 2952.0),
        uniformity_cv=2.3,
        source="auto",
    )


def make_channel_cal(channel: str = "R") -> ChannelCalibration:
    return ChannelCalibration(
        channel=channel,
        led_level=128,
        black_level=250.0,
        gain=1.25,
        clip_fraction=0.002,
    )


def make_cal_result() -> CalibrationResult:
    return CalibrationResult(
        r=make_channel_cal("R"),
        g=make_channel_cal("G"),
        b=make_channel_cal("B"),
        base_region=make_brd(),
        ffc_cal_dir="/tmp/cal",
    )


def make_inversion_params() -> InversionParams:
    return InversionParams(
        base_target=10000.0,
        black_point_r=250.0,
        black_point_g=250.0,
        black_point_b=250.0,
        white_point_r=8930.0,
        white_point_g=12097.0,
        white_point_b=2952.0,
        tone_curve_id="linear",
        tone_curve_params=(1.0, 0.0),
        gamma=1.0,
    )


# ---------------------------------------------------------------------------
# Package-level smoke test
# ---------------------------------------------------------------------------

def test_package_version():
    assert c41_core.__version__ == "0.1.0"


# ---------------------------------------------------------------------------
# Round-trip tests (one per contract type — NFR-13)
# ---------------------------------------------------------------------------

def test_base_region_descriptor_round_trip():
    brd = make_brd()
    restored = BaseRegionDescriptor.from_json(brd.to_json())
    assert restored == brd
    assert restored.schema_version == 1
    assert isinstance(restored.base_rgb, tuple), "base_rgb must be tuple after round-trip"
    assert isinstance(restored, BaseRegionDescriptor)


def test_channel_calibration_round_trip_r():
    cc = make_channel_cal("R")
    restored = ChannelCalibration.from_json(cc.to_json())
    assert restored == cc
    assert isinstance(restored, ChannelCalibration)


def test_channel_calibration_round_trip_g():
    cc = make_channel_cal("G")
    restored = ChannelCalibration.from_json(cc.to_json())
    assert restored == cc


def test_channel_calibration_round_trip_b():
    cc = make_channel_cal("B")
    restored = ChannelCalibration.from_json(cc.to_json())
    assert restored == cc


def test_calibration_result_round_trip():
    """CalibrationResult.from_json must reconstruct nested types, not store dicts."""
    cr = make_cal_result()
    restored = CalibrationResult.from_json(cr.to_json())
    assert restored == cr
    # Pitfall 2: verify nested types are reconstructed correctly
    assert isinstance(restored.r, ChannelCalibration), "r must be ChannelCalibration not dict"
    assert isinstance(restored.g, ChannelCalibration), "g must be ChannelCalibration not dict"
    assert isinstance(restored.b, ChannelCalibration), "b must be ChannelCalibration not dict"
    assert isinstance(restored.base_region, BaseRegionDescriptor), "base_region must be BaseRegionDescriptor not dict"
    assert restored.ffc_cal_dir == "/tmp/cal"


def test_inversion_params_round_trip():
    ip = make_inversion_params()
    restored = InversionParams.from_json(ip.to_json())
    assert restored == ip
    assert isinstance(restored.tone_curve_params, tuple), "tone_curve_params must be tuple after round-trip"


# ---------------------------------------------------------------------------
# schema_version present in all serialized contracts (NFR-13)
# ---------------------------------------------------------------------------

def test_schema_version_present():
    ffr = FlatFieldResult(
        flat_data_path="/tmp/f.npy", n_frames_averaged=1, warmup_s=0.0,
        black_level_r=0.0, black_level_g=0.0, black_level_b=0.0,
        working_brightness=128, uniformity_improvement=1.0,
    )
    for obj in [make_brd(), make_channel_cal(), make_cal_result(), make_inversion_params(), ffr]:
        d = json.loads(obj.to_json())
        assert "schema_version" in d, f"Missing schema_version in {type(obj).__name__}"
        assert d["schema_version"] == 1


# ---------------------------------------------------------------------------
# __post_init__ validation rejects invalid construction
# ---------------------------------------------------------------------------

def test_channel_calibration_rejects_bad_led_level():
    with pytest.raises(ValueError, match="led_level"):
        ChannelCalibration(channel="R", led_level=300, black_level=0.0, gain=1.0, clip_fraction=0.0)


def test_channel_calibration_rejects_bad_channel():
    with pytest.raises(ValueError, match="channel"):
        ChannelCalibration(channel="X", led_level=128, black_level=0.0, gain=1.0, clip_fraction=0.0)


def test_channel_calibration_rejects_zero_gain():
    with pytest.raises(ValueError, match="gain"):
        ChannelCalibration(channel="R", led_level=128, black_level=0.0, gain=0.0, clip_fraction=0.0)


def test_channel_calibration_rejects_clip_fraction_overflow():
    with pytest.raises(ValueError, match="clip_fraction"):
        ChannelCalibration(channel="R", led_level=128, black_level=0.0, gain=1.0, clip_fraction=1.5)


def test_base_region_descriptor_rejects_zero_width():
    with pytest.raises(ValueError, match="w"):
        BaseRegionDescriptor(
            x=0, y=0, w=0, h=10,
            base_rgb=(1.0, 1.0, 1.0), uniformity_cv=1.0, source="auto",
        )


def test_base_region_descriptor_rejects_bad_source():
    with pytest.raises(ValueError, match="source"):
        BaseRegionDescriptor(
            x=0, y=0, w=10, h=10,
            base_rgb=(1.0, 1.0, 1.0), uniformity_cv=1.0, source="green",
        )


def test_inversion_params_rejects_zero_gamma():
    with pytest.raises(ValueError, match="gamma"):
        InversionParams(
            base_target=1000.0,
            black_point_r=0.0, black_point_g=0.0, black_point_b=0.0,
            white_point_r=8000.0, white_point_g=8000.0, white_point_b=8000.0,
            tone_curve_id="linear",
            tone_curve_params=(),
            gamma=0.0,
        )


# ---------------------------------------------------------------------------
# Tuple-coercion: base_rgb and tone_curve_params survive list→tuple round-trip
# ---------------------------------------------------------------------------

def test_base_rgb_is_tuple_after_construction_from_list():
    """Pitfall 1: base_rgb passed as list must be coerced to tuple by __post_init__."""
    brd = BaseRegionDescriptor(
        x=0, y=0, w=10, h=10,
        base_rgb=[8930.0, 12097.0, 2952.0],  # type: ignore[arg-type]  # deliberate list
        uniformity_cv=1.0, source="auto",
    )
    assert isinstance(brd.base_rgb, tuple)


def test_tone_curve_params_is_tuple_after_construction_from_list():
    """Pitfall 1 (InversionParams): tone_curve_params passed as list must be coerced."""
    ip = InversionParams(
        base_target=1000.0,
        black_point_r=0.0, black_point_g=0.0, black_point_b=0.0,
        white_point_r=8000.0, white_point_g=8000.0, white_point_b=8000.0,
        tone_curve_id="linear",
        tone_curve_params=[1.0, 0.0],  # type: ignore[arg-type]  # deliberate list
        gamma=1.0,
    )
    assert isinstance(ip.tone_curve_params, tuple)


# ---------------------------------------------------------------------------
# CR-01: CalibrationResult rejects dict-typed nested fields at construction time
# ---------------------------------------------------------------------------

def test_calibration_result_rejects_dict_for_r_channel():
    """CR-01: CalibrationResult must raise TypeError when r is a plain dict."""
    with pytest.raises(TypeError, match="ChannelCalibration"):
        CalibrationResult(
            r={"channel": "R", "led_level": 0, "black_level": 0.0, "gain": 1.0, "clip_fraction": 0.0},
            g=make_channel_cal("G"),
            b=make_channel_cal("B"),
            base_region=make_brd(),
            ffc_cal_dir="/tmp",
        )


def test_calibration_result_rejects_dict_for_base_region():
    """CR-01: CalibrationResult must raise TypeError when base_region is a plain dict."""
    with pytest.raises(TypeError, match="BaseRegionDescriptor"):
        CalibrationResult(
            r=make_channel_cal("R"),
            g=make_channel_cal("G"),
            b=make_channel_cal("B"),
            base_region={"x": 0, "y": 0, "w": 10, "h": 10, "base_rgb": (1.0, 1.0, 1.0),
                         "uniformity_cv": 1.0, "source": "auto"},
            ffc_cal_dir="/tmp",
        )


def test_calibration_result_from_json_still_round_trips():
    """CR-01: from_json must still reconstruct correctly after __post_init__ is added."""
    cr = make_cal_result()
    restored = CalibrationResult.from_json(cr.to_json())
    assert restored == cr
    assert isinstance(restored.r, ChannelCalibration)
    assert isinstance(restored.base_region, BaseRegionDescriptor)


# ---------------------------------------------------------------------------
# CR-02: InversionParams rejects white_point <= black_point (div-by-zero guard)
# ---------------------------------------------------------------------------

def test_inversion_params_rejects_equal_white_black_point_r():
    """CR-02: white_point_r == black_point_r must raise ValueError."""
    with pytest.raises(ValueError, match="white_point_r"):
        InversionParams(
            base_target=10000.0,
            black_point_r=8000.0, black_point_g=250.0, black_point_b=250.0,
            white_point_r=8000.0, white_point_g=12097.0, white_point_b=2952.0,
            tone_curve_id="linear", tone_curve_params=(), gamma=1.0,
        )


def test_inversion_params_rejects_inverted_points_g():
    """CR-02: white_point_g < black_point_g must raise ValueError."""
    with pytest.raises(ValueError, match="white_point_g"):
        InversionParams(
            base_target=10000.0,
            black_point_r=250.0, black_point_g=12000.0, black_point_b=250.0,
            white_point_r=8930.0, white_point_g=5000.0, white_point_b=2952.0,
            tone_curve_id="linear", tone_curve_params=(), gamma=1.0,
        )


# ---------------------------------------------------------------------------
# WR-01: InversionParams rejects negative black_point and base_target
# ---------------------------------------------------------------------------

def test_inversion_params_rejects_negative_black_point_r():
    """WR-01: negative black_point_r must raise ValueError."""
    with pytest.raises(ValueError, match="black_point_r"):
        InversionParams(
            base_target=10000.0,
            black_point_r=-1.0, black_point_g=250.0, black_point_b=250.0,
            white_point_r=8930.0, white_point_g=12097.0, white_point_b=2952.0,
            tone_curve_id="linear", tone_curve_params=(), gamma=1.0,
        )


def test_inversion_params_rejects_negative_base_target():
    """WR-01: negative base_target must raise ValueError."""
    with pytest.raises(ValueError, match="base_target"):
        InversionParams(
            base_target=-1.0,
            black_point_r=250.0, black_point_g=250.0, black_point_b=250.0,
            white_point_r=8930.0, white_point_g=12097.0, white_point_b=2952.0,
            tone_curve_id="linear", tone_curve_params=(), gamma=1.0,
        )


# ---------------------------------------------------------------------------
# WR-02: from_json schema-evolution behavior — consistent policy test
# ---------------------------------------------------------------------------

def test_calibration_result_from_json_ignores_unknown_top_level_keys():
    """WR-02: CalibrationResult.from_json silently drops unknown top-level keys (forward-compat)."""
    import json as _json
    cr = make_cal_result()
    d = _json.loads(cr.to_json())
    d["future_v2_field"] = "ignored"
    # Must not raise — unknown top-level key is silently dropped
    restored = CalibrationResult.from_json(_json.dumps(d))
    assert restored == cr


def test_channel_calibration_from_json_rejects_unknown_keys():
    """WR-02 (updated): ChannelCalibration.from_json still raises on unknown keys
    when called DIRECTLY (the base mixin does not filter); unknown keys inside
    CalibrationResult.from_json are filtered before passing to ChannelCalibration."""
    import json as _json
    cc = make_channel_cal("R")
    d = _json.loads(cc.to_json())
    d["future_field"] = "boom"
    with pytest.raises(TypeError):
        ChannelCalibration.from_json(_json.dumps(d))


# ---------------------------------------------------------------------------
# FIX 1: NaN/Infinity bypass — math.isfinite guards on all float fields
# ---------------------------------------------------------------------------

import math as _math


def test_base_region_descriptor_rejects_nan_in_base_rgb():
    """FIX 1: NaN in base_rgb must raise ValueError, not slip past the < 0 guard."""
    with pytest.raises(ValueError, match="base_rgb"):
        BaseRegionDescriptor(
            x=0, y=0, w=10, h=10,
            base_rgb=(_math.nan, 1.0, 1.0),
            uniformity_cv=1.0, source="auto",
        )


def test_base_region_descriptor_rejects_inf_in_base_rgb():
    """FIX 1: Infinity in base_rgb must raise ValueError."""
    with pytest.raises(ValueError, match="base_rgb"):
        BaseRegionDescriptor(
            x=0, y=0, w=10, h=10,
            base_rgb=(float("inf"), 1.0, 1.0),
            uniformity_cv=1.0, source="auto",
        )


def test_channel_calibration_rejects_inf_gain():
    """FIX 1: Infinity in gain must raise ValueError."""
    with pytest.raises(ValueError, match="gain"):
        ChannelCalibration(
            channel="R", led_level=128,
            black_level=0.0, gain=float("inf"), clip_fraction=0.0,
        )


def test_channel_calibration_rejects_nan_black_level():
    """FIX 1: NaN in black_level must raise ValueError."""
    with pytest.raises(ValueError, match="black_level"):
        ChannelCalibration(
            channel="R", led_level=128,
            black_level=_math.nan, gain=1.0, clip_fraction=0.0,
        )


def test_inversion_params_rejects_nan_white_point_r():
    """FIX 1: NaN in white_point_r must raise ValueError."""
    with pytest.raises(ValueError, match="white_point_r"):
        InversionParams(
            base_target=10000.0,
            black_point_r=250.0, black_point_g=250.0, black_point_b=250.0,
            white_point_r=_math.nan, white_point_g=12097.0, white_point_b=2952.0,
            tone_curve_id="linear", tone_curve_params=(), gamma=1.0,
        )


def test_inversion_params_rejects_nan_gamma():
    """FIX 1: NaN in gamma must raise ValueError."""
    with pytest.raises(ValueError, match="gamma"):
        InversionParams(
            base_target=10000.0,
            black_point_r=250.0, black_point_g=250.0, black_point_b=250.0,
            white_point_r=8930.0, white_point_g=12097.0, white_point_b=2952.0,
            tone_curve_id="linear", tone_curve_params=(), gamma=_math.nan,
        )


def test_to_json_rejects_non_finite_at_serialize():
    """FIX 1: allow_nan=False in to_json must raise ValueError on non-finite values
    that bypass construction (e.g. injected via object.__setattr__ bypass)."""
    import dataclasses as _dc
    # Construct valid object then forcibly corrupt it to test serialization guard
    brd = BaseRegionDescriptor(
        x=0, y=0, w=10, h=10,
        base_rgb=(1.0, 1.0, 1.0), uniformity_cv=1.0, source="auto",
    )
    # Use object.__setattr__ to bypass frozen + __post_init__ and inject nan
    object.__setattr__(brd, "uniformity_cv", float("nan"))
    with pytest.raises(ValueError):
        brd.to_json()


# ---------------------------------------------------------------------------
# FIX 2: Nested-key forward-compat in CalibrationResult.from_json
# ---------------------------------------------------------------------------

def test_calibration_result_from_json_tolerates_unknown_nested_key_in_r():
    """FIX 2: Unknown key inside the 'r' nested dict must NOT raise TypeError."""
    import json as _json
    cr = make_cal_result()
    d = _json.loads(cr.to_json())
    d["r"]["future_nested_field"] = "ignored"
    # Must not raise — unknown nested key is filtered out
    restored = CalibrationResult.from_json(_json.dumps(d))
    assert isinstance(restored.r, ChannelCalibration)
    assert restored.r == cr.r


def test_calibration_result_from_json_tolerates_unknown_nested_key_in_base_region():
    """FIX 2: Unknown key inside 'base_region' nested dict must NOT raise TypeError."""
    import json as _json
    cr = make_cal_result()
    d = _json.loads(cr.to_json())
    d["base_region"]["future_nested_field"] = "ignored"
    # Must not raise
    restored = CalibrationResult.from_json(_json.dumps(d))
    assert isinstance(restored.base_region, BaseRegionDescriptor)
    assert restored.base_region == cr.base_region


# ---------------------------------------------------------------------------
# FIX 3: Channel/slot consistency in CalibrationResult.__post_init__
# ---------------------------------------------------------------------------

def test_calibration_result_rejects_wrong_channel_in_r_slot():
    """FIX 3: CalibrationResult must raise ValueError when r slot holds channel='G'."""
    with pytest.raises(ValueError, match="r slot"):
        CalibrationResult(
            r=make_channel_cal("G"),  # G-channel in r slot
            g=make_channel_cal("G"),
            b=make_channel_cal("B"),
            base_region=make_brd(),
            ffc_cal_dir="/tmp",
        )


def test_calibration_result_rejects_wrong_channel_in_g_slot():
    """FIX 3: CalibrationResult must raise ValueError when g slot holds channel='B'."""
    with pytest.raises(ValueError, match="g slot"):
        CalibrationResult(
            r=make_channel_cal("R"),
            g=make_channel_cal("B"),  # B-channel in g slot
            b=make_channel_cal("B"),
            base_region=make_brd(),
            ffc_cal_dir="/tmp",
        )


def test_calibration_result_rejects_wrong_channel_in_b_slot():
    """FIX 3: CalibrationResult must raise ValueError when b slot holds channel='R'."""
    with pytest.raises(ValueError, match="b slot"):
        CalibrationResult(
            r=make_channel_cal("R"),
            g=make_channel_cal("G"),
            b=make_channel_cal("R"),  # R-channel in b slot
            base_region=make_brd(),
            ffc_cal_dir="/tmp",
        )


# ---------------------------------------------------------------------------
# FlatFieldResult — round-trip, validation, schema_version (Phase 10 R-26)
# ---------------------------------------------------------------------------

def make_flat_field_result() -> FlatFieldResult:
    """Return a valid FlatFieldResult instance for testing."""
    return FlatFieldResult(
        flat_data_path="/tmp/flat.npy",
        n_frames_averaged=8,
        warmup_s=5.0,
        black_level_r=250.0,
        black_level_g=255.0,
        black_level_b=240.0,
        working_brightness=200,
        uniformity_improvement=2.83,
    )


def test_flat_field_result_round_trip():
    """FlatFieldResult round-trips through JSON using the DEFAULT mixin (no override)."""
    ffr = make_flat_field_result()
    restored = FlatFieldResult.from_json(ffr.to_json())
    assert restored == ffr
    # Confirm the default mixin is in use — no custom from_json on the class
    assert "from_json" not in FlatFieldResult.__dict__, (
        "FlatFieldResult must NOT define a custom from_json — "
        "all fields are primitive, the default mixin works directly"
    )
    # Confirm all fields are plain primitives (not nested dicts) after round-trip
    assert isinstance(restored.flat_data_path, str)
    assert isinstance(restored.n_frames_averaged, int)
    assert isinstance(restored.warmup_s, float)
    assert isinstance(restored.working_brightness, int)
    assert isinstance(restored.uniformity_improvement, float)


def test_flat_field_result_validation():
    """FlatFieldResult.__post_init__ rejects all invalid construction cases."""
    base = dict(
        flat_data_path="/tmp/flat.npy",
        n_frames_averaged=8,
        warmup_s=5.0,
        black_level_r=250.0,
        black_level_g=255.0,
        black_level_b=240.0,
        working_brightness=200,
        uniformity_improvement=2.83,
    )
    # n_frames_averaged < 1
    with pytest.raises(ValueError, match="n_frames_averaged"):
        FlatFieldResult(**{**base, "n_frames_averaged": 0})
    # warmup_s < 0
    with pytest.raises(ValueError, match="warmup_s"):
        FlatFieldResult(**{**base, "warmup_s": -1.0})
    # working_brightness out of range
    with pytest.raises(ValueError, match="working_brightness"):
        FlatFieldResult(**{**base, "working_brightness": 300})
    # non-finite black level — infinity
    with pytest.raises(ValueError, match="black_level_r"):
        FlatFieldResult(**{**base, "black_level_r": float("inf")})
    # non-finite black level — NaN
    with pytest.raises(ValueError, match="black_level_g"):
        FlatFieldResult(**{**base, "black_level_g": float("nan")})
    # negative uniformity_improvement
    with pytest.raises(ValueError, match="uniformity_improvement"):
        FlatFieldResult(**{**base, "uniformity_improvement": -0.1})


def test_flat_field_result_rejects_nan_warmup_s():
    """FIX 4: warmup_s=nan must raise ValueError — nan<0 is False so the
    negative guard alone is insufficient; the isfinite check must run first."""
    import math as _math
    base = dict(
        flat_data_path="/tmp/flat.npy",
        n_frames_averaged=8,
        warmup_s=5.0,
        black_level_r=250.0,
        black_level_g=255.0,
        black_level_b=240.0,
        working_brightness=200,
        uniformity_improvement=2.83,
    )
    with pytest.raises(ValueError, match="warmup_s"):
        FlatFieldResult(**{**base, "warmup_s": _math.nan})
    with pytest.raises(ValueError, match="warmup_s"):
        FlatFieldResult(**{**base, "warmup_s": float("inf")})


def test_flat_field_result_schema_version_present():
    """schema_version defaults to 1 and appears in the JSON output."""
    ffr = make_flat_field_result()
    assert ffr.schema_version == 1
    serialized = ffr.to_json()
    assert "schema_version" in serialized
    import json as _json
    d = _json.loads(serialized)
    assert d["schema_version"] == 1
