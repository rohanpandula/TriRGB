"""rgb_composite.icc — minimal, self-contained ICC profile builder.

The pipeline produces image data in ProPhoto-RGB (Romm RGB) primaries, D50
white. Until now those bytes were written with no embedded color profile, so
viewers (Preview, browsers, ``NSImage``) fell back to assuming sRGB — wrong
primaries, wrong gamut. This module builds a small, valid ICC v2 matrix/TRC
display profile straight from the primaries we already declare in ``dng.py``,
so every TIFF/PNG we write can carry the correct color space.

We hand-build the ICC bytes (rather than shipping a binary ``.icc`` asset or
pulling in a new dependency) for the same reason ``dng.py`` hand-builds its
DNG tags: one auditable source of truth, derived from the same primary matrix.

Two profiles are exposed:

* ``PROPHOTO_LINEAR_ICC`` — gamma 1.0. For the *linear* archival data
  (the channel-isolated composite that feeds Lightroom/NLP). Exactly correct.
* ``PROPHOTO_G22_ICC`` — ProPhoto primaries with a ~2.2 display gamma. For the
  *rendered positive* and its on-screen preview, which carry a baked display
  tone curve. 2.2 is chosen (not the standard ROMM 1.8) because the untagged
  preview was already being shown as sRGB (~2.2), so this corrects ONLY the
  wrong primaries and leaves tone essentially unchanged — the literal "fix the
  assumed-sRGB bug" intent. Preview and export use the *same* profile, so what
  you see on screen matches the exported file (WYSIWYG).

Reference: ICC.1:2001-04 (v2) — header (§6.1), tag table (§6.2), and the
``XYZType`` / ``curveType`` / ``textType`` / ``textDescriptionType`` element
encodings (§6.5, §10).
"""
from __future__ import annotations

import struct
from functools import lru_cache

import numpy as np

from .dng import _PROPHOTO_TO_XYZ_D50

# ICC PCS illuminant is fixed at D50 by the spec. These are the canonical
# s15Fixed16-friendly values (0.9642, 1.0000, 0.8249).
_D50_XYZ = (0.96420, 1.00000, 0.82491)

# Profile creation date is hardcoded for reproducible, byte-stable output
# (two runs produce identical profiles → identical file hashes in tests).
_DATE = (2026, 1, 1, 0, 0, 0)


def _s15f16(x: float) -> bytes:
    """Encode a float as ICC s15Fixed16Number (big-endian signed int32)."""
    return struct.pack(">i", int(round(x * 65536.0)))


def _pad4(blob: bytes) -> bytes:
    """Zero-pad to a 4-byte boundary (ICC requires 4-aligned tag data)."""
    pad = (-len(blob)) % 4
    return blob + b"\x00" * pad


def _xyz_type(x: float, y: float, z: float) -> bytes:
    """XYZType element holding a single XYZNumber."""
    return b"XYZ \x00\x00\x00\x00" + _s15f16(x) + _s15f16(y) + _s15f16(z)


def _curv_type(gamma: float | None) -> bytes:
    """curveType element. ``gamma is None`` (or 1.0) → identity (count 0).

    Otherwise a single u8Fixed8Number gamma (count 1).
    """
    if gamma is None or gamma == 1.0:
        return b"curv\x00\x00\x00\x00" + struct.pack(">I", 0)
    encoded = int(round(gamma * 256.0))
    return b"curv\x00\x00\x00\x00" + struct.pack(">I", 1) + struct.pack(">H", encoded)


def _text_type(text: str) -> bytes:
    """textType element (used for the copyright tag)."""
    return b"text\x00\x00\x00\x00" + text.encode("ascii") + b"\x00"


