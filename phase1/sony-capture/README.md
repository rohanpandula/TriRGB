# sony-capture

Single-shot tether capture for the Sony a7CR (and any body supported by Sony Camera Remote SDK v1.10+). Phase 1 / Deliverable 1B of the film scanner build — see `../../PROJECT.md`.

## What it does

Drives one capture-and-download cycle against the connected camera over USB. The Phase 2 orchestrator shells out to this binary three times per frame (R, G, B), once the Scanlight is set to the matching channel.

## Camera-side setup (do this on the body first)

- **PC Remote** mode enabled
- File format: **RAW (lossless compressed)**
- Save destination: **PC** (Host)
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

## Usage

```
sony-capture --out PATH [--timeout SECONDS]
```

| Flag | Meaning |
|---|---|
| `--out PATH` | Where to write the downloaded RAW. Parent directories are created if missing. If the file exists it is overwritten (intentional — supports retakes). |
| `--timeout SECONDS` | Per-stage (connect, download) timeout. Default 30. |

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Gatekeeper dialog on first run | SDK dylibs are quarantined | `xattr -dr com.apple.quarantine` on the SDK tree (see Build §2) |
| `EnumCameraObjects failed` | Camera not in PC Remote mode, USB cable is power-only, camera not powered | Check body menu, swap to a known-good USB-C **data** cable, confirm dummy battery is connected |
| `Connect failed (CrError 0x…)` | Other host already claimed the camera; PC Remote authentication setting | Quit Imaging Edge / any other tether app; on newer bodies, disable "Connection Authentication" in menu for headless use |
| `timed out waiting for image download` | Shutter speed longer than `--timeout`, or file transfer is genuinely slow | Bump `--timeout`; check shutter speed; consider tether speed (USB 3.x vs 2.0) |
| Capture happens but no file written | Camera-side save destination is "Camera" or "Camera+PC" instead of "PC" | Change save destination to **PC** in body menu |

## Implementation notes

- `IDeviceCallback` is implemented in-line as `CaptureCallback`. The capture flow blocks on a `condition_variable` for `OnConnected` and `OnCompleteDownload`.
- The SDK's `EnumCameraObjects` returns a `const ICrCameraObjectInfo*`, but `Connect` requires non-const. We follow the official sample's pattern: re-build the info via `SDK::CreateCameraObjectInfo(...)` copying values out of the enumerated entry, then release the enumeration immediately.
- `SetSaveInfo` points the SDK at a unique per-invocation scratch dir (`.sony-capture-tmp-<pid>-<ts>` alongside `--out`). After the download callback fires, we `rename` the result onto `--out` and `remove_all` the scratch dir.
- `rpath` is set to `@executable_path/../third_party/sony_sdk/external/crsdk` (and the `CrAdapter` subdir) at link time — the binary finds its dylibs without `DYLD_LIBRARY_PATH`.

## Not implemented in Phase 1

These are explicitly deferred:
- Live view (Phase 3)
- Setting capture parameters from the CLI (operator drives the camera)
- Picking among multiple cameras (we always use the first)
- Reconnect/retry logic (left to the orchestrator)
