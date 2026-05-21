"""triplet_capture — per-frame R/G/B capture orchestrator for Phase 2."""
from .orchestrator import (
    Orchestrator,
    CaptureSettings,
    TripletResult,
    TripletAbort,
)
from .capture_flats import capture_flats
from .calibrate_exposure import calibrate_exposure

__all__ = [
    "Orchestrator",
    "CaptureSettings",
    "TripletResult",
    "TripletAbort",
    "capture_flats",
    "calibrate_exposure",
]
__version__ = "0.1.0"
