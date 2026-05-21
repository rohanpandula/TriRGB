// sony-capture — single-shot tether capture for the Sony a7CR (and any
// Camera Remote SDK v1.10+ supported body).
//
// Flow:
//   1. SDK::Init()
//   2. EnumCameraObjects → connect to the first device.
//   3. Wait for OnConnected.
//   4. SetSaveInfo() to a unique temp directory adjacent to --out.
//   5. SendCommand(Release, Down) → SendCommand(Release, Up).
//   6. Wait for the RemoteTransfer contents-list add notification.
//   7. Fetch the new contents info and pull its first still file.
//   8. Rename the downloaded file onto --out atomically.
//   9. Disconnect → ReleaseDevice → Release.
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
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <mutex>
#include <optional>
#include <string>
#include <system_error>
#include <thread>
#include <vector>
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
        {
            std::lock_guard<std::mutex> lk(mtx_);
            disconnected_ = true;
            last_error_ = error;
            cv_.notify_all();
        }
        // A mid-capture disconnect must also wake the RemoteTransfer waits
        // (their predicates check last_error_) so they don't stall to timeout.
        { std::lock_guard<std::mutex> lk(contents_mutex_); contents_cv_.notify_all(); }
        { std::lock_guard<std::mutex> lk(transfer_mutex_); transfer_cv_.notify_all(); }
    }

    void OnError(CrInt32u error) override {
        last_error_ = error;  // atomic — visible to every wait predicate
        // Wake ALL waits (connect/download AND the RemoteTransfer contents/
        // transfer waits) so an async SDK error surfaces promptly instead of
        // stalling to a wait's timeout. Each cv is notified under its own mutex.
        { std::lock_guard<std::mutex> lk(mtx_);            cv_.notify_all(); }
        { std::lock_guard<std::mutex> lk(contents_mutex_); contents_cv_.notify_all(); }
        { std::lock_guard<std::mutex> lk(transfer_mutex_); transfer_cv_.notify_all(); }
    }

    void OnCompleteDownload(CrChar* filename, CrInt32u type) override {
        // type == None means a captured image; SettingFile types are unrelated.
        if (type != SDK::CrDownloadSettingFileType_None) return;
        std::lock_guard<std::mutex> lk(mtx_);
        downloaded_filename_.assign(reinterpret_cast<const char*>(filename));
        downloaded_ = true;
        cv_.notify_all();
    }

    void OnNotifyRemoteTransferContentsListChanged(
        CrInt32u notify, CrInt32u slotNumber, CrInt32u addSize) override {
        if (notify != SDK::CrNotify_RemoteTransfer_Changed_Add) return;
        std::cerr << "sony-capture: RemoteTransfer contents added: slot "
                  << slotNumber << ", addSize " << addSize << std::endl;
        {
            std::lock_guard<std::mutex> lk(contents_mutex_);
            contents_slot_ = slotNumber;
            contents_changed_ = true;
        }
        contents_cv_.notify_all();
    }

    void OnNotifyRemoteTransferResult(
        CrInt32u notify, CrInt32u percent, CrChar* filename) override {
        if (notify == SDK::CrNotify_RemoteTransfer_InProgress) {
            std::cerr << "sony-capture: RemoteTransfer download "
                      << percent << "%" << std::endl;
            return;
        }

        if (notify == SDK::CrNotify_RemoteTransfer_Result_OK) {
            {
                std::lock_guard<std::mutex> lk(transfer_mutex_);
                transferred_filename_ = filename
                    ? reinterpret_cast<const char*>(filename)
                    : "";
                transfer_done_ = true;
            }
            transfer_cv_.notify_all();
            return;
        }

        std::cerr << "sony-capture: RemoteTransfer result notify "
                  << notify << " at " << percent << "%" << std::endl;
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

    bool wait_for_contents_changed(std::chrono::seconds timeout, CrInt32u& out_slot) {
        std::unique_lock<std::mutex> lk(contents_mutex_);
        if (!contents_cv_.wait_for(lk, timeout, [this] {
                return contents_changed_ || last_error_ != 0 || disconnected_;
            })) {
            return false;  // timed out
        }
        if (last_error_ != 0 || disconnected_) {
            return false;  // woke on an async error/disconnect, not a real add
        }
        out_slot = contents_slot_;
        contents_changed_ = false;
        return true;
    }

    bool wait_for_transfer_done(std::chrono::seconds timeout, std::string& out_filename) {
        std::unique_lock<std::mutex> lk(transfer_mutex_);
        if (!transfer_cv_.wait_for(lk, timeout, [this] {
                return transfer_done_ || last_error_ != 0 || disconnected_;
            })) {
            return false;  // timed out
        }
        if (last_error_ != 0 || disconnected_) {
            return false;  // woke on an async error/disconnect, not a completed transfer
        }
        out_filename = transferred_filename_;
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
    // Atomic so the RemoteTransfer wait predicates (which hold a different
    // mutex) can observe a mid-transfer disconnect even when it carries no
    // error code (error == 0).
    std::atomic<bool> disconnected_{false};
    bool downloaded_ = false;
    std::string downloaded_filename_;
    // Atomic so the RemoteTransfer waits (which hold contents_mutex_/
    // transfer_mutex_, not mtx_) can read it in their predicates without a
    // cross-mutex data race. Set by OnError/OnDisconnected.
    std::atomic<CrInt32u> last_error_{0};

    std::mutex contents_mutex_;
    std::condition_variable contents_cv_;
    bool contents_changed_ = false;
    CrInt32u contents_slot_ = 0;

    std::mutex transfer_mutex_;
    std::condition_variable transfer_cv_;
    bool transfer_done_ = false;
    std::string transferred_filename_;
};

struct Args {
    std::string out;
    int timeout_s = kDefaultTimeoutSeconds;
    std::string user;
    std::string password;
    std::string fingerprint_cache_path;
    std::string pairing_name = "SonyCapture";
    std::string ip_address;   // dotted-decimal, e.g. "10.0.0.247"
    std::string mac_address;  // colon-hex, e.g. "10:32:2c:26:1a:3f"
};

std::string default_fingerprint_cache_path() {
    if (const char* home = std::getenv("HOME")) {
        if (*home) return std::string(home) + "/.cache/sony-capture/fingerprint.bin";
    }
    return ".sony-capture-fingerprint.bin";
}

std::string expand_user_path(const std::string& path) {
    if (path.empty() || path[0] != '~') return path;
    const char* home = std::getenv("HOME");
    if (home == nullptr || *home == '\0') return path;
    if (path.size() == 1) return std::string(home);
    if (path[1] == '/') return std::string(home) + path.substr(1);
    return path;
}

std::vector<CrInt16u> make_pairing_display_name(const std::string& name) {
    std::vector<CrInt16u> out;
    out.reserve(name.size() + 1);
    for (unsigned char c : name) {
        out.push_back(static_cast<CrInt16u>(c));
    }
    out.push_back(0);
    return out;
}

void print_usage() {
    std::cerr <<
        "usage: sony-capture --out PATH [--timeout SECONDS]\n"
        "                    [--username USER] [--password PW]\n"
        "                    [--fingerprint-cache-path PATH]\n"
        "                    [--pairing-name NAME]\n"
        "                    [--ip-address IP --mac-address MAC]\n"
        "\n"
        "Tether-trigger one capture on the connected Sony body and write the\n"
        "downloaded RAW to PATH atomically.\n"
        "\n"
        "Options:\n"
        "  --out PATH         Output file (directory is created if missing).\n"
        "  --timeout SECONDS  Per-stage timeout (default 30).\n"
        "  --username USER    SDK Access Authentication username (a7CR fw >= 1.10).\n"
        "                     --user is accepted as an alias. Falls back to env\n"
        "                     SONY_USERNAME or SONY_USER. Default 'admin' for bodies\n"
        "                     without Access Authentication enabled.\n"
        "  --password PW      Authentication password. Falls back to env SONY_PW.\n"
        "  --fingerprint-cache-path PATH\n"
        "                     Binary cache file for the SDK fingerprint blob.\n"
        "                     Falls back to env SONY_FINGERPRINT_CACHE_PATH, then\n"
        "                     ~/.cache/sony-capture/fingerprint.bin.\n"
        "  --pairing-name NAME\n"
        "                     SSH pairing display name shown on-camera when the SDK\n"
        "                     asks for first-contact approval (default SonyCapture).\n"
        "  --ip-address IP    Connect via network instead of USB enumeration. The\n"
        "                     camera must be in network PC Remote mode and reachable\n"
        "                     at this IPv4 address. Falls back to env SONY_IP.\n"
        "  --mac-address MAC  Camera MAC address (six colon-hex bytes). Required\n"
        "                     when --ip-address is set. Falls back to env SONY_MAC.\n"
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
        } else if (arg == "--username" || arg == "--user") {
            if (i + 1 >= argc) { log_err(arg + " requires a value"); return false; }
            a.user = argv[++i];
        } else if (arg == "--password") {
            if (i + 1 >= argc) { log_err("--password requires a value"); return false; }
            a.password = argv[++i];
        } else if (arg == "--pairing-name") {
            if (i + 1 >= argc) { log_err("--pairing-name requires a value"); return false; }
            a.pairing_name = argv[++i];
        } else if (arg == "--fingerprint-cache-path") {
            if (i + 1 >= argc) { log_err("--fingerprint-cache-path requires a value"); return false; }
            a.fingerprint_cache_path = argv[++i];
        } else if (arg == "--ip-address") {
            if (i + 1 >= argc) { log_err("--ip-address requires a value"); return false; }
            a.ip_address = argv[++i];
        } else if (arg == "--mac-address") {
            if (i + 1 >= argc) { log_err("--mac-address requires a value"); return false; }
            a.mac_address = argv[++i];
        } else if (arg == "-h" || arg == "--help") {
            print_usage();
            std::exit(0);
        } else {
            log_err("unknown argument: " + arg);
            return false;
        }
    }
    if (a.out.empty()) { log_err("--out is required"); return false; }
    // Env-var fallback. CLI takes precedence; env fills the gap.
    if (a.user.empty()) {
        if (const char* e = std::getenv("SONY_USERNAME")) a.user = e;
    }
    if (a.user.empty()) {
        if (const char* e = std::getenv("SONY_USER")) a.user = e;
    }
    if (a.password.empty()) {
        if (const char* e = std::getenv("SONY_PW")) a.password = e;
    }
    if (a.fingerprint_cache_path.empty()) {
        if (const char* e = std::getenv("SONY_FINGERPRINT_CACHE_PATH")) {
            a.fingerprint_cache_path = e;
        }
    }
    if (a.fingerprint_cache_path.empty()) {
        a.fingerprint_cache_path = default_fingerprint_cache_path();
    }
    if (const char* e = std::getenv("SONY_PAIRING_NAME")) {
        if (a.pairing_name == "SonyCapture") a.pairing_name = e;
    }
    if (a.ip_address.empty()) {
        if (const char* e = std::getenv("SONY_IP")) a.ip_address = e;
    }
    if (a.mac_address.empty()) {
        if (const char* e = std::getenv("SONY_MAC")) a.mac_address = e;
    }
    if (!a.ip_address.empty() && a.mac_address.empty()) {
        log_err("--ip-address requires --mac-address (camera MAC, six colon-hex bytes)");
        return false;
    }
    // Body without Access Authentication keeps the legacy 'admin' behavior;
    // body with Access Authentication enabled needs the per-session creds
    // from the camera's "Access Authen. Info" screen.
    if (a.user.empty()) a.user = "admin";
    return true;
}

