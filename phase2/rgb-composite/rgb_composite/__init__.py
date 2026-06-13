"""rgb_composite — narrowband-RGB triplet → 16-bit linear ProPhoto-RGB."""
from .composite import (
    DEMOSAIC_KWARGS,
    DEFAULT_AUTO_BASE_CV_THRESHOLD,
    DensityBounds,
    DimensionMismatchError,
    OUTPUT_FORMATS,
    DEFAULT_POSITIVE_ANALYSIS_CROP_FRACTION,
    DEFAULT_POSITIVE_BLACK_PERCENTILE,
    DEFAULT_POSITIVE_TONE_GAMMA,
    DEFAULT_POSITIVE_WHITE_PERCENTILE,
    DEFAULT_WHOLE_FRAME_BASE_PERCENTILE,
    aggregate_positive_density_bounds,
    auto_positive_from_composite,
    composite_triplet,
    demosaic_linear,
    hd_sigmoid_tone,
    invert_composite,
    positive_density_bounds_from_composite,
)
from .dng import (
    build_dng_extratags,
    read_linear_dng_tags,
    write_linear_dng,
)
from .ffc import (
    CalibrationError,
    FFCMaps,
    apply_ffc_radiometric,
    apply_ffc_to_channel,
    clear_cache as clear_ffc_cache,
    compute_ffc_map,
    load_ffc_maps,
)
from .rebate_detect import detect_rebate, manual_picker
# checks (Phase 13 R-28)
from .checks import check_base_neutrality, check_frame_anomaly, check_registration
from c41_core.contracts import CheckResult

__all__ = [
    # composite
    "composite_triplet",
    "aggregate_positive_density_bounds",
    "auto_positive_from_composite",
    "demosaic_linear",
    "hd_sigmoid_tone",
    "invert_composite",
    "positive_density_bounds_from_composite",
    "DEMOSAIC_KWARGS",
    "DEFAULT_AUTO_BASE_CV_THRESHOLD",
    "DEFAULT_POSITIVE_ANALYSIS_CROP_FRACTION",
    "DEFAULT_POSITIVE_BLACK_PERCENTILE",
    "DEFAULT_POSITIVE_TONE_GAMMA",
    "DEFAULT_POSITIVE_WHITE_PERCENTILE",
    "DEFAULT_WHOLE_FRAME_BASE_PERCENTILE",
    "DensityBounds",
    "DimensionMismatchError",
    "OUTPUT_FORMATS",
    # dng
    "write_linear_dng",
    "read_linear_dng_tags",
    "build_dng_extratags",
    # ffc
    "load_ffc_maps",
    "compute_ffc_map",
    "apply_ffc_radiometric",
    "apply_ffc_to_channel",
    "clear_ffc_cache",
    "FFCMaps",
    "CalibrationError",
    # rebate_detect (Phase 09)
    "detect_rebate",
    "manual_picker",
    # checks (Phase 13)
    "check_registration",
    "check_base_neutrality",
    "check_frame_anomaly",
    "CheckResult",
]
__version__ = "0.2.0"
