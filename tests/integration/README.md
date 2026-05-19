# Integration Tests

Catches contract drift at the seams between packages — where each package's unit
tests stub the adjacent layer and cannot see a breaking change on the other side.
The three suites here exercise the real `rgb_composite.composite_triplet` and the
real `batch_composite.composite_roll`, with only the hardware-dependent RAW decoder
(`demosaic_linear`) monkeypatched.  The orchestrator file-naming test exercises the
real `triplet_capture.Orchestrator` HW-mode path with a stubbed scanlight and inbox.

## What's here

| File | Tests | Purpose |
|---|---|---|
| `conftest.py` | — | Shared fixtures (3 factories + 1 autouse cache-cleaner) |
| `test_triplet_to_composite.py` | 6 | Channel-selection, FFC, DNG tags, empty-roll guard |
| `test_orchestrator_to_composite_handoff.py` | 2 | Filename contract: orchestrator → `FRAME_PATTERN` |

## Why monkeypatch demosaic_linear

- **No binary fixtures in-repo.** A real a7CR ARW is 60-80 MB; a 36-frame roll (3
  channels each) would add ~7 GB to the repository.
- **The seam under test is channel selection, not bytes-on-disk decoding.**  rawpy
  decoding is already exercised by the `requires_arw`-marked tests in
  `phase2/rgb-composite/tests/test_real_arw.py` (hardware-gated).
- **Monkeypatching is the established pattern here.**  `phase2/rgb-composite/tests/
  test_composite.py::demosaics_stub` (line 35) does exactly this; the integration
  fixtures extend that pattern to batch-level and cross-package calls.
- **Only the module-local symbol is patched.**  `composite_triplet` calls
  `rgb_composite.composite.demosaic_linear` (the module attribute), not the
  package-level re-export snapshot in `rgb_composite.__init__`.  Patching the
  module attribute is what makes monkeypatching work; the re-export is a snapshot
  taken at import time and is not affected.  See `conftest.py` line 32 for the
  `import rgb_composite.composite as composite_mod` / `monkeypatch.setattr` pattern.

## Fixtures

### `synthetic_demosaic`

```python
@pytest.fixture
def synthetic_demosaic(monkeypatch) -> list[Path]:
    ...
```

Patches `rgb_composite.composite.demosaic_linear` to return channel-dominant
uint16 arrays (H=128, W=192) keyed by filename:
- `_R.ARW` or `R.ARW` → channel 0 = DOMINANT (50000), channels 1+2 = CROSSTALK (2500)
- `_G.ARW` or `G.ARW` → channel 1 dominant
- `_B.ARW` or `B.ARW` → channel 2 dominant

Returns a `calls` list so tests can assert the stub was invoked the expected number
of times (e.g., `assert len(synthetic_demosaic) == 3 * n_frames`).

```python
def test_channel_selection(synthetic_demosaic, roll_directory_factory, tmp_path):
    roll_dir = roll_directory_factory(n_frames=1)
    composite_roll(roll_dir, workers=1)
    assert len(synthetic_demosaic) == 3  # one triplet, 3 channels
```

### `cal_triplet_factory`

```python
@pytest.fixture
def cal_triplet_factory(tmp_path) -> Callable[[str], Path]:
    ...
```

Returns a `make(name="calibration") -> Path` callable that writes a flat-field
calibration directory under `tmp_path`.  The directory contains `R.ARW`, `G.ARW`,
`B.ARW` (1-byte placeholder files).  `synthetic_demosaic` maps these to
channel-dominant arrays so the FFC code path receives uniform cal frames
(no vignette → near-identity FFC maps → near-passthrough on pixel values).

```python
def test_with_ffc(synthetic_demosaic, roll_directory_factory, cal_triplet_factory):
    roll_dir = roll_directory_factory(n_frames=2)
    cal = cal_triplet_factory()  # tmp_path/calibration
    result = composite_roll(roll_dir, workers=1, ffc_calibration_dir=cal)
    assert len(result.composited) == 2
```

### `roll_directory_factory`

```python
@pytest.fixture
def roll_directory_factory(tmp_path) -> Callable[[str, int, int], Path]:
    ...
```