// The SDK expects IPv4 octets packed low-byte first:
// 192.168.0.5 => 0x0500A8C0. This matches Sony's RemoteCli sample and the
// value returned by inet_pton(...).s_addr on little-endian macOS.
bool parse_ipv4(const std::string& s, CrInt32u& out) {
    unsigned b0, b1, b2, b3;
    char extra;
    if (std::sscanf(s.c_str(), "%u.%u.%u.%u%c", &b0, &b1, &b2, &b3, &extra) != 4)
        return false;
    if (b0 > 255 || b1 > 255 || b2 > 255 || b3 > 255) return false;
    out =  static_cast<CrInt32u>(b0)
        | (static_cast<CrInt32u>(b1) << 8)
        | (static_cast<CrInt32u>(b2) << 16)
        | (static_cast<CrInt32u>(b3) << 24);
    return true;
}

// Parse "10:32:2c:26:1a:3f" into six bytes. Returns false on malformed input.
bool parse_mac(const std::string& s, CrInt8u out[6]) {
    unsigned b0, b1, b2, b3, b4, b5;
    char extra;
    if (std::sscanf(s.c_str(), "%x:%x:%x:%x:%x:%x%c",
                    &b0, &b1, &b2, &b3, &b4, &b5, &extra) != 6)
        return false;
    if (b0 > 255 || b1 > 255 || b2 > 255 || b3 > 255 || b4 > 255 || b5 > 255)
        return false;
    out[0] = static_cast<CrInt8u>(b0);
    out[1] = static_cast<CrInt8u>(b1);
    out[2] = static_cast<CrInt8u>(b2);
    out[3] = static_cast<CrInt8u>(b3);
    out[4] = static_cast<CrInt8u>(b4);
    out[5] = static_cast<CrInt8u>(b5);
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

bool load_binary_file(const fs::path& path, std::vector<char>& bytes, std::string& error) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        error = "could not open " + path.string();
        return false;
    }

    bytes.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    if (in.bad()) {
        error = "could not read " + path.string();
        bytes.clear();
        return false;
    }
    return true;
}

