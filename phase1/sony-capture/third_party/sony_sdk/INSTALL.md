# Sony Camera Remote SDK — installed here

The SDK is **not redistributable**, so this directory is gitignored except for this file. The build expects `app/CRSDK/*.h` (headers) and `external/crsdk/*.dylib` (libraries) to be present.

## Currently installed

If you can see `app/` and `external/` next to this file, the SDK is set up. Architecture: universal (arm64 + x86_64). Verified working against the SDK shipped in `CrSDK_v2.01.00_20260203a_Mac.zip`.

## Re-installing on a new machine

```bash
# 1. Get the SDK from https://www.sony.net/Products/CameraRemoteSDK/
#    (Sony requires registration but the download is free)

# 2. Unzip the outer + inner archive into this directory:
cd phase1/sony-capture/third_party/sony_sdk
unzip /path/to/CrSDK_v2.01.00_*_Mac.zip RemoteCli.zip
unzip RemoteCli.zip
rm RemoteCli.zip

# 3. Strip Gatekeeper quarantine (the dylibs aren't notarized by Apple):
xattr -dr com.apple.quarantine .

# 4. Build:
cd ../..   # back to phase1/sony-capture/
cmake -B build && cmake --build build
```

## When upgrading the SDK

1. Delete `app/`, `external/`, `cmake/`, `CMakeLists.txt`, `README.md` (the SDK files, not this `INSTALL.md`).
2. Repeat the install steps above with the new SDK zip.
3. Re-run the test suites — if Sony changed the API, `sony-capture/src/main.cpp` may need updates.
