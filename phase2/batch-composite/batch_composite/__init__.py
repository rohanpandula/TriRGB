"""batch_composite — walk a roll directory, composite every frame."""
from .batch import (
    discover_frames,
    composite_roll,
    FrameGroup,
    BatchResult,
    SkipReason,
)

__all__ = [
    "discover_frames",
    "composite_roll",
    "FrameGroup",
    "BatchResult",
    "SkipReason",
]
__version__ = "0.1.0"
