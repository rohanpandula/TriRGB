"""rgb_composite — narrowband-RGB triplet → 16-bit linear ProPhoto-RGB."""
from .composite import (
    DEMOSAIC_KWARGS,
    DimensionMismatchError,
    OUTPUT_FORMATS,
    composite_triplet,
    demosaic_linear,
)
from .dng import (
    build_dng_extratags,
    read_linear_dng_tags,
    write_linear_dng,
)
from .ffc import (
    CalibrationError,
    FFCMaps,
    apply_ffc_to_channel,
    clear_cache as clear_ffc_cache,
    compute_ffc_map,
    load_ffc_maps,
)

__all__ = [
    # composite
    "composite_triplet",
    "demosaic_linear",
    "DEMOSAIC_KWARGS",
    "DimensionMismatchError",
    "OUTPUT_FORMATS",
    # dng
    "write_linear_dng",
    "read_linear_dng_tags",
    "build_dng_extratags",
    # ffc
    "load_ffc_maps",
    "compute_ffc_map",
    "apply_ffc_to_channel",
    "clear_ffc_cache",
    "FFCMaps",
    "CalibrationError",
]
__version__ = "0.2.0"