def _desc_type(text: str) -> bytes:
    """textDescriptionType (v2 'desc'): ASCII + empty Unicode + empty Mac."""
    ascii_bytes = text.encode("ascii") + b"\x00"
    return (
        b"desc\x00\x00\x00\x00"
        + struct.pack(">I", len(ascii_bytes))
        + ascii_bytes
        + struct.pack(">I", 0)  # Unicode language code
        + struct.pack(">I", 0)  # Unicode count (no Unicode string)
        + struct.pack(">H", 0)  # Macintosh script code
        + struct.pack(">B", 0)  # Macintosh count
        + b"\x00" * 67  # fixed-size Macintosh description buffer
    )


def _build_header(size: int) -> bytes:
    """128-byte ICC profile header for an RGB→XYZ display ('mntr') profile."""
    h = bytearray(128)
    struct.pack_into(">I", h, 0, size)  # profile size
    # 4: preferred CMM — 0 (none)
    struct.pack_into(">I", h, 8, 0x02400000)  # version 2.4.0
    h[12:16] = b"mntr"  # device class: display
    h[16:20] = b"RGB "  # data color space
    h[20:24] = b"XYZ "  # PCS
    struct.pack_into(">6H", h, 24, *_DATE)  # creation date/time
    h[36:40] = b"acsp"  # profile file signature
    # 40 platform, 44 flags, 48 mfg, 52 model, 56 attributes (8B): all 0
    struct.pack_into(">I", h, 64, 0)  # rendering intent: perceptual
    h[68:80] = _s15f16(_D50_XYZ[0]) + _s15f16(_D50_XYZ[1]) + _s15f16(_D50_XYZ[2])
    # 84 creator, 100-127 reserved: all 0
    return bytes(h)


@lru_cache(maxsize=4)
def build_prophoto_icc(
    gamma: float | None = None,
    description: str = "Scanlight ProPhoto-RGB (D50)",
) -> bytes:
    """Build an ICC v2 profile for ProPhoto-RGB primaries at the given gamma.

    Args:
        gamma: TRC gamma. ``None`` or ``1.0`` → linear. ``1.8`` → ROMM RGB.
        description: profileDescriptionTag text.

    Returns:
        The complete ICC profile as bytes, ready for ``iccprofile=`` (tifffile)
        or ``icc_profile=`` (PIL).
    """
    # Colorants: columns of the ProPhoto→XYZ(D50) matrix are the D50-adapted
    # XYZ of the red/green/blue primaries — exactly the rXYZ/gXYZ/bXYZ tags.
    m = np.asarray(_PROPHOTO_TO_XYZ_D50, dtype=np.float64)
    red = _xyz_type(m[0, 0], m[1, 0], m[2, 0])
    green = _xyz_type(m[0, 1], m[1, 1], m[2, 1])
    blue = _xyz_type(m[0, 2], m[1, 2], m[2, 2])
    trc = _curv_type(gamma)

    # Required tags for a matrix/TRC display profile (ICC §6.3.1.1).
    tags = [
        (b"desc", _desc_type(description)),
        (b"wtpt", _xyz_type(*_D50_XYZ)),
        (b"rXYZ", red),
        (b"gXYZ", green),
        (b"bXYZ", blue),
        (b"rTRC", trc),
        (b"gTRC", trc),
        (b"bTRC", trc),
        (b"cprt", _text_type("Public Domain")),
    ]

    count = len(tags)
    data_start = 128 + 4 + 12 * count
    data_start += (-data_start) % 4

    table = bytearray(struct.pack(">I", count))
    data = bytearray()
    offset = data_start
    for sig, blob in tags:
        table += sig + struct.pack(">II", offset, len(blob))
        padded = _pad4(blob)
        data += padded
        offset += len(padded)

    total = data_start + len(data)
    body = _build_header(total) + bytes(table)
    body = body + b"\x00" * (data_start - len(body)) + bytes(data)
    return body


# Prebuilt profiles for the two encodings the pipeline writes.
PROPHOTO_LINEAR_ICC: bytes = build_prophoto_icc(
    None, "Scanlight Linear ProPhoto-RGB (D50)"
)
PROPHOTO_G22_ICC: bytes = build_prophoto_icc(
    2.2, "Scanlight ProPhoto-RGB primaries, 2.2 display (D50)"
)
