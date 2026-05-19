"""scanlight — driver and CLI for the Scanlight v4 narrowband-RGB light source."""
from .device import Scanlight, discover_port
from . import protocol

__all__ = ["Scanlight", "discover_port", "protocol"]
__version__ = "0.1.0"
