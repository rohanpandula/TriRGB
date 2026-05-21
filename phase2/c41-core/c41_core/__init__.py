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
    # fixtures (added in Task 2)
    "make_c41_negative",
    "make_rebate_strip",
    "DEFAULT_SEED",
]

# Fixtures imported after __all__ is defined (populated in Task 2)
from .fixtures import make_c41_negative, make_rebate_strip, DEFAULT_SEED  # noqa: E402
