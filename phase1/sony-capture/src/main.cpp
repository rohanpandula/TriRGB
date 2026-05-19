// sony-capture — single-shot tether capture for the Sony a7CR (and any
// Camera Remote SDK v1.10+ supported body).
//
// Flow:
//   1. SDK::Init()
//   2. EnumCameraObjects → connect to the first device.
//   3. Wait for OnConnected.
//   4. SetSaveInfo() to a unique temp directory adjacent to --out.
//   5. SendCommand(Release, Down) → SendCommand(Release, Up).
//   6. Wait for OnCompleteDownload.
//   7. Rename the downloaded file onto --out atomically.
//   8. Disconnect → ReleaseDevice → Release.
//
// Camera-side prerequisites (set on the body before running this — we do not
// touch capture settings in Phase 1):
//   - PC Remote mode enabled
//   - File format: RAW (lossless compressed)
//   - Save destination: PC (Host)
//   - Manual exposure, ISO 100, fixed WB, AF off, IBIS off

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <mutex>
#include <optional>
#include <string>
#include <system_error>
#include <thread>
#include <unistd.h>

#include "CRSDK/CameraRemote_SDK.h"
#include "CRSDK/IDeviceCallback.h"
#include "CRSDK/ICrCameraObjectInfo.h"

namespace fs = std::filesystem;
namespace SDK = SCRSDK;

namespace {

constexpr int kDefaultTimeoutSeconds = 30;

void log_err(const std::string& msg) {
    std::cerr << "sony-capture: " << msg << std::endl;
}

class CaptureCallback : public SDK::IDeviceCallback {
public:
    void OnConnected(SDK::DeviceConnectionVersioin /*version*/) override {
        std::lock_guard<std::mutex> lk(mtx_);
        connected_ = true;
        cv_.notify_all();
    }

    void OnDisconnected(CrInt32u error) override {
        std::lock_guard<std::mutex> lk(mtx_);
        disconnected_ = true;
        last_error_ = error;
        cv_.notify_all();
    }

    void OnError(CrInt32u error) override {
        std::lock_guard<std::mutex> lk(mtx_);
        last_error_ = error;
        cv_.notify_all();
    }

    void OnCompleteDownload(CrChar* filename, CrInt32u type) override {
        // type == None means a captured image; SettingFile types are unrelated.
        if (type != SDK::CrDownloadSettingFileType_None) return;
        std::lock_guard<std::mutex> lk(mtx_);
        downloaded_filename_.assign(reinterpret_cast<const char*>(filename));
        downloaded_ = true;
        cv_.notify_all();
    }

    void OnWarning(CrInt32u /*warning*/) override {}

    // Wake on success, disconnect, OR async SDK error so the main thread
    // returns promptly with the real cause instead of stalling to timeout.
    bool wait_connected(std::chrono::seconds timeout) {
        std::unique_lock<std::mutex> lk(mtx_);
        return cv_.wait_for(lk, timeout, [this] {
                   return connected_ || disconnected_ || last_error_ != 0;
               })
               && connected_;
    }

    bool wait_downloaded(std::chrono::seconds timeout, std::string& out_path) {
        std::unique_lock<std::mutex> lk(mtx_);
        bool ok = cv_.wait_for(lk, timeout, [this] {
            return downloaded_ || disconnected_ || last_error_ != 0;
        });
        if (!ok || !downloaded_) return false;
        out_path = downloaded_filename_;
        return true;
    }

    CrInt32u last_error() {
        std::lock_guard<std::mutex> lk(mtx_);
        return last_error_;
    }

private:
    std::mutex mtx_;
    std::condition_variable cv_;
    bool connected_ = false;
    bool disconnected_ = false;
    bool downloaded_ = false;
    std::string downloaded_filename_;
    CrInt32u last_error_ = 0;
};

struct Args {
    std::string out;
    int timeout_s = kDefaultTimeoutSeconds;
};

void print_usage() {
    std::cerr <<
        "usage: sony-capture --out PATH [--timeout SECONDS]\n"
        "\n"
        "Tether-trigger one capture on the connected Sony body and write the\n"
        "downloaded RAW to PATH atomically.\n"
        "\n"
        "Options:\n"
        "  --out PATH         Output file (directory is created if missing).\n"
        "  --timeout SECONDS  Per-stage timeout (default 30).\n"
        "  -h, --help         Show this message.\n";
}

bool parse_args(int argc, char** argv, Args& a) {
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--out") {
            if (i + 1 >= argc) { log_err("--out requires a value"); return false; }
            a.out = argv[++i];
        } else if (arg == "--timeout") {
            if (i + 1 >= argc) { log_err("--timeout requires a value"); return false; }
            a.timeout_s = std::atoi(argv[++i]);
            if (a.timeout_s <= 0) { log_err("--timeout must be positive"); return false; }
        } else if (arg == "-h" || arg == "--help") {
            print_usage();
            std::exit(0);
        } else {
            log_err("unknown argument: " + arg);
            return false;
        }
    }
    if (a.out.empty()) { log_err("--out is required"); return false; }
    return true;
}

