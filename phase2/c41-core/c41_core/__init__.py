"""c41_core — versioned data contracts and synthetic C-41 fixtures."""
from .contracts import (
    JsonContract,
    BaseRegionDescriptor,
    ChannelCalibration,
    CalibrationResult,
    InversionParams,
)

__version__ = "0.1.0"

__all__ = [
    # contracts
    "JsonContract",
    "BaseRegionDescriptor",
    "ChannelCalibration",
    "CalibrationResult",
    "InversionParams",
    # fixtures
    "make_c41_negative",
    "make_rebate_strip",
    "DEFAULT_SEED",
]

# noqa: E402 suppresses the "module level import not at top of file" lint warning;
# fixtures is imported here (after contracts) so the public API is grouped cleanly.
from .fixtures import make_c41_negative, make_rebate_strip, DEFAULT_SEED  # noqa: E402
