# batch-composite

Walks a roll directory of narrowband-RGB triplet captures and composites every frame into a 16-bit linear ProPhoto-RGB TIFF.

Phase 2 / Deliverable 2C of the film scanner build — see `../../PROJECT.md`.

## Input layout (produced by Phase 2A's `triplet-capture`)

```
/Volumes/SSD/Scans/Roll001/
    Roll001_Frame001_R.ARW
    Roll001_Frame001_G.ARW
    Roll001_Frame001_B.ARW
    Roll001_Frame002_R.ARW
    ...
    Roll001_Frame036_B.ARW
    scan_log.jsonl
```

## Output layout

```
/Volumes/SSD/Scans/Roll001/
    composites/
        Roll001_Frame001.tif
        Roll001_Frame002.tif
        ...
        Roll001_Frame036.tif
```

## Install

```bash
cd phase2/batch-composite
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# Also install rgb-composite (the per-frame compositor):
pip install -e ../rgb-composite
```

## Usage

```bash
batch-composite /Volumes/SSD/Scans/Roll001
```

Options:

| Flag | Meaning |
|---|---|
| `--workers N` | Process-pool size. Default: `os.cpu_count()`. Pass `1` to run inline (useful for debugging). |
| `--overwrite` | Re-composite frames even if the output TIFF already exists. By default existing outputs are skipped. |
| `-v, --verbose` | DEBUG-level logging. |

Exit codes: `0` if everything succeeded (composited or skipped). `1` if any frame *failed* during compositing (skipped-due-to-missing-channel is not a failure).

Final stdout line is a one-line summary: `composited: N  skipped: M  failed: K`.

## Behavior

- Files are grouped by frame number (`{roll}_Frame{NNN}_{R|G|B}.ARW`). Files that don't match the convention are ignored with a debug log.
- A frame is composited only when all three channels (R, G, B) are present. Missing-channel frames are logged as warnings and listed in the final summary as skipped.
- libraw is not thread-safe within a single frame but is safely parallelizable across frames. We use `ProcessPoolExecutor` with one frame per worker.
- Outputs that already exist are skipped by default (idempotent re-runs). Pass `--overwrite` to force.

## Library usage

```python
from batch_composite import composite_roll
result = composite_roll("/Volumes/SSD/Scans/Roll001", workers=4)
print(result.composited)  # list of Path
print(result.skipped)     # list of (FrameGroup, SkipReason)
print(result.failed)      # list of (FrameGroup, error_message)
```

## Tests

```bash
pytest
```

Tests substitute the per-frame compositor with a stub that writes a marker file. They verify:
- Discovery groups files by frame number, ignores non-matching files, sorts numerically.
- Missing-channel frames are detected and reported, never silently dropped.
- Existing outputs are skipped unless `--overwrite` is passed.
- Failures are captured per frame and surfaced via exit code.

Tests run inline (`workers=1`) so a stubbed `rgb_composite` is visible to the worker — process-pool workers wouldn't see a monkeypatched module. The same code path runs in both modes; the production parallelism is exercised by the type system and the `ProcessPoolExecutor` `_composite_one` boundary, which has no per-call state.