bool write_binary_file(const fs::path& path, const char* bytes, CrInt32u size, std::string& error) {
    const fs::path parent = path.parent_path();
    if (!parent.empty()) {
        std::error_code ec;
        fs::create_directories(parent, ec);
        if (ec) {
            error = "could not create " + parent.string() + ": " + ec.message();
            return false;
        }
    }

    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out) {
        error = "could not open " + path.string() + " for writing";
        return false;
    }

    out.write(bytes, static_cast<std::streamsize>(size));
    if (!out) {
        error = "could not write " + path.string();
        return false;
    }
    return true;
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

    // Two ways to obtain an ICrCameraObjectInfo:
    //   USB path:      EnumCameraObjects() finds a body on USB, then we pass
    //                  the enumerated pointer to SDK::Connect directly.
    //   Ethernet path: CreateCameraObjectInfoEthernetConnection() builds an
    //                  info object from a known IP + MAC + model. No
    //                  enumeration; the user supplies what Sony's RemoteCli
    //                  enumeration shows in its IP/MAC columns.
    SDK::ICrEnumCameraObjectInfo* enum_info = nullptr;
    SDK::ICrCameraObjectInfo* cam_info = nullptr;
    SDK::CrError rc = 0;
    const bool use_ip = !args.ip_address.empty();
    CrInt8u mac_bytes[6] = {0};

    if (use_ip) {
        CrInt32u ip_packed = 0;
        if (!parse_ipv4(args.ip_address, ip_packed)) {
            log_err("--ip-address is not a valid IPv4 address: " + args.ip_address);
            cleanup();
            return 1;
        }
        if (!parse_mac(args.mac_address, mac_bytes)) {
            log_err("--mac-address is not a valid MAC: " + args.mac_address);
            cleanup();
            return 1;
        }
        // a7CR with current firmware listens on SSH (port 22 open) for SDK
        // PC Remote sessions. Passing sshSupport=ON is required; sshSupport=0
        // makes the SDK try a non-SSH port that the camera does not listen on,
        // producing CrError_Connect_ConnectIP (0x8202).
        rc = SDK::CreateCameraObjectInfoEthernetConnection(
            &cam_info,
            SDK::CrCameraDeviceModel_ILCE_7CR,
            ip_packed,
            mac_bytes,
            SDK::CrSSHsupport_ON);
        if (CR_FAILED(rc) || cam_info == nullptr) {
            log_err("CreateCameraObjectInfoEthernetConnection failed (CrError "
                    + std::to_string(static_cast<unsigned>(rc)) + ")");
            cleanup();
            return 1;
        }
    } else {
        rc = SDK::EnumCameraObjects(&enum_info);
        if (CR_FAILED(rc) || enum_info == nullptr) {
            log_err("EnumCameraObjects failed (no camera connected, or USB not in PC Remote mode)");
            cleanup();
            return 1;
        }

        const auto ncams = enum_info->GetCount();
        if (ncams < 1) {
            log_err("no cameras found");
            if (enum_info) enum_info->Release();
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
        cam_info = const_cast<SDK::ICrCameraObjectInfo*>(
            enum_info->GetCameraObjectInfo(0));
        if (cam_info == nullptr) {
            log_err("GetCameraObjectInfo(0) returned null");
            if (enum_info) enum_info->Release();
            cleanup();
            return 1;
        }
    }

    // Connect param tuning — the SDK's Connect() defaults `userId`,
    // `userPassword`, and `fingerprint` to nullptr, but the a7CR's USB
    // handshake silently times out (CrError_Connect_TimeOut = 0x8208)
    // when those are null. Bodies without Access Authentication accept
    // userId="admin" with empty password+fingerprint (legacy SampleApp
    // pattern).
    //
    // With Access Authentication, do not type the ASCII/base64-ish string
    // shown on the camera screen into this CLI. That display is for operator
    // verification; SDK::Connect wants the exact byte blob returned by
    // SDK::GetFingerprint(), and that blob may contain NUL bytes. Keep it in a
    // binary cache and pass the real byte count, never strlen().
    const fs::path fingerprint_cache_path = args.fingerprint_cache_path.empty()
        ? fs::path()
        : fs::path(expand_user_path(args.fingerprint_cache_path));
    std::vector<char> fingerprint_blob;
    std::vector<char> cached_fingerprint_blob;
    std::string fingerprint_source = "none";

    const bool ssh_support = (cam_info->GetSSHsupport() == SDK::CrSSHsupport_ON);
    if (ssh_support && !fingerprint_cache_path.empty()) {
        std::error_code fp_ec;
        if (fs::exists(fingerprint_cache_path, fp_ec)) {
            std::string read_error;
            if (!load_binary_file(fingerprint_cache_path, cached_fingerprint_blob, read_error)) {
                log_err(read_error);
                if (enum_info) enum_info->Release();
                cleanup();
                return 1;
            }
        } else if (fp_ec) {
            log_err("could not stat fingerprint cache " + fingerprint_cache_path.string()
                    + ": " + fp_ec.message());
            if (enum_info) enum_info->Release();
            cleanup();
            return 1;
        }
    }

    if (ssh_support) {
        char fp_buffer[512] = {0};
        CrInt32u fp_len = 0;
        const auto fp_rc = SDK::GetFingerprint(cam_info, fp_buffer, &fp_len);
        if (CR_SUCCEEDED(fp_rc) && fp_len > 0) {
            if (fp_len > sizeof(fp_buffer)) {
                log_err("SDK::GetFingerprint returned an unexpectedly large fingerprint");
                if (enum_info) enum_info->Release();
                cleanup();
                return 1;
            }
            fingerprint_blob.assign(fp_buffer, fp_buffer + fp_len);
            fingerprint_source = "SDK::GetFingerprint";
        } else if (!cached_fingerprint_blob.empty()) {
            fingerprint_blob = cached_fingerprint_blob;
            fingerprint_source = "cache " + fingerprint_cache_path.string();
        } else if (CR_FAILED(fp_rc)) {
            log_err("SDK::GetFingerprint failed before connect (CrError "
                    + std::to_string(static_cast<unsigned>(fp_rc))
                    + "); attempting first-contact connect without cached fingerprint");
        } else {
            log_err("SDK::GetFingerprint returned no fingerprint; attempting first-contact "
                    "connect without cached fingerprint");
        }
    }

    if (fingerprint_blob.size() > std::numeric_limits<CrInt32u>::max()) {
        log_err("fingerprint cache is too large: " + fingerprint_cache_path.string());
        if (enum_info) enum_info->Release();
        cleanup();
        return 1;
    }

    const char* user_id = args.user.c_str();
    const char* user_password = args.password.c_str();
    const char* fingerprint = fingerprint_blob.empty() ? nullptr : fingerprint_blob.data();
    CrInt32u fingerprint_size = static_cast<CrInt32u>(fingerprint_blob.size());
    const auto reconnect = (use_ip || (ssh_support && fingerprint_blob.empty()))
        ? SDK::CrReconnecting_OFF
        : SDK::CrReconnecting_ON;
    // SonShell src/main.cpp Connect call (lines 3642-3646) intentionally does
    // NOT pass pairingDisplayName for the IP+SSH path. Passing one here
    // triggers CrError_Connect_SSH_InvalidParameter (0x8213). The pairing
    // confirmation on the camera screen happens via Access Authentication
    // (user/password) rather than a per-app display name.
    const CrInt16u* pairing_display_name_ptr = nullptr;
    // ControlMode: SonShell uses CrSdkControlMode_RemoteTransfer for both
    // USB-enumerated and direct-IP paths (src/main.cpp line 3643). The
    // older "Remote" mode is for legacy SDK clients on pre-Access-Auth
    // firmware; on a7CR fw >= 1.10 it produces CrError_Connect_SSH_InvalidParameter
    // (0x8213) on IP and a silent CrError_Connect_TimeOut (0x8208) on USB.
    const auto control_mode = SDK::CrSdkControlMode_RemoteTransfer;
    rc = SDK::Connect(
        cam_info, &cb, &handle,
        control_mode, reconnect,
        user_id, user_password, fingerprint, fingerprint_size,
        pairing_display_name_ptr);
    if (CR_FAILED(rc)) {
        log_err("Connect failed (CrError " + std::to_string(static_cast<unsigned>(rc)) + ")");
        if (enum_info) enum_info->Release();
        cleanup();
        return 1;
    }
    handle_allocated = true;  // SDK owns this handle; we must ReleaseDevice it on any exit

    if (!cb.wait_connected(std::chrono::seconds(args.timeout_s))) {
        const auto err = cb.last_error();
        if (err != 0) {
            log_err("camera connect failed asynchronously (CrError "
                    + std::to_string(static_cast<unsigned>(err)) + ")");
        } else {
            log_err("timed out waiting for camera to connect");
        }
        if (ssh_support) {
            log_err("Access Authentication hint: remove a stale fingerprint cache, "
                    "then retry with --username, --password, and --fingerprint-cache-path");
        }
        if (enum_info) enum_info->Release();
        cleanup();
        return 1;
    }
    connected = true;

    if (ssh_support && !fingerprint_cache_path.empty()) {
        char fp_buffer[512] = {0};
        CrInt32u fp_len = 0;
        const auto fp_rc = SDK::GetFingerprint(cam_info, fp_buffer, &fp_len);
        if (CR_SUCCEEDED(fp_rc) && fp_len > 0 && fp_len <= sizeof(fp_buffer)) {
            std::string write_error;
            if (!write_binary_file(fingerprint_cache_path, fp_buffer, fp_len, write_error)) {
                log_err(write_error);
            }
        } else if (fingerprint_source == "none") {
            log_err("connected, but SDK::GetFingerprint did not return a cacheable fingerprint");
        }
    }

    // Release the enumerator after any post-connect GetFingerprint call;
    // cam_info points into its storage.
    if (enum_info) enum_info->Release();

    // SetSaveInfo only applies to CrSdkControlMode_Remote (auto-download).
    // In CrSdkControlMode_RemoteTransfer we pull files explicitly via
    // GetRemoteTransferContentsDataFile with our own path/filename, so
    // SetSaveInfo is unnecessary. Worse: on a7CR fw >= 1.10 calling it in
    // RemoteTransfer mode leaves the session in a state where subsequent
    // SetDeviceProperty(S1) writes return CrError_Api_InvalidCalled (0x8402).
    // SonShell never calls SetSaveInfo (it works exclusively in RemoteTransfer
    // mode) — mirror that.
    constexpr int kImageSaveAutoStartNo = -1;
    std::string tmp_path_str = tmp_dir.string();
    char prefix[] = "frame";
    if (!use_ip) {
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
    }

    // Trigger the shutter — Release Down/Up.
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

    CrInt32u slot_raw = 0;
    if (!cb.wait_for_contents_changed(std::chrono::seconds(args.timeout_s), slot_raw)) {
        const auto err = cb.last_error();
        if (err != 0) {
            log_err("camera reported error before RemoteTransfer contents appeared (CrError "
                    + std::to_string(static_cast<unsigned>(err)) + ")");
        } else {
            log_err("timed out waiting for RemoteTransfer contents-list update");
        }
        cleanup();
        return 1;
    }

    SDK::CrContentsInfo* contents = nullptr;
    CrInt32u contents_count = 0;
    SDK::CrCaptureDate dummy_date{};
    const auto slot = static_cast<SDK::CrSlotNumber>(slot_raw);
    rc = SDK::GetRemoteTransferContentsInfoList(
        handle, slot, SDK::CrGetContentsInfoListType_All, &dummy_date, 1,
        &contents, &contents_count);
    if (CR_FAILED(rc) || contents == nullptr || contents_count == 0) {
        if (contents) SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);
        log_err("GetRemoteTransferContentsInfoList failed or returned no contents (CrError "
                + std::to_string(static_cast<unsigned>(rc)) + ")");
        cleanup();
        return 1;
    }

    const SDK::CrContentsInfo& content = contents[0];
    const SDK::CrContentsFile* file = nullptr;
    if (content.files != nullptr) {
        for (CrInt32u i = 0; i < content.filesNum; ++i) {
            if (content.files[i].fileFormat == SDK::CrContentsFile_FileFormat_Raw) {
                file = &content.files[i];
                break;
            }
        }
        if (file == nullptr && content.filesNum > 0) {
            file = &content.files[0];
        }
    }
    if (content.contentId == 0 || file == nullptr) {
        SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);
        log_err("RemoteTransfer contents entry did not include a downloadable file");
        cleanup();
        return 1;
    }

    const CrInt32u content_id = content.contentId;
    const CrInt32u file_id = file->fileId;
    SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);

    std::string download_dir_str = tmp_dir.string();
    std::string download_name_str = out_path.filename().string();
    rc = SDK::GetRemoteTransferContentsDataFile(
        handle, slot, content_id, file_id, 0x1000000,
        const_cast<CrChar*>(reinterpret_cast<const CrChar*>(download_dir_str.c_str())),
        const_cast<CrChar*>(reinterpret_cast<const CrChar*>(download_name_str.c_str())));
    if (CR_FAILED(rc)) {
        log_err("GetRemoteTransferContentsDataFile failed (CrError "
                + std::to_string(static_cast<unsigned>(rc)) + ")");
        cleanup();
        return 1;
    }

    std::string transferred_filename;
    if (!cb.wait_for_transfer_done(std::chrono::seconds(args.timeout_s), transferred_filename)) {
        const auto err = cb.last_error();
        if (err != 0) {
            log_err("camera reported error during RemoteTransfer download (CrError "
                    + std::to_string(static_cast<unsigned>(err)) + ")");
        } else {
            log_err("timed out waiting for RemoteTransfer download");
        }
        cleanup();
        return 1;
    }

    fs::path src;
    if (!transferred_filename.empty()) {
        const fs::path reported(transferred_filename);
        const fs::path tmp_reported = tmp_dir / reported.filename();
        if (reported.is_absolute() && fs::exists(reported)) {
            src = reported;
        } else if (fs::exists(tmp_reported)) {
            src = tmp_reported;
        } else if (fs::exists(reported)) {
            src = reported;
        }
    }
    if (!src.empty()) {
        src = fs::absolute(src);
    } else if (auto found = find_one_file(tmp_dir)) {
        src = fs::absolute(*found);
    } else {
        log_err("RemoteTransfer reported completion but no file found in " + tmp_dir.string());
        cleanup();
        return 1;
    }

    // Atomic rename onto --out. fs::rename overwrites the destination on
    // POSIX, which is what we want for retakes.
    if (src != out_path) {
        fs::rename(src, out_path, ec);
        if (ec) {
            log_err("rename " + src.string() + " -> " + out_path.string()
                    + " failed: " + ec.message());
            cleanup();
            return 1;
        }
    }

    exit_code = 0;
    std::cout << out_path.string() << std::endl;
    cleanup();
    return exit_code;
}
