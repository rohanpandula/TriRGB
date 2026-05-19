"""Shared pytest fixtures for the integration test suite.

Why these fixtures exist
------------------------
The integration tests exercise the real `rgb_composite.composite_triplet` and
`batch_composite.composite_roll` end-to-end, with only the hardware-dependent
`demosaic_linear` call monkeypatched.  This keeps the test suite runnable
without any ARW files in the repository while still exercising every real code
path: channel selection, FFC application, TIFF/DNG write, batch discovery, and
the file-naming contract between the orchestrator and batch-composite.

Why monkeypatch demosaic_linear instead of shipping real ARW fixtures
----------------------------------------------------------------------
- No binary fixtures in-repo.  A real a7CR ARW is 60-80 MB per file; a 36-frame
  roll with three channels would add ~7 GB to the repository — unworkable.
- The seam this suite tests is *channel selection*, not bytes-on-disk decoding.
  rawpy is already covered by the HW-gated tests in phase2/rgb-composite (the
  `requires_arw` mark).  What the integration tests exercise is whether channel
  0 from the R-lit demosaic actually ends up in channel 0 of the composite —
  that contract is fully observable with synthetic numpy arrays.
- Monkeypatching `rgb_composite.composite.demosaic_linear` (the module-local
  symbol, not the package-level re-export) is the established pattern in this
  codebase; see `phase2/rgb-composite/tests/test_composite.py::demosaics_stub`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import rgb_composite.composite as composite_mod
from rgb_composite import clear_ffc_cache


# ---------------------------------------------------------------------------
# Constants (exported so test modules can import them)
# ---------------------------------------------------------------------------

H = 128       # frame height, pixels
W = 192       # frame width, pixels
DOMINANT = 50000   # value for the dominant channel in a synthetic demosaic
CROSSTALK = 2500   # value for the non-dominant channels (sensor crosstalk)


# ---------------------------------------------------------------------------
# Module-level helper (not a fixture)
# ---------------------------------------------------------------------------

def _make_channel_dominant_array(channel: str) -> np.ndarray:
    """Return an H x W x 3 uint16 array where `channel` is dominant.

    Args:
        channel: one of "R", "G", or "B".

    Returns:
        uint16 ndarray shape (H, W, 3).  The named channel is filled with
        DOMINANT; the other two are filled with CROSSTALK.

    Raises:
        ValueError: if channel is not "R", "G", or "B".
    """
    img = np.full((H, W, 3), CROSSTALK, dtype=np.uint16)
    ch_index = {"R": 0, "G": 1, "B": 2}
    if channel not in ch_index:
        raise ValueError(f"Unknown channel {channel!r}; expected one of R, G, B")
    img[..., ch_index[channel]] = DOMINANT
    return img


# ---------------------------------------------------------------------------
# Fixture 1: synthetic_demosaic
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_demosaic(monkeypatch):
    """Monkeypatch `rgb_composite.composite.demosaic_linear` with a stub.

    The stub returns a channel-dominant uint16 array based on the filename:
    - filename contains `_r.` OR equals `r.arw`  → R dominant
    - filename contains `_g.` OR equals `g.arw`  → G dominant
    - filename contains `_b.` OR equals `b.arw`  → B dominant
    - capital cal-frame names R.ARW / G.ARW / B.ARW → matching channel dominant

    Returns:
        A `calls` list.  Every path passed to `_fake_demosaic` is appended so
        tests can verify the stub was called the expected number of times.

    Usage::

        def test_something(synthetic_demosaic, roll_directory_factory, tmp_path):
            roll_dir = roll_directory_factory(n_frames=1)
            composite_roll(roll_dir, workers=1)
            assert len(synthetic_demosaic) == 3  # one triplet → 3 calls
    """
    calls: list[Path] = []

    def _fake_demosaic(path):
        path = Path(path)
        calls.append(path)
        name = path.name.lower()
        # Cal-frame and roll-frame dispatch.
        # _r. / _g. / _b. covers roll frames like RollIntTest_Frame001_R.ARW
        # r.arw / g.arw / b.arw covers cal frames like R.ARW (lowercased)
        if "_r." in name or name == "r.arw":
            return _make_channel_dominant_array("R")
        elif "_g." in name or name == "g.arw":
            return _make_channel_dominant_array("G")
        elif "_b." in name or name == "b.arw":
            return _make_channel_dominant_array("B")
        else:
            raise FileNotFoundError(f"synthetic_demosaic has no rule for {path}")

    monkeypatch.setattr(composite_mod, "demosaic_linear", _fake_demosaic)
    return calls


# ---------------------------------------------------------------------------
# Fixture 2: cal_triplet_factory
# ---------------------------------------------------------------------------

@pytest.fixture
def cal_triplet_factory(tmp_path):
    """Factory that builds a flat-field calibration directory.

    Returns a callable `make(name="calibration") -> Path` that creates a
    directory under `tmp_path` containing three 1-byte placeholder files:
    `R.ARW`, `G.ARW`, `B.ARW`.  The `synthetic_demosaic` rules match these
    filenames so the FFC code path receives channel-dominant arrays.

    Usage::

        def test_ffc(synthetic_demosaic, cal_triplet_factory):
            cal = cal_triplet_factory()  # returns tmp_path/calibration
            result = composite_roll(roll_dir, workers=1, ffc_calibration_dir=cal)
    """
    def make(name: str = "calibration") -> Path:
        cal_dir = tmp_path / name
        cal_dir.mkdir(parents=True, exist_ok=True)
        (cal_dir / "R.ARW").write_bytes(b"\x00")
        (cal_dir / "G.ARW").write_bytes(b"\x00")
        (cal_dir / "B.ARW").write_bytes(b"\x00")
        return cal_dir

    return make


# ---------------------------------------------------------------------------
# Fixture 3: roll_directory_factory
# ---------------------------------------------------------------------------

@pytest.fixture
def roll_directory_factory(tmp_path):
    """Factory that builds a roll directory with synthetic ARW triplets.

    Returns a callable `make(roll_name, n_frames, start_frame) -> Path` that
    creates `tmp_path / roll_name` and writes `n_frames` triplets named
    `{roll_name}_Frame{NNN}_{R|G|B}.ARW` for NNN = start_frame..start_frame+n-1.
    Each file contains one null byte.

    Files produced match `batch_composite.batch.FRAME_PATTERN` exactly
    (`{roll}_Frame{NNN}_{R|G|B}.ARW`), so `discover_frames` and
    `composite_roll` consume them without modification.

    Roll names use the prefix `RollIntTest` by default — grep-distinct from
    production roll names so accidental fixture-vs-production collisions are
    immediately obvious in log scans.

    Usage::

        def test_roll(synthetic_demosaic, roll_directory_factory):
            roll_dir = roll_directory_factory(roll_name="RollIntTest", n_frames=36)
            result = composite_roll(roll_dir, workers=1)
            assert len(result.composited) == 36
    """
    def make(
        roll_name: str = "RollIntTest",
        n_frames: int = 1,
        start_frame: int = 1,
    ) -> Path:
        roll_dir = tmp_path / roll_name
        roll_dir.mkdir(parents=True, exist_ok=True)
        for i in range(n_frames):
            frame_num = start_frame + i
            for ch in ("R", "G", "B"):
                fname = f"{roll_name}_Frame{frame_num:03d}_{ch}.ARW"
                (roll_dir / fname).write_bytes(b"\x00")
        return roll_dir

    return make


# ---------------------------------------------------------------------------
# Autouse fixture: clear FFC lru_cache before each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_ffc_cache_before_each():
    """Clear the FFC lru_cache before and after every test.

    `rgb_composite.ffc.load_ffc_maps` is decorated with `@lru_cache`.  Two
    tests that use the same `tmp_path`-rooted cal directory path (unlikely
    but possible when `tmp_path` reuses directory names across a session)
    would otherwise share a stale FFC map from the previous test.  Clearing
    before *and* after ensures both the test's setup and any subsequent test
    start clean.
    """
    clear_ffc_cache()
    yield
    clear_ffc_cache()
