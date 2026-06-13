# sony-capture

Single-shot tether capture for the Sony a7CR (and any body supported by Sony Camera Remote SDK v1.10+). Phase 1 / Deliverable 1B of the film scanner build — see `../../PROJECT.md`.

## What it does

Drives one capture-and-download cycle against the connected camera over USB or Wi-Fi. The Phase 2 orchestrator shells out to this binary three times per frame (R, G, B), once the Scanlight is set to the matching channel.

## Camera-side setup (do this on the body first)

- **PC Remote** mode enabled
- File format: **RAW (lossless compressed)**
- Save destination: **PC** (Host) or **PC + memory card**
- Manual exposure, ISO 100, fixed WB, manual focus, IBIS off, EFCS or electronic shutter
- All in-camera corrections disabled

The CLI deliberately does **not** change capture settings in Phase 1 — calibration is operator-driven so we don't accidentally drift a setting between captures.

## Build

Prerequisites:
- macOS, Apple Silicon (the binary builds and runs native arm64; the Sony dylibs are universal so x86_64 is also fine)
- CMake ≥ 3.20
- Xcode command line tools (clang++ with libc++)
- Sony Camera Remote SDK v2.01.00 or newer

### 1. Install the SDK

If `third_party/sony_sdk/app/` and `third_party/sony_sdk/external/` already exist on this checkout, the SDK is installed. Otherwise see `third_party/sony_sdk/INSTALL.md`.

### 2. Strip Gatekeeper quarantine from the SDK dylibs (one-time per install)

The Sony SDK is not notarized by Apple, so any time the dylibs come from a fresh download they carry the `com.apple.quarantine` extended attribute. dyld will refuse to load them and you'll see a "could not verify free of malware" dialog. Clear it:

```bash
xattr -dr com.apple.quarantine third_party/sony_sdk
```

This is metadata-only — it doesn't change the dylibs.

### 3. Build

```bash
cmake -B build -G "Unix Makefiles"
cmake --build build
```

Result: `build/sony-capture`.

If CMake is not installed, this target can also be rebuilt directly because it
is a single translation unit:

```bash
xcrun clang++ -std=c++17 -stdlib=libc++ \
  -I third_party/sony_sdk/app \
  src/main.cpp third_party/sony_sdk/external/crsdk/libCr_Core.dylib \
  -o build/sony-capture \
  -Wl,-rpath,@executable_path \
  -Wl,-rpath,@executable_path/../third_party/sony_sdk/external/crsdk \
  -Wl,-rpath,@executable_path/../third_party/sony_sdk/external/crsdk/CrAdapter
```

## Usage

```
sony-capture --list
sony-capture --connect-only [--ip-address IP [--mac-address MAC]]
sony-capture --out PATH [--timeout SECONDS] [--ip-address IP [--mac-address MAC]]
sony-capture --persist [connection flags]
sony-capture --live-view-out PATH [--ip-address IP [--mac-address MAC]]
sony-capture --live-view-stream-out PATH [--ip-address IP [--mac-address MAC]]
```

| Flag | Meaning |
|---|---|
| `--out PATH` | Where to write the downloaded RAW. Parent directories are created if missing. If the file exists it is overwritten (intentional — supports retakes). |
| `--persist` | Persistent session mode: connect once, then loop reading commands from stdin. See **Persistent mode** below. |
| `--s1-settle-ms N` | Milliseconds to wait after S1 lock before firing the shutter. Default 500. MF/manual-exposure rigs can drop to ~50 ms; auto-focus bodies need the full settle for the AF lock indicator. |
| `--post-release-ms N` | Milliseconds to wait after Release-Up before S1 unlock. Default 1000. MF/manual-exposure rigs can drop to 0 ms since `wait_downloaded` already blocks until the file arrives. |
| `--live-view-out PATH`, `--live-view PATH` | Connect, pull one SDK live-view JPEG frame, write it atomically to PATH, then disconnect. This does not fire the shutter or download a RAW. |
| `--live-view-stream-out PATH`, `--live-view-stream PATH` | Connect once, keep the SDK session open, and refresh PATH with live-view JPEG frames until SIGTERM/SIGINT. |
| `--live-view-interval-ms MS` | Delay between live-view stream frame polls. Default 250 ms. |
| `--timeout SECONDS` | Per-stage (connect, download) timeout. Default 30. |
| `--list` | Enumerate SDK-visible cameras and exit without connecting or firing the shutter. Safe first smoke test. |
| `--connect-only`, `--probe` | Connect/authenticate/cache fingerprint, then disconnect without firing the shutter. Use this before the first capture attempt. |
| `--ip-address IP` | Direct network PC Remote connection, matching SonShell's direct-IP path. Falls back to `SONY_IP`. |
| `--mac-address MAC` | Optional camera MAC for direct IP. Falls back to `SONY_MAC`. |
| `--username USER`, `--password PW` | Access Authentication credentials from the camera. `--user` is accepted as an alias; env fallbacks are `SONY_USERNAME`/`SONY_USER` and `SONY_PW`. |
| `--fingerprint-cache-path PATH` | Binary SDK fingerprint cache. Defaults to `~/.cache/sony-capture/fingerprint.bin`. Delete it if Access Auth pairing gets stuck. |

