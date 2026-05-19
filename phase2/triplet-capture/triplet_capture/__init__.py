"""triplet_capture — per-frame R/G/B capture orchestrator for Phase 2."""
from .orchestrator import (
    Orchestrator,
    CaptureSettings,
    TripletResult,
    TripletAbort,
)

__all__ = [
    "Orchestrator",
    "CaptureSettings",
    "TripletResult",
    "TripletAbort",
]
__version__ = "0.1.0"
