# Phase 3 — Live preview app (scaffold)

Phase 3 of the film scanner build — see `../PROJECT.md`.

**Status: scaffolded, not yet a working app.** Only the parts safe to build without hardware are in here. The SwiftUI shell, the Sony SDK Obj-C++ bridge, and the live-preview filter chain are all deferred until the Phase 1 hardware smoke test passes, because they all need a real camera to exercise.

## What's shipped now

| Module | What it does | Tested? |
|---|---|---|
| `FilmScanner/Sources/ScanlightSwift/Protocol.swift` | Wire codec — packet framing, encode `SET_COLOR`, decode `LED_TEMP`/`VBUS`/`FW_VERSION`/`DEFAULT_RGB` | Yes, 12 tests |
| `FilmScanner/Sources/ScanlightSwift/Scanlight.swift` | Driver class — background reader, request/response by header, telemetry cache, white+RGB rejection | Yes, 9 tests, in-memory `FakeTransport` |
| `FilmScanner/Sources/ScanlightSwift/Transport.swift` | `ScanlightTransport` protocol abstraction | (interface) |
| `FilmScanner/Sources/ScanlightSwift/SerialPortTransport.swift` | Real POSIX `/dev/cu.usbmodem*` impl | **Compiles, not yet HW-verified** |

The Swift driver is a direct port of `phase1/scanlightctl/scanlight/device.py` with the same correctness invariants (BE on the wire for LED_TEMP/VBUS/FW_VERSION, FW lives in the low 16 bits of the version word). The Python and Swift implementations should produce byte-identical wire output; a future cross-language test could feed the Python `Scanlight.set_color(...)` packet into Swift's `consumeBuffer` and vice versa, but that's overkill until the app actually exists.

## What's deferred

- `FilmScanner/Sources/PreviewPipeline/` — Core Image filter chain (`CIColorMatrix` → `CIColorInvert` → `CIToneCurve`). Buildable now on a static image but **the per-stock WB neutralization matrix coefficients require calibration data we won't have until the optical dry run is run on a real frame**, so investing here is premature.
- `FilmScanner/Sources/SonyBridge/` — Obj-C++ wrapper around the Sony Camera Remote SDK. Live-view delivery is the entire point of Phase 3 and it can only be exercised against a real camera. Phase 1B's `sony-capture` C++ binary already proves the SDK lifecycle works on this platform; the Phase 3 bridge is a port + live-view callback hook.
- `FilmScanner/App/` — SwiftUI shell with the two modes (Framing / Capture). Worth scaffolding only once the Sony bridge can deliver a JPEG to render.

## Build / test

```bash
cd phase3/FilmScanner
swift test
```

21 tests pass; no hardware needed.

## When HW arrives

See `../HANDOFF.md`. The Phase 3 work picks up after Phase 1 + 2 are smoke-tested against real hardware.