### Persistent mode (`--persist`)

`--persist` keeps one SDK session alive across multiple captures, eliminating per-capture SDK init + Wi-Fi reconnect overhead (~5–11 min/roll saved).

**Invocation:** all connection flags (`--ip-address`, `--mac-address`, env credentials, etc.) work identically. On successful connect + SetSaveInfo, the process prints `READY\n` to stdout and flushes.

**stdin commands → stdout responses** (all lines newline-terminated, stdout flushed immediately):

| stdin | stdout |
|---|---|
| `shutter <speed>` | `SHUTTER_OK <speed>` on success |
| | `SHUTTER_FAIL <speed>` on failure (session stays alive) |
| `capture <absolute-out-path>` | `CAPTURE_OK <absolute-out-path>` on success |
| | `CAPTURE_FAIL <short-reason>` on failure (session stays alive) |
| `quit` or EOF | clean teardown (Disconnect → ReleaseDevice → Release), exit 0 |
| unknown | `ERR unknown-command` |

- `shutter` re-applies the camera shutter mid-session (`SetDeviceProperty(ShutterSpeed)` on the live handle). Narrowband RGB scanning uses a **different shutter per channel** (blue needs far more exposure than red/green), so the orchestrator sends `shutter <speed>` before each channel's `capture` — and only when the value changed from the last one applied. A `SHUTTER_FAIL` makes the orchestrator abort that frame rather than expose at the wrong shutter.
- The path after `capture ` is treated as everything after the first space, so paths containing spaces are supported.
- `CAPTURE_FAIL` does **not** terminate the session — the orchestrator can retry or send `quit`.
- The `SONY_CAPTURE_EXPOSURE_COMPLETE` stderr marker is still emitted per capture right after the shutter fires (the Python orchestrator uses it to turn the LED off early).
- If connect fails, the process prints `FAIL <reason>` to stdout and exits nonzero.

**Example orchestrator usage:**

```bash
sony-capture --persist --ip-address 10.0.0.247 --user USER --password PW &
PID=$!
# Wait for READY
read -r line  # "READY"
echo "shutter 1/100"
read -r s     # "SHUTTER_OK 1/100"
echo "capture /Volumes/SSD/Roll001/Frame001_R.ARW"
read -r result  # "CAPTURE_OK /Volumes/SSD/Roll001/Frame001_R.ARW"
echo "shutter 1/4"   # blue channel: longer exposure
read -r s     # "SHUTTER_OK 1/4"
echo "capture /Volumes/SSD/Roll001/Frame001_B.ARW"
read -r result  # "CAPTURE_OK ..."
echo "quit"
wait $PID
```

Exit codes:
- `0` on success; the absolute path of the written file is printed to stdout
- `1` on any failure with a one-line message on stderr
- `2` on bad arguments

### Atomicity

