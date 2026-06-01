# scanlightctl

Driver and CLI for the **Scanlight v4** narrowband-RGB light source ([jackw01](https://jackw01.github.io/)) over USB CDC serial. Phase 1 / Deliverable 1A of the film scanner build — see `../../PROJECT.md`.

## Hardware setup (read first)

- **LEFT** USB-C port on the Scanlight: data only, connects to the Mac.
- **RIGHT** USB-C port: power only, connects to a wall PSU rated ≥5V/2A. **Never power the Scanlight from a Mac USB port.**
- **3.5mm shutter jack:** use only when the camera is not USB-tethered to
  the same Mac. It is safe for the Wi-Fi/IED path after pulse verification,
  but never combine it with USB camera tethering because that closes a
  ground loop. The current safest path is manual IED trigger, which uses no
  shutter cable.

## Install

```bash
cd phase1/scanlightctl
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs `pyserial` and registers the `scanlightctl` console script.

## Commands

```
scanlightctl [--port /dev/cu.usbmodemXXXX] [--timeout 2.0] COMMAND
```

The port is auto-discovered when possible. If the Scanlight doesn't expose a recognizable descriptor, pass `--port` explicitly.

| Command | Effect |
|---|---|
| `scanlightctl on r [--level N]` | Red channel only, level 0–255 (default 255). G/B/W → 0. |
| `scanlightctl on g [--level N]` | Green channel only. |
| `scanlightctl on b [--level N]` | Blue channel only. |
| `scanlightctl on w [--level N]` | White channel only (used for framing in Phase 3). |
| `scanlightctl off` | All channels → 0. |
| `scanlightctl set --r N --g N --b N` | Combined RGB (W = 0). |
| `scanlightctl set-default --r N --g N --b N` | RGB + persist to NVM as power-on default. **Use sparingly — finite write cycles.** |
| `scanlightctl status` | Firmware version, hardware id, NVM defaults, last LED temp + VBUS. |

### Firmware constraints

- The device firmware blocks white + RGB simultaneously. This driver mirrors that — `set_color(r=100, w=100)` raises before any bytes go out.
- `save_preset` writes to NVM. Only `set-default` sets this flag; nothing else touches it.

## Library usage

The CLI is a thin wrapper around `scanlight.Scanlight`, which Phase 2's triplet-capture orchestrator imports directly to avoid shelling out per command (and to keep the telemetry stream continuous):

```python
from scanlight import Scanlight

with Scanlight() as s:                 # auto-discovers, starts reader thread
    fw, hw = s.get_fw_version()
    s.set_color(r=200, g=0, b=0, w=0)  # red only
    print(s.last_temp_c)               # populated by background telemetry
    s.off()
```

`Scanlight` is a context manager. The background reader thread is stopped and the serial port closed on `__exit__`.

## Phase 1 manual exit criteria

With camera and Scanlight both connected per `../../PROJECT.md § Hardware architecture`:

```bash
scanlightctl on r
sony-capture --out Frame001_R.ARW
scanlightctl on g
sony-capture --out Frame001_G.ARW
scanlightctl on b
sony-capture --out Frame001_B.ARW
scanlightctl off
```

…produces three ~60–80 MB RAW files.

## Tests

```bash
pytest
```

Tests use an in-memory fake serial port — no hardware needed.

## Wire protocol summary

See `scanlight/protocol.py` for the authoritative constants. Packets are framed as `0xFE | header | length | data...` in both directions; telemetry (`LED_TEMP`, `VBUS`) arrives every ~200ms unsolicited.
