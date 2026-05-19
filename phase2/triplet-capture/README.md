# triplet-capture

Per-frame R/G/B capture orchestrator. One button, three exposures, manual film advance between frames.

Phase 2 / Deliverable 2A of the film scanner build — see `../../PROJECT.md`. **This UI is disposable** — Phase 3's native Swift app replaces it. Don't invest beyond what the operator needs to press a button per frame.

## What it does, per frame

```
Scanlight R only → sleep settle_ms → sony-capture --out {roll}_Frame{NNN}_R.ARW
Scanlight G only → sleep settle_ms → sony-capture --out {roll}_Frame{NNN}_G.ARW
Scanlight B only → sleep settle_ms → sony-capture --out {roll}_Frame{NNN}_B.ARW
Scanlight off
Verify all three files exist and are 40–120 MB
Advance frame counter — ONLY on success
```

## Install

```bash
cd phase2/triplet-capture
python3 -m venv .venv
source .venv/bin/activate
pip install -e "../../phase1/scanlightctl"   # the Scanlight class
pip install -e .[dev]
# sony-capture binary must be on $PATH (or pass --sony-capture /path/to/binary)
```

## Run

```bash
triplet-capture \
  --roll-name Roll001 \
  --output-folder /Volumes/SSD/Scans
# → web UI on http://127.0.0.1:8765
```

Open the URL. One button: **Capture Triplet**. Frame counter advances automatically on success. **Retake** overwrites the current frame's files without advancing — use it when the result is bad.

## Layout on disk

```
{output_folder}/{roll_name}/
    Roll001_Frame001_R.ARW
    Roll001_Frame001_G.ARW
    Roll001_Frame001_B.ARW
    ...
    scan_log.jsonl   # one JSON line per action
```

Pass this directory directly to `batch-composite` after the roll is done.

## Per-channel level calibration

Set the R, G, B levels (0–255) in the UI. These are operator-tuned per film stock during the optical dry run; record the working levels in `docs/calibration_notes.md`.

Bumping a level brightens that LED (and thus the corresponding camera channel). Per PROJECT.md, start with mid-range (~200) and adjust until the histogram for each channel occupies the right half of 0–255 without clipping the film base highlights.

## Failure handling

| Failure | Behavior |
|---|---|
| `sony-capture` exits nonzero | Surface stderr in the log; frame counter does NOT advance; Scanlight is turned off |
| `sony-capture` exits 0 but file missing | Same as above |
| File exists but is <40 MB or >120 MB | Same as above; usually means a corrupt download or the camera changed file format |
| Scanlight serial error | Propagates as a Python exception; web UI shows it |

The state machine is deliberately blunt: *all three or none*. A half-captured frame is worse than a missing one because the compositor would silently produce a mis-colored TIFF.

## Library usage

```python
from scanlight import Scanlight
from triplet_capture import Orchestrator, CaptureSettings

with Scanlight() as light:
    settings = CaptureSettings(
        roll_name="Roll001",
        frame_number=1,
        output_folder=Path("/Volumes/SSD/Scans/Roll001"),
    )
    orch = Orchestrator(light, settings)
    result = orch.capture_triplet()
    print(result.success, result.files)
```

## Tests

```bash
pytest
```

16 tests cover: settings validation, channel sequencing, frame-counter advance-on-success-only, retake overwrite without advance, implausible-size + missing-file aborts, log-line correctness, the four HTTP endpoints. The Scanlight and sony-capture subprocess are both stubbed; no hardware needed.
