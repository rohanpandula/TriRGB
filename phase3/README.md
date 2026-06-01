# Phase 3 — Swift Scanner App

Phase 3 of the film scanner build — see `../PROJECT.md`.

**Current status:** this is now a real SwiftPM macOS app target, not only a
driver scaffold. The app is the intended unified operator surface for the
scanner: Light, Settings, Calibrate, and Scan tabs all share one set of
state objects.

The app still wraps the tested Python/C++ pipeline instead of reimplementing
image processing in Swift. That is intentional. The Swift app owns UI,
settings, process lifecycle, serial-port handoff, and calibration workflow;
the Python tools own capture orchestration, FFC, compositing, calibration
math, and inversion helpers.

## What's Shipped Now

| Module | What it does | Hardware required? |
|---|---|---|
| `ScanlightSwift` | Swift Scanlight protocol/driver and fake transport | no for tests, yes for real serial |
| `scanlight-swift-cli` | JSON-capable CLI/selftest for automation | optional |
| `scanlight-app` | SwiftUI control hub with Light, Settings, Calibrate, and Scan tabs | no for fake-transport UI tests, yes for real scanning |
| `OrchestratorClient` | Starts/stops `triplet-capture` as a child process and drives its HTTP API | yes for real capture |
| `ScanCoordinator` | Owns the serial-port handoff between the manual Light panel and the Python scan/calibration process | no for tests |
| Calibration wizard | Starts `triplet-capture`, enters `.calibrating`, drives exposure calibration, FFC capture, and numeric checks through the Python backend, then releases the port | yes for real calibration |

Known remaining gap: manual IED mode still needs operator-visible R/G/B
prompts while capture/calibration routes are waiting for the next IED file.
FFC persistence also needs a final contract pass because the compositor still
expects a calibration triplet directory while the newer radiometric FFC route
can produce flat-stack data.

## Current Capture Direction

Sony SDK capture is CLI-verified over Wi-Fi and wired through the Swift app's
SDK trigger mode. Imaging Edge Desktop remains the fallback:

1. `manual` trigger mode: the app lights R/G/B and waits for you to trigger
   each channel manually in IED. This is the safest fallback because it uses
   no SDK and no Scanlight shutter pulse.
2. `hw` trigger mode: the app lights R/G/B and asks the Scanlight to pulse
   the 3.5 mm shutter output. Use this after the cable/pulse path is proven.
3. `sdk` trigger mode: the app passes Sony IP/MAC/Access Auth fields to
   `triplet-capture`, which calls `sony-capture` in the verified host-PC
   auto-download path.

Film advance is always manual through the Valoi 360.

## Still Deferred

- Live inverted preview from camera live-view frames.
- Sony SDK Obj-C++ bridge / direct Swift integration. The app currently uses
  the C++ CLI as the SDK boundary.
- Any motorized film advance.

## Build / Test

```bash
cd phase3/FilmScanner
swift test
```

For manual app testing without hardware:

```bash
swift run scanlight-app -FakeTransport YES
```

Real scanning also needs the Python packages installed in editable mode so
`triplet-capture` is on `PATH`.