// Find the first captured file in `dir`. The SDK names files with whatever
// prefix we passed to SetSaveInfo plus a camera-assigned suffix; under our
// own temp directory, anything that turns up is ours.
std::optional<fs::path> find_one_file(const fs::path& dir) {
    std::error_code ec;
    for (const auto& entry : fs::directory_iterator(dir, ec)) {
        if (ec) return std::nullopt;
        if (entry.is_regular_file()) return entry.path();
    }
    return std::nullopt;
}

}  // namespace

int main(int argc, char** argv) {
    Args args;
    if (!parse_args(argc, argv, args)) {
        print_usage();
        return 2;
    }

    const fs::path out_path = fs::absolute(args.out);
    fs::path parent = out_path.parent_path();
    if (parent.empty()) parent = ".";

    std::error_code ec;
    fs::create_directories(parent, ec);
    if (ec) {
        log_err("could not create output directory " + parent.string() + ": " + ec.message());
        return 1;
    }

    // Unique scratch directory adjacent to the final output. Putting it on
    // the same filesystem as `out` means the eventual rename(2) is atomic.
    const fs::path tmp_dir =
        parent / (".sony-capture-tmp-" + std::to_string(::getpid()) +
                  "-" + std::to_string(
                      std::chrono::steady_clock::now().time_since_epoch().count()));
    fs::create_directories(tmp_dir, ec);
    if (ec) {
        log_err("could not create scratch dir " + tmp_dir.string() + ": " + ec.message());
        return 1;
    }

    // Tear-down helpers — RAII would be cleaner but the SDK doesn't offer it.
    //
    // `handle_allocated` tracks whether SDK::Connect succeeded synchronously
    // (which gives us an owned device handle that MUST be released) — separate
    // from `connected`, which is only true once the async OnConnected fires.
    // If Connect returns success but the callback times out, we still own the
    // handle and must ReleaseDevice it. Disconnect is only meaningful once
    // the link is actually up.
    int exit_code = 1;
    bool sdk_init = false;
    SDK::CrDeviceHandle handle = 0;
    bool handle_allocated = false;
    bool connected = false;
    CaptureCallback cb;

    auto cleanup = [&]() {
        if (connected) {
            SDK::Disconnect(handle);
        }
        if (handle_allocated) {
            SDK::ReleaseDevice(handle);
        }
        if (sdk_init) {
            SDK::Release();
        }
        std::error_code _ec;
        fs::remove_all(tmp_dir, _ec);
    };

    sdk_init = SDK::Init(0);
    if (!sdk_init) {
        log_err("SDK::Init failed");
        cleanup();
        return 1;
    }

    SDK::ICrEnumCameraObjectInfo* enum_info = nullptr;
    auto rc = SDK::EnumCameraObjects(&enum_info);
    if (CR_FAILED(rc) || enum_info == nullptr) {
        log_err("EnumCameraObjects failed (no camera connected, or USB not in PC Remote mode)");
        cleanup();
        return 1;
    }

    const auto ncams = enum_info->GetCount();
    if (ncams < 1) {
        log_err("no cameras found");
        enum_info->Release();
        cleanup();
        return 1;
    }

    // SimpleCli (the minimal Sony sample) does NOT recreate the info via
    // CreateCameraObjectInfo — it does a C-cast on the enumerated pointer
    // and passes that straight to Connect. CreateCameraObjectInfo appears
    // to be intended for cases where you build an info from scratch
    // (e.g., USB-by-serial or Ethernet construction), not for re-wrapping
    // an enumerated one. When we go through CreateCameraObjectInfo with a
    // USB-enumerated camera, Connect hangs and times out at 0x8208.
    SDK::ICrCameraObjectInfo* cam_info =
        const_cast<SDK::ICrCameraObjectInfo*>(enum_info->GetCameraObjectInfo(0));
    if (cam_info == nullptr) {
        log_err("GetCameraObjectInfo(0) returned null");
        enum_info->Release();
        cleanup();
        return 1;
    }

    // Connect param tuning — the SDK's Connect() defaults `userId`,
    // `userPassword`, and `fingerprint` to nullptr, but the a7CR's USB
    // handshake silently times out (CrError_Connect_TimeOut = 0x8208)
    // when those are null. The official SampleApp passes a literal
    // "admin" userId plus empty C-strings; mirror that or Connect never
    // completes asynchronously.
    const char* user_id = "admin";
    const char* user_password = "";
    const char* fingerprint = "";
    rc = SDK::Connect(
        cam_info, &cb, &handle,
        SDK::CrSdkControlMode_Remote, SDK::CrReconnecting_ON,
        user_id, user_password, fingerprint, 0);
    if (CR_FAILED(rc)) {
        log_err("Connect failed (CrError " + std::to_string(static_cast<unsigned>(rc)) + ")");
        enum_info->Release();
        cleanup();
        return 1;
    }
    // Release the enumerator AFTER Connect returns — cam_info points into
    // its storage and the SDK appears to keep references during the
    // asynchronous handshake.
    enum_info->Release();
    handle_allocated = true;  // SDK owns this handle; we must ReleaseDevice it on any exit

    if (!cb.wait_connected(std::chrono::seconds(args.timeout_s))) {
        const auto err = cb.last_error();
        if (err != 0) {
            log_err("camera connect failed asynchronously (CrError "
                    + std::to_string(static_cast<unsigned>(err)) + ")");
        } else {
            log_err("timed out waiting for camera to connect");
        }
        cleanup();
        return 1;
    }
    connected = true;

    // Set the host save directory. On macOS CrChar == char, so we can pass
    // a regular C string. The SDK assigns its own numeric suffix when
    // ImageSaveAutoStartNo (-1) is given.
    constexpr int kImageSaveAutoStartNo = -1;
    std::string tmp_path_str = tmp_dir.string();
    char prefix[] = "frame";
    rc = SDK::SetSaveInfo(
        handle,
        const_cast<CrChar*>(reinterpret_cast<const CrChar*>(tmp_path_str.c_str())),
        prefix,
        kImageSaveAutoStartNo);
    if (CR_FAILED(rc)) {
        log_err("SetSaveInfo failed (CrError " + std::to_string(static_cast<unsigned>(rc)) + ")");
        cleanup();
        return 1;
    }

    // Trigger the shutter. Down/Up is the canonical pattern from the SDK
    // sample; the brief delay between them avoids the camera collapsing them.
    // Always send Up after Down even if Down errors — otherwise the camera
    // can be left in a half-pressed state. Report the first error after both
    // commands have been issued.
    const auto rc_down = SDK::SendCommand(
        handle, SDK::CrCommandId_Release, SDK::CrCommandParam_Down);
    std::this_thread::sleep_for(std::chrono::milliseconds(35));
    const auto rc_up = SDK::SendCommand(
        handle, SDK::CrCommandId_Release, SDK::CrCommandParam_Up);
    if (CR_FAILED(rc_down) || CR_FAILED(rc_up)) {
        const auto first_err = CR_FAILED(rc_down) ? rc_down : rc_up;
        log_err("SendCommand(Release) failed (CrError "
                + std::to_string(static_cast<unsigned>(first_err)) + ")");
        cleanup();
        return 1;
    }

    std::string downloaded_path;
    if (!cb.wait_downloaded(std::chrono::seconds(args.timeout_s), downloaded_path)) {
        const auto err = cb.last_error();
        if (err != 0) {
            log_err("camera reported error during capture/download (CrError "
                    + std::to_string(static_cast<unsigned>(err)) + ")");
        } else {
            log_err("timed out waiting for image download");
        }
        cleanup();
        return 1;
    }

    // The SDK's OnCompleteDownload normally hands us the full path it wrote
    // to. Be defensive: if the path is empty or doesn't exist, fall back to
    // scanning our scratch dir for the single file we expect.
    fs::path src;
    if (!downloaded_path.empty() && fs::exists(downloaded_path)) {
        src = downloaded_path;
    } else if (auto found = find_one_file(tmp_dir)) {
        src = *found;
    } else {
        log_err("download reported but no file found in " + tmp_dir.string());
        cleanup();
        return 1;
    }

    // Atomic rename onto --out. fs::rename overwrites the destination on
    // POSIX, which is what we want for retakes.
    fs::rename(src, out_path, ec);
    if (ec) {
        log_err("rename " + src.string() + " -> " + out_path.string()
                + " failed: " + ec.message());
        cleanup();
        return 1;
    }

    exit_code = 0;
    std::cout << out_path.string() << std::endl;
    cleanup();
    return exit_code;
}
