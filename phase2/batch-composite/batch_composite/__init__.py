"""batch_composite — walk a roll directory, composite every frame."""
from .batch import (
    discover_frames,
    composite_roll,
    FrameGroup,
    BatchResult,
    RollPositiveResult,
    SkipReason,
    render_roll_positives_from_composites,
)

__all__ = [
    "discover_frames",
    "composite_roll",
    "FrameGroup",
    "BatchResult",
    "RollPositiveResult",
    "SkipReason",
    "render_roll_positives_from_composites",
]
__version__ = "0.1.0"
