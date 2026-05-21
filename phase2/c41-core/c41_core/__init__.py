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

# Fixtures are imported after __all__ to silence the E402 lint rule
from .fixtures import make_c41_negative, make_rebate_strip, DEFAULT_SEED  # noqa: E402