`sony-capture` writes the file to a scratch directory adjacent to `--out` (so it's on the same filesystem) and `rename(2)`s onto the final path after the SDK signals `OnCompleteDownload`. Readers of the output directory therefore never see partial files.

### Example

```bash
sony-capture --out /Volumes/SSD/Scans/Roll001/Roll001_Frame001_R.ARW
# stdout: /Volumes/SSD/Scans/Roll001/Roll001_Frame001_R.ARW
# exit 0
```

Wi-Fi probe before capture:

```bash
sony-capture --list
sony-capture --connect-only --ip-address 192.168.1.1 --user USER --password PW
sony-capture --out /tmp/test.ARW --ip-address 192.168.1.1 --user USER --password PW
sony-capture --live-view-out /tmp/sony-live-view.jpg --ip-address 192.168.1.1 --user USER --password PW
sony-capture --live-view-stream-out /tmp/sony-live-view.jpg --ip-address 192.168.1.1 --user USER --password PW
```

Known-good a7CR Wi-Fi path as of 2026-05-22:

```bash
sony-capture --out /tmp/test.ARW \
  --ip-address 10.0.0.247 \
  --mac-address 10:32:2C:26:1A:3F \
  --user USER \
  --password PW
```

That path uses host-PC auto-download and produced a valid 63 MB `ILCE-7CR` RAW.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Gatekeeper dialog on first run | SDK dylibs are quarantined | `xattr -dr com.apple.quarantine` on the SDK tree (see Build §2) |
| `EnumCameraObjects failed` | Camera not in PC Remote mode, USB cable is power-only, camera not powered | Check body menu, swap to a known-good USB-C **data** cable, confirm dummy battery is connected |
| `Connect failed (CrError 0x…)` | Other host already claimed the camera; PC Remote authentication setting | Quit Imaging Edge / any other tether app; for Wi-Fi use `--connect-only` first and check Access Authentication credentials/fingerprint cache |
| `timed out waiting for image download` | Shutter speed longer than `--timeout`, or file transfer is genuinely slow | Bump `--timeout`; check shutter speed; consider tether speed (USB 3.x vs 2.0) |
| Capture happens but no file written | Camera-side save destination excludes host-PC transfer, or another app claimed the PC Remote session | Use **PC** or **PC + memory card** in body menu; quit Imaging Edge / other tether apps |
| `--remote-transfer has been removed` | The RemoteTransfer path was deleted (it was broken on the a7CR and is a no-op on tested hardware) | Remove the flag from your invocation; the default host-PC mode is the correct path |

## Implementation notes

- `IDeviceCallback` is implemented in-line as `CaptureCallback`. The default capture flow blocks on `OnConnected` and the SDK host-PC `OnCompleteDownload` callback.
- USB enumeration passes the SDK's enumerated `ICrCameraObjectInfo` directly to `Connect`, matching Sony's SimpleCli behavior.
- Wi-Fi/direct IP prefers the SDK-enumerated camera object when it matches IP/MAC and falls back to `CreateCameraObjectInfoEthernetConnection`.
- Wi-Fi/direct IP does a short TCP preflight against port 22 before entering the Sony SDK so camera-off cases fail quickly instead of hanging inside `Connect`.
- Host-PC mode uses `CrSdkControlMode_Remote` + `SetSaveInfo`, which works on the a7CR with Access Authentication over Wi-Fi.
- Live view uses the SDK preview JPEG path (`GetLiveViewImageInfo` + `GetLiveViewImage`). The streaming mode keeps one SDK connection open and atomically replaces the same JPEG file for the Swift UI.
- The `--out` fallback for empty `OnCompleteDownload` filenames prefers RAW extensions (ARW/SR2/SRF, case-insensitive); if multiple RAW candidates exist, the largest is picked. A warning is emitted if a non-RAW file is the only option (body may be in RAW+JPEG mode).
- `CaptureCallback::reset_for_next_capture()` clears per-capture state (downloaded flag, filename, last error) under the mutex before each capture in `--persist` mode. Connection state (`connected_`, `disconnected_`) is not cleared.
- `rpath` is set to `@executable_path/../third_party/sony_sdk/external/crsdk` (and the `CrAdapter` subdir) at link time — the binary finds its dylibs without `DYLD_LIBRARY_PATH`.
- **RemoteTransfer removed:** the `--remote-transfer` / `--transfer-mode remote-transfer` path (~200 lines) was deleted. It was known-broken on the a7CR (error 36101) and wasted its full timeout on every invocation. The implementation lives in git history if a future body needs it.

## Not implemented in Phase 1

These are explicitly deferred:
- Setting capture parameters from the CLI (operator drives the camera)
- Picking among multiple cameras (we always use the first)
- Reconnect/retry logic (left to the orchestrator)
