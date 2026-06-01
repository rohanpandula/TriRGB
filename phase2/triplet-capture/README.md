# triplet-capture

Per-frame R/G/B capture orchestrator. One button, three exposures, manual film advance between frames.

Phase 2 / Deliverable 2A of the film scanner build — see `../../PROJECT.md`.
The Flask UI is now a fallback/manual surface; the Swift app starts and
drives this same Python orchestrator for the unified workflow.

## What it does, per frame

```
Scanlight R only → sleep settle_ms → capture/wait → {roll}_Frame{NNN}_R.ARW
Scanlight G only → sleep settle_ms → capture/wait → {roll}_Frame{NNN}_G.ARW
Scanlight B only → sleep settle_ms → capture/wait → {roll}_Frame{NNN}_B.ARW
Scanlight off
Verify all three files exist and are 40–120 MB
Advance frame counter — ONLY on success
```

Capture/wait is selected by `--trigger-mode`:

| Mode | Behavior |
|---|---|
| `manual` | Current default. The app sets each Scanlight color and waits for the operator to manually trigger an Imaging Edge Desktop capture into `--ied-inbox`. No Sony SDK and no Scanlight shutter pulse. |
| `hw` | The app sets each Scanlight color, pulses the Scanlight 3.5 mm shutter output, then waits for the resulting IED inbox file. |
| `sdk` | Sony SDK path via `sony-capture`. The Swift app passes the Wi-Fi IP/MAC and Access Auth fields through to the verified host-PC auto-download path. |

In SDK mode, exposure calibration assumes manual exposure, f/8, and manual focus are fixed. The Sony SDK cannot move the physical mode dial on the a7CR, so the body must already be in M. Each SDK capture asks for ISO 100, falling back to ISO 125 if 100 is not in the camera's candidate list; it does not use extended ISO 50. Calibration then chooses per-channel Sony shutter speed as the coarse exposure control and per-channel Scanlight LED level as the fine trim.

## Install

```bash
cd phase2/triplet-capture
python3 -m venv .venv
source .venv/bin/activate
pip install -e "../../phase1/scanlightctl"   # the Scanlight class
pip install -e .[dev]
# sony-capture is only needed for --trigger-mode sdk.
```

## Run

```bash
triplet-capture \
  --roll-name Roll001 \
  --output-folder /Volumes/SSD/Scans \
  --trigger-mode manual \
  --ied-inbox /Volumes/SSD/_ied_inbox
# → web UI on http://127.0.0.1:8765
```

Open the URL. One button: **Capture Triplet**. In manual mode, fire one
IED capture when the light is red, one when it is green, and one when it
is blue. Frame counter advances automatically on success. **Retake**
overwrites the current frame's files without advancing — use it when the
result is bad.

SDK mode example:

```bash
triplet-capture \
  --roll-name Roll001 \
  --output-folder /Volumes/SSD/Scans \
  --trigger-mode sdk \
  --sony-ip-address 10.0.0.247 \
  --sony-mac-address 10:32:2C:26:1A:3F \
  --sony-user USER \
  --sony-password PASSWORD \
  --shutter-r 1/4 --shutter-g 1/4 --shutter-b 1/2
```

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
| Manual/hardware IED timeout | Surface timeout in the log; frame counter does NOT advance; Scanlight is turned off |
| Ambiguous IED inbox | Quarantine the inbox files, abort the frame, and do NOT advance |
| `sony-capture` exits nonzero | SDK mode only. Surface stderr in the log; frame counter does NOT advance; Scanlight is turned off |
| `sony-capture` exits 0 but file missing | SDK mode only. Same as above |
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

Tests cover: settings validation, channel sequencing, manual IED pickup,
hardware-pulse pickup, frame-counter advance-on-success-only, retake
overwrite without advance, implausible-size + missing-file aborts,
log-line correctness, and the HTTP endpoints. The Scanlight, IED inbox,
and SDK subprocess are stubbed where needed; no hardware is required.