Returns a `make(roll_name="RollIntTest", n_frames=1, start_frame=1) -> Path`
callable that creates `tmp_path / roll_name` and writes `n_frames` triplets named
`{roll_name}_Frame{NNN}_{R|G|B}.ARW`.  File content is one null byte — the
`synthetic_demosaic` stub never reads bytes, only filenames.

Files match `batch_composite.batch.FRAME_PATTERN` exactly, so `discover_frames` and
`composite_roll` consume them without modification.

Roll names use the prefix `RollIntTest` by default — grep-distinct from production
roll names (see Gotchas below).

```python
def test_batch(synthetic_demosaic, roll_directory_factory):
    roll_dir = roll_directory_factory(roll_name="RollIntTest", n_frames=36)
    result = composite_roll(roll_dir, workers=1)
    assert len(result.composited) == 36
```

### `clear_ffc_cache_before_each` (autouse)

```python
@pytest.fixture(autouse=True)
def clear_ffc_cache_before_each():
    ...
```

Calls `rgb_composite.clear_ffc_cache()` before and after every test.
`load_ffc_maps` is decorated with `@lru_cache`; without this cleaner, two tests
that happen to reuse the same `tmp_path`-rooted cal directory path would share a
stale FFC map from the previous test.

## Adding a new integration test

1. Identify the seam your test exercises:
   - orchestrator → batch-composite (filename contract) → add to
     `test_orchestrator_to_composite_handoff.py`
   - rgb-composite ↔ batch-composite (channel selection, FFC, DNG) → add to
     `test_triplet_to_composite.py`

2. Pick fixtures: most tests need `synthetic_demosaic` + either
   `roll_directory_factory` (compositing path) or the HW-mode helper pattern
   from `test_orchestrator_to_composite_handoff.py` (filename path).

3. Write the test in the relevant file. If it calls `composite_roll`, pass
   `workers=1` (see Gotchas below).

4. Run and time it:
   ```
   pytest tests/integration/<file>::<test_name> -v --durations=0
   ```

5. Confirm the new test adds less than 1 second to the suite runtime. If it's
   slower, check whether a fixture is doing more than writing 1-byte placeholder
   files or an inner loop is allocating large arrays.

## Gotchas

- **`workers=1` invariant.**  `composite_roll` defaults to a `ProcessPoolExecutor`.
  Workers run in separate processes and cannot see monkeypatched modules in the
  parent process.  Every call to `composite_roll` in integration tests MUST pass
  `workers=1` to force the inline code path
  (`phase2/batch-composite/batch_composite/batch.py:254-264`).  Omitting `workers=1`
  causes tests to hang waiting for a worker that cannot import the patched module.

- **`clear_ffc_cache()` between FFC tests.**  The FFC module uses `@lru_cache` on the
  cal directory path.  The autouse `clear_ffc_cache_before_each` fixture handles this
  automatically, but if you write custom FFC tests that manually construct
  `FFCMaps` or call `load_ffc_maps` directly, call `clear_ffc_cache()` explicitly
  before and after.

- **`RollIntTest` prefix.**  Integration fixture roll names use `RollIntTest` by
  default.  This prefix is deliberately grep-distinct from production roll names
  (which follow `{CameraID}_{Date}_Roll{N}` or operator-assigned conventions).
  Keep the convention so integration test artifacts are immediately recognisable
  in log scans and don't accidentally collide with operator-configured directories.

## Running

Full integration suite from repo root:
```
pytest tests/integration/
```

Single test, verbose:
```
pytest tests/integration/test_triplet_to_composite.py::test_full_roll_36_frames -v
```

Bare `pytest` from repo root (picks up integration suite only via `pytest.ini`'s
`testpaths = tests/integration` — per-package suites under `phase2/*/tests/` are
isolated by their own `pyproject.toml` testpaths and are NOT collected by the root
invocation; this is deliberate):
```
pytest
```

## See also

- `.planning/phases/02-e2e-integration-test/SPEC.md` — design rationale, fixture
  design decisions, anti-patterns.
- `phase2/rgb-composite/README.md` — rgb-composite package under test.
- `phase2/batch-composite/README.md` — batch-composite package under test.
- `phase2/rgb-composite/tests/test_composite.py` line 35 — prior-art
  `demosaics_stub` fixture this suite extends.
