// sony-capture — single-shot tether capture for the Sony a7CR (and any
// Camera Remote SDK v1.10+ supported body).
//
// Flow:
//   1. SDK::Init()
//   2. EnumCameraObjects → connect to the first device.
//   3. Wait for OnConnected.
//   4. SetSaveInfo() to a unique temp directory adjacent to --out.
//   5. SendCommand(Release, Down) → SendCommand(Release, Up).
//   6. In host-PC mode, wait for the SDK auto-download callback.
//      In RemoteTransfer mode, wait for a contents-list add notification.
//   7. Resolve the downloaded file.
//   8. Rename the downloaded file onto --out atomically.
//   9. Disconnect → ReleaseDevice → Release.
//
// Camera-side prerequisites:
//   - PC Remote mode enabled
//   - File format: RAW (lossless compressed)
//   - Save destination: PC (Host) or PC + memory card
//   - Fixed WB, AF off, IBIS off
//   - Manual exposure + ISO can be set with --exposure-program M --iso 100
//     when the body exposes those SDK properties as writable.

#include <atomic>
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cctype>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <csignal>
#include <cstring>
#include <cmath>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <limits>
#include <mutex>
#include <numeric>
#include <optional>
#include <sstream>
#include <string>
#include <system_error>
#include <thread>
#include <vector>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include "CRSDK/CameraRemote_SDK.h"
#include "CRSDK/IDeviceCallback.h"
#include "CRSDK/ICrCameraObjectInfo.h"

namespace fs = std::filesystem;
namespace SDK = SCRSDK;

namespace {

constexpr int kDefaultTimeoutSeconds = 30;
constexpr int kDefaultLiveViewIntervalMs = 250;
constexpr const char* kExposureCompleteMarker = "sony-capture: exposure-complete";

std::atomic<bool> g_stop_requested{false};

void handle_stop_signal(int /*signal*/) {
    g_stop_requested.store(true);
}

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
        downloaded_filename_ = filename ? reinterpret_cast<const char*>(filename) : "";
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

enum class TransferMode {
    RemoteTransfer,
    HostPc,
};

struct Args {
    std::string out;
    std::string live_view_out;
    std::string live_view_stream_out;
    std::string exposure_program;
    std::string iso;
    std::string shutter_speed;
    int live_view_interval_ms = kDefaultLiveViewIntervalMs;
    int timeout_s = kDefaultTimeoutSeconds;
    std::string user;
    std::string password;
    std::string fingerprint_cache_path;
    std::string pairing_name = "SonyCapture";
    std::string ip_address;   // dotted-decimal, e.g. "10.0.0.247"
    std::string mac_address;  // colon-hex, e.g. "10:32:2c:26:1a:3f"
    bool list_cameras = false;
    bool connect_only = false;
    bool status_only = false;
    bool list_capture_settings = false;
    bool list_shutter_speeds = false;
    TransferMode transfer_mode = TransferMode::HostPc;
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

void print_usage() {
    std::cerr <<
        "usage: sony-capture --out PATH [--timeout SECONDS]\n"
        "                    [--username USER] [--password PW]\n"
        "                    [--fingerprint-cache-path PATH]\n"
        "                    [--pairing-name NAME]\n"
        "                    [--ip-address IP [--mac-address MAC]]\n"
        "                    [--transfer-mode remote-transfer|host-pc]\n"
        "       sony-capture --list\n"
        "       sony-capture --connect-only [--ip-address IP [--mac-address MAC]]\n"
        "       sony-capture --status [--ip-address IP [--mac-address MAC]]\n"
        "       sony-capture --list-capture-settings [--ip-address IP [--mac-address MAC]]\n"
        "       sony-capture --set-exposure-program M --iso 100 [--ip-address IP [--mac-address MAC]]\n"
        "       sony-capture --list-shutter-speeds [--ip-address IP [--mac-address MAC]]\n"
        "       sony-capture --set-shutter-speed VALUE [--ip-address IP [--mac-address MAC]]\n"
        "       sony-capture --live-view-out PATH [--ip-address IP [--mac-address MAC]]\n"
        "       sony-capture --live-view-stream-out PATH [--ip-address IP [--mac-address MAC]]\n"
        "\n"
        "Tether-trigger one capture on the connected Sony body and write the\n"
        "downloaded RAW to PATH atomically, or write one live-view JPEG frame\n"
        "without firing the shutter.\n"
        "\n"
        "Options:\n"
        "  --out PATH         Output file (directory is created if missing).\n"
        "  --live-view-out PATH\n"
        "                     Write one SDK live-view JPEG frame to PATH without\n"
        "                     firing the shutter. --live-view is accepted as an alias.\n"
        "  --live-view-stream-out PATH\n"
        "                     Keep one SDK session open and refresh PATH with live-view\n"
        "                     JPEG frames until SIGTERM/SIGINT.\n"
        "  --live-view-interval-ms MS\n"
        "                     Delay between stream frame polls (default 250).\n"
        "  --timeout SECONDS  Per-stage timeout (default 30).\n"
        "  --exposure-program VALUE\n"
        "                     Set exposure program before capture. Accepts M/manual,\n"
        "                     P/program, A/aperture-priority, or S/shutter-priority.\n"
        "                     --set-exposure-program is accepted as an alias.\n"
        "  --manual-exposure  Alias for --exposure-program M.\n"
        "  --iso VALUE        Set ISO sensitivity before capture. Use 100or125\n"
        "                     for scans: ISO 100 preferred, ISO 125 fallback.\n"
        "                     --set-iso is accepted as an alias.\n"
        "  --shutter-speed VALUE\n"
        "                     Set still shutter speed before capture. Accepts 1/4,\n"
        "                     0.25, 1, or a raw SDK integer (0x...).\n"
        "  --set-shutter-speed VALUE\n"
        "                     Connect, set still shutter speed, then exit unless\n"
        "                     combined with --out. Alias: --shutter-speed.\n"
        "  --list-shutter-speeds\n"
        "                     Connect and print current/candidate shutter speeds.\n"
        "  --list-capture-settings\n"
        "                     Connect and print current/candidate exposure program,\n"
        "                     ISO, and shutter-speed settings.\n"
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
        "  --mac-address MAC  Optional camera MAC address (six colon-hex bytes).\n"
        "                     Falls back to env SONY_MAC.\n"
        "  --transfer-mode MODE\n"
        "                     host-pc: use CrSdkControlMode_Remote + SetSaveInfo and\n"
        "                     wait for the SDK auto-download callback (default).\n"
        "                     remote-transfer: shoot, then pull newest card file via\n"
        "                     RemoteTransfer contents APIs.\n"
        "  --host-pc          Alias for --transfer-mode host-pc.\n"
        "  --remote-transfer  Alias for --transfer-mode remote-transfer.\n"
        "  --list             Enumerate SDK-visible cameras, print connection info,\n"
        "                     and exit without connecting or firing the shutter.\n"
        "  --connect-only     Connect, complete Access Auth/fingerprint caching,\n"
        "                     then disconnect without firing the shutter.\n"
        "                     --probe is accepted as an alias.\n"
        "  --status           Connect, print key camera/RemoteTransfer properties,\n"
        "                     then disconnect without firing the shutter.\n"
        "  -h, --help         Show this message.\n";
}

bool parse_args(int argc, char** argv, Args& a) {
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--out") {
            if (i + 1 >= argc) { log_err("--out requires a value"); return false; }
            a.out = argv[++i];
        } else if (arg == "--live-view-out" || arg == "--live-view") {
            if (i + 1 >= argc) { log_err(arg + " requires a value"); return false; }
            a.live_view_out = argv[++i];
        } else if (arg == "--live-view-stream-out" || arg == "--live-view-stream") {
            if (i + 1 >= argc) { log_err(arg + " requires a value"); return false; }
            a.live_view_stream_out = argv[++i];
        } else if (arg == "--live-view-interval-ms") {
            if (i + 1 >= argc) { log_err("--live-view-interval-ms requires a value"); return false; }
            a.live_view_interval_ms = std::atoi(argv[++i]);
            if (a.live_view_interval_ms < 50 || a.live_view_interval_ms > 5000) {
                log_err("--live-view-interval-ms must be between 50 and 5000");
                return false;
            }
        } else if (arg == "--timeout") {
            if (i + 1 >= argc) { log_err("--timeout requires a value"); return false; }
            a.timeout_s = std::atoi(argv[++i]);
            if (a.timeout_s <= 0) { log_err("--timeout must be positive"); return false; }
        } else if (arg == "--exposure-program" || arg == "--set-exposure-program") {
            if (i + 1 >= argc) { log_err(arg + " requires a value"); return false; }
            a.exposure_program = argv[++i];
        } else if (arg == "--manual-exposure") {
            a.exposure_program = "M";
        } else if (arg == "--iso" || arg == "--set-iso") {
            if (i + 1 >= argc) { log_err(arg + " requires a value"); return false; }
            a.iso = argv[++i];
        } else if (arg == "--shutter-speed" || arg == "--set-shutter-speed") {
            if (i + 1 >= argc) { log_err(arg + " requires a value"); return false; }
            a.shutter_speed = argv[++i];
        } else if (arg == "--list-capture-settings") {
            a.list_capture_settings = true;
        } else if (arg == "--list-shutter-speeds") {
            a.list_shutter_speeds = true;
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
        } else if (arg == "--transfer-mode") {
            if (i + 1 >= argc) { log_err("--transfer-mode requires a value"); return false; }
            std::string mode = argv[++i];
            if (mode == "remote-transfer") {
                a.transfer_mode = TransferMode::RemoteTransfer;
            } else if (mode == "host-pc") {
                a.transfer_mode = TransferMode::HostPc;
            } else {
                log_err("--transfer-mode must be remote-transfer or host-pc");
                return false;
            }
        } else if (arg == "--host-pc") {
            a.transfer_mode = TransferMode::HostPc;
        } else if (arg == "--remote-transfer") {
            a.transfer_mode = TransferMode::RemoteTransfer;
        } else if (arg == "--list" || arg == "--list-cameras") {
            a.list_cameras = true;
        } else if (arg == "--connect-only" || arg == "--probe") {
            a.connect_only = true;
        } else if (arg == "--status") {
            a.status_only = true;
        } else if (arg == "-h" || arg == "--help") {
            print_usage();
            std::exit(0);
        } else {
            log_err("unknown argument: " + arg);
            return false;
        }
    }
    const int output_modes =
        (a.out.empty() ? 0 : 1)
        + (a.live_view_out.empty() ? 0 : 1)
        + (a.live_view_stream_out.empty() ? 0 : 1);
    if (output_modes > 1) {
        log_err("--out, --live-view-out, and --live-view-stream-out are mutually exclusive");
        return false;
    }
    if (a.out.empty() && a.live_view_out.empty() && a.live_view_stream_out.empty()
        && !a.list_cameras && !a.connect_only && !a.status_only
        && !a.list_capture_settings && !a.list_shutter_speeds
        && a.exposure_program.empty() && a.iso.empty() && a.shutter_speed.empty()) {
        log_err("--out is required unless --live-view-out, --live-view-stream-out, --list, --connect-only, --status, --list-capture-settings, --list-shutter-speeds, or a camera-setting setter is used");
        return false;
    }
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
    // USB bodies without Access Authentication keep the legacy 'admin'
    // behavior. Direct-IP/SSH bodies should send null credentials unless the
    // operator provided Access Auth values, matching SonShell's connection
    // path.
    if (a.user.empty() && a.ip_address.empty()) a.user = "admin";
    return true;
}

std::string camera_text(CrChar* value) {
    if (value == nullptr) return "";
    return reinterpret_cast<const char*>(value);
}

std::string camera_text(CrChar* value, CrInt32u size) {
    if (value == nullptr || size == 0) return "";
    const char* bytes = reinterpret_cast<const char*>(value);
    std::size_t n = 0;
    while (n < size && bytes[n] != '\0') ++n;
    return std::string(bytes, n);
}

std::string ip_to_string(CrInt32u ip) {
    if (ip == 0) return "";
    std::ostringstream out;
    out << (ip & 0xff)
        << "." << ((ip >> 8) & 0xff)
        << "." << ((ip >> 16) & 0xff)
        << "." << ((ip >> 24) & 0xff);
    return out.str();
}

std::string mac_to_string(const SDK::ICrCameraObjectInfo* info) {
    if (info == nullptr) return "";

    std::string mac = camera_text(info->GetMACAddressChar(), info->GetMACAddressCharSize());
    if (!mac.empty()) return mac;

    CrInt8u* raw = info->GetMACAddress();
    CrInt32u size = info->GetMACAddressSize();
    if (raw == nullptr || size < 6) return "";

    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (int i = 0; i < 6; ++i) {
        if (i > 0) out << ":";
        out << std::setw(2) << static_cast<unsigned>(raw[i]);
    }
    return out.str();
}

void print_camera_info(const SDK::ICrCameraObjectInfo* info, CrInt32u index) {
    if (info == nullptr) return;

    std::string ip = camera_text(info->GetIPAddressChar(), info->GetIPAddressCharSize());
    if (ip.empty()) ip = ip_to_string(info->GetIPAddress());

    std::cout
        << "[" << index << "]"
        << " model=" << (camera_text(info->GetModel()).empty() ? "-" : camera_text(info->GetModel()))
        << " name=" << (camera_text(info->GetName()).empty() ? "-" : camera_text(info->GetName()))
        << " connection=" << (camera_text(info->GetConnectionTypeName()).empty() ? "-" : camera_text(info->GetConnectionTypeName()))
        << " adapter=" << (camera_text(info->GetAdaptorName()).empty() ? "-" : camera_text(info->GetAdaptorName()))
        << " ip=" << (ip.empty() ? "-" : ip)
        << " mac=" << (mac_to_string(info).empty() ? "-" : mac_to_string(info))
        << " ssh=" << (info->GetSSHsupport() == SDK::CrSSHsupport_ON ? "on" : "off")
        << "\n";
}

int list_cameras() {
    if (!SDK::Init(0)) {
        log_err("SDK::Init failed");
        return 1;
    }

    SDK::ICrEnumCameraObjectInfo* enum_info = nullptr;
    SDK::CrError rc = SDK::EnumCameraObjects(&enum_info, 2);
    if (CR_FAILED(rc)) {
        log_err("EnumCameraObjects failed (CrError "
                + std::to_string(static_cast<unsigned>(rc)) + ")");
        SDK::Release();
        return 1;
    }
    if (enum_info == nullptr) {
        std::cout << "no cameras found\n";
        SDK::Release();
        return 0;
    }

    const auto count = enum_info->GetCount();
    if (count == 0) {
        std::cout << "no cameras found\n";
    } else {
        for (CrInt32u i = 0; i < count; ++i) {
            print_camera_info(enum_info->GetCameraObjectInfo(i), i);
        }
    }

    enum_info->Release();
    SDK::Release();
    return 0;
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

bool tcp_port_reachable(
    const std::string& ip_address,
    int port,
    int timeout_ms,
    std::string& error
) {
    int sock = ::socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        error = "socket() failed: " + std::string(std::strerror(errno));
        return false;
    }

    auto close_sock = [&]() {
        if (sock >= 0) {
            ::close(sock);
            sock = -1;
        }
    };

    int flags = ::fcntl(sock, F_GETFL, 0);
    if (flags < 0 || ::fcntl(sock, F_SETFL, flags | O_NONBLOCK) < 0) {
        error = "fcntl(O_NONBLOCK) failed: " + std::string(std::strerror(errno));
        close_sock();
        return false;
    }

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::inet_pton(AF_INET, ip_address.c_str(), &addr.sin_addr) != 1) {
        error = "invalid IPv4 address: " + ip_address;
        close_sock();
        return false;
    }

    int rc = ::connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    if (rc == 0) {
        close_sock();
        return true;
    }
    if (errno != EINPROGRESS) {
        error = "connect(" + ip_address + ":" + std::to_string(port) + ") failed: "
            + std::string(std::strerror(errno));
        close_sock();
        return false;
    }

    fd_set writefds;
    FD_ZERO(&writefds);
    FD_SET(sock, &writefds);

    timeval tv{};
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;

    rc = ::select(sock + 1, nullptr, &writefds, nullptr, &tv);
    if (rc == 0) {
        error = "timed out connecting to " + ip_address + ":" + std::to_string(port);
        close_sock();
        return false;
    }
    if (rc < 0) {
        error = "select() failed: " + std::string(std::strerror(errno));
        close_sock();
        return false;
    }

    int socket_error = 0;
    socklen_t socket_error_len = sizeof(socket_error);
    if (::getsockopt(sock, SOL_SOCKET, SO_ERROR, &socket_error, &socket_error_len) < 0) {
        error = "getsockopt(SO_ERROR) failed: " + std::string(std::strerror(errno));
        close_sock();
        return false;
    }
    close_sock();

    if (socket_error != 0) {
        error = "connect(" + ip_address + ":" + std::to_string(port) + ") failed: "
            + std::string(std::strerror(socket_error));
        return false;
    }
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

bool trigger_full_shutter_press(SDK::CrDeviceHandle handle) {
    SDK::CrDeviceProperty s1;
    s1.SetCode(SDK::CrDevicePropertyCode::CrDeviceProperty_S1);
    s1.SetValueType(SDK::CrDataType::CrDataType_UInt16);
    s1.SetCurrentValue(SDK::CrLockIndicator::CrLockIndicator_Locked);
    auto s1_lock_err = SDK::SetDeviceProperty(handle, &s1);
    if (CR_FAILED(s1_lock_err)) {
        log_err("SetDeviceProperty(S1 locked) failed (CrError "
                + std::to_string(static_cast<unsigned>(s1_lock_err)) + ")");
        return false;
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    const auto rc_down = SDK::SendCommand(
        handle, SDK::CrCommandId_Release, SDK::CrCommandParam_Down);
    std::this_thread::sleep_for(std::chrono::milliseconds(35));
    const auto rc_up = SDK::SendCommand(
        handle, SDK::CrCommandId_Release, SDK::CrCommandParam_Up);

    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    s1.SetCurrentValue(SDK::CrLockIndicator::CrLockIndicator_Unlocked);
    auto s1_unlock_err = SDK::SetDeviceProperty(handle, &s1);

    if (CR_FAILED(rc_down) || CR_FAILED(rc_up)) {
        const auto first_err = CR_FAILED(rc_down) ? rc_down : rc_up;
        log_err("SendCommand(Release) failed (CrError "
                + std::to_string(static_cast<unsigned>(first_err)) + ")");
        return false;
    }
    if (CR_FAILED(s1_unlock_err)) {
        log_err("SetDeviceProperty(S1 unlocked) failed (CrError "
                + std::to_string(static_cast<unsigned>(s1_unlock_err)) + ")");
        return false;
    }
    return true;
}

bool ensure_live_view_enabled(SDK::CrDeviceHandle handle) {
    CrInt32u current = 0;
    const auto get_rc = SDK::GetDeviceSetting(handle, SDK::Setting_Key_EnableLiveView, &current);
    if (CR_FAILED(get_rc)) {
        log_err("GetDeviceSetting(EnableLiveView) failed (CrError "
                + std::to_string(static_cast<unsigned>(get_rc))
                + "); trying live view anyway");
        return true;
    }

    if (current != SDK::CrDeviceSetting_Disable) {
        return true;
    }

    const auto set_rc = SDK::SetDeviceSetting(
        handle,
        SDK::Setting_Key_EnableLiveView,
        SDK::CrDeviceSetting_Enable);
    if (CR_FAILED(set_rc)) {
        log_err("SetDeviceSetting(EnableLiveView) failed (CrError "
                + std::to_string(static_cast<unsigned>(set_rc)) + ")");
        return false;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    return true;
}

bool capture_live_view_jpeg(SDK::CrDeviceHandle handle, const fs::path& out_path) {
    CrInt32 property_count = 0;
    SDK::CrLiveViewProperty* properties = nullptr;
    auto rc = SDK::GetLiveViewProperties(handle, &properties, &property_count);
    if (CR_FAILED(rc)) {
        log_err("GetLiveViewProperties failed (CrError "
                + std::to_string(static_cast<unsigned>(rc)) + ")");
        return false;
    }
    if (properties != nullptr) {
        SDK::ReleaseLiveViewProperties(handle, properties);
    }

    SDK::CrImageInfo image_info;
    rc = SDK::GetLiveViewImageInfo(handle, &image_info);
    if (CR_FAILED(rc)) {
        log_err("GetLiveViewImageInfo failed (CrError "
                + std::to_string(static_cast<unsigned>(rc)) + ")");
        return false;
    }

    const CrInt32u buffer_size = image_info.GetBufferSize();
    if (buffer_size == 0) {
        log_err("GetLiveViewImageInfo returned a zero-byte buffer");
        return false;
    }

    std::vector<CrInt8u> image_buffer(buffer_size);
    SDK::CrImageDataBlock image_data;

    SDK::CrError last_rc = SDK::CrError_None;
    bool have_frame = false;
    for (int attempt = 0; attempt < 5; ++attempt) {
        image_data.SetSize(buffer_size);
        image_data.SetData(image_buffer.data());
        last_rc = SDK::GetLiveViewImage(handle, &image_data);
        if (CR_SUCCEEDED(last_rc) && image_data.GetImageSize() > 0
            && image_data.GetImageData() != nullptr) {
            have_frame = true;
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(120));
    }

    if (!have_frame) {
        if (CR_FAILED(last_rc)) {
            log_err("GetLiveViewImage failed (CrError "
                    + std::to_string(static_cast<unsigned>(last_rc)) + ")");
        } else {
            log_err("GetLiveViewImage returned no JPEG data");
        }
        return false;
    }

    const fs::path parent = out_path.parent_path();
    std::error_code ec;
    if (!parent.empty()) {
        fs::create_directories(parent, ec);
        if (ec) {
            log_err("could not create live-view output directory "
                    + parent.string() + ": " + ec.message());
            return false;
        }
    }

    fs::path tmp_path = out_path;
    tmp_path += ".tmp." + std::to_string(::getpid());

    std::string write_error;
    if (!write_binary_file(
            tmp_path,
            reinterpret_cast<const char*>(image_data.GetImageData()),
            image_data.GetImageSize(),
            write_error)) {
        log_err(write_error);
        return false;
    }

    fs::rename(tmp_path, out_path, ec);
    if (ec) {
        std::error_code remove_ec;
        fs::remove(tmp_path, remove_ec);
        log_err("rename " + tmp_path.string() + " -> " + out_path.string()
                + " failed: " + ec.message());
        return false;
    }

    return true;
}

bool write_live_view_jpeg(SDK::CrDeviceHandle handle, const fs::path& out_path) {
    if (!ensure_live_view_enabled(handle)) {
        return false;
    }
    return capture_live_view_jpeg(handle, out_path);
}

int run_live_view_stream(
    SDK::CrDeviceHandle handle,
    const fs::path& out_path,
    int interval_ms
) {
    if (!ensure_live_view_enabled(handle)) {
        return 1;
    }

    bool announced = false;
    int consecutive_failures = 0;
    while (!g_stop_requested.load()) {
        if (capture_live_view_jpeg(handle, out_path)) {
            consecutive_failures = 0;
            if (!announced) {
                std::cout << out_path.string() << std::endl;
                announced = true;
            }
        } else {
            ++consecutive_failures;
            if (consecutive_failures >= 5) {
                log_err("live-view stream stopped after repeated frame failures");
                return 1;
            }
        }

        const auto sleep_until = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(interval_ms);
        while (!g_stop_requested.load() && std::chrono::steady_clock::now() < sleep_until) {
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }
    }

    return 0;
}

std::string normalize_shutter_label(std::string s) {
    s.erase(std::remove_if(s.begin(), s.end(), [](unsigned char c) {
        return std::isspace(c) || c == '"';
    }), s.end());
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    for (const std::string suffix : {"seconds", "second", "secs", "sec", "s"}) {
        if (s.size() > suffix.size()
            && s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0) {
            s.resize(s.size() - suffix.size());
            break;
        }
    }
    return s;
}

std::string format_shutter_speed32(CrInt32u shutter_speed) {
    if (shutter_speed == SDK::CrShutterSpeed_Bulb) return "Bulb";
    if (shutter_speed == SDK::CrShutterSpeed_Nothing) return "Nothing";

    const CrInt16u numerator = static_cast<CrInt16u>((shutter_speed >> 16) & 0xffff);
    const CrInt16u denominator = static_cast<CrInt16u>(shutter_speed & 0xffff);
    if (denominator == 0) return std::to_string(shutter_speed);

    std::ostringstream ts;
    if (numerator == 1) {
        ts << numerator << "/" << denominator;
    } else if (numerator % denominator == 0) {
        ts << (numerator / denominator);
    } else {
        ts << static_cast<double>(numerator) / static_cast<double>(denominator);
    }
    return ts.str();
}

std::string format_shutter_speed64(CrInt64u shutter_speed) {
    const CrInt32u numerator = static_cast<CrInt32u>((shutter_speed >> 32) & 0xffffffff);
    const CrInt32u denominator = static_cast<CrInt32u>(shutter_speed & 0xffffffff);
    if (denominator == 0) return std::to_string(shutter_speed);

    std::ostringstream ts;
    if (numerator == 1) {
        ts << numerator << "/" << denominator;
    } else if (numerator % denominator == 0) {
        ts << (numerator / denominator);
    } else {
        ts << static_cast<double>(numerator) / static_cast<double>(denominator);
    }
    return ts.str();
}

bool parse_shutter_fraction(
    const std::string& input,
    CrInt32u& numerator,
    CrInt32u& denominator
) {
    std::string s = normalize_shutter_label(input);
    if (s.empty()) return false;

    const auto slash = s.find('/');
    if (slash != std::string::npos) {
        try {
            numerator = static_cast<CrInt32u>(std::stoul(s.substr(0, slash)));
            denominator = static_cast<CrInt32u>(std::stoul(s.substr(slash + 1)));
            return numerator > 0 && denominator > 0;
        } catch (...) {
            return false;
        }
    }

    try {
        double seconds = std::stod(s);
        if (!(seconds > 0.0) || seconds > 65535.0) return false;
        constexpr CrInt32u scale = 1000000;
        auto scaled = static_cast<CrInt64u>(seconds * scale + 0.5);
        if (scaled == 0) scaled = 1;
        auto g = std::gcd(static_cast<CrInt64u>(scaled), static_cast<CrInt64u>(scale));
        numerator = static_cast<CrInt32u>(scaled / g);
        denominator = static_cast<CrInt32u>(scale / g);
        return numerator > 0 && denominator > 0;
    } catch (...) {
        return false;
    }
}

bool parse_raw_u64(const std::string& input, CrInt64u& out) {
    std::string s = normalize_shutter_label(input);
    if (s.empty()) return false;
    char* end = nullptr;
    errno = 0;
    unsigned long long value = std::strtoull(s.c_str(), &end, 0);
    if (errno != 0 || end == s.c_str() || *end != '\0') return false;
    out = static_cast<CrInt64u>(value);
    return true;
}

bool shutter_value_seconds(CrInt64u value, bool is64, double& seconds) {
    const CrInt64u numerator = is64
        ? ((value >> 32) & 0xffffffff)
        : ((value >> 16) & 0xffff);
    const CrInt64u denominator = is64
        ? (value & 0xffffffff)
        : (value & 0xffff);
    if (numerator == 0 || denominator == 0) return false;
    seconds = static_cast<double>(numerator) / static_cast<double>(denominator);
    return true;
}

std::vector<CrInt64u> property_values(const SDK::CrDeviceProperty& prop, std::size_t width) {
    std::vector<CrInt64u> values;
    const auto bytes = prop.GetValues();
    const auto size = prop.GetValueSize();
    if (bytes == nullptr || width == 0 || size == 0) return values;
    const auto count = size / width;
    values.reserve(count);
    for (CrInt32u i = 0; i < count; ++i) {
        CrInt64u value = 0;
        std::memcpy(&value, bytes + i * width, width);
        values.push_back(value);
    }
    return values;
}

bool find_shutter_value_for_property(
    const SDK::CrDeviceProperty& prop,
    const std::string& requested,
    bool is64,
    CrInt64u& out_value
) {
    CrInt32u numerator = 0;
    CrInt32u denominator = 0;
    const bool have_fraction = parse_shutter_fraction(requested, numerator, denominator);
    const double requested_seconds = have_fraction
        ? static_cast<double>(numerator) / static_cast<double>(denominator)
        : 0.0;
    const CrInt64u encoded = is64
        ? ((static_cast<CrInt64u>(numerator) << 32) | denominator)
        : ((static_cast<CrInt64u>(numerator) << 16) | denominator);

    CrInt64u raw = 0;
    const bool have_raw = parse_raw_u64(requested, raw);
    const auto wanted_label = normalize_shutter_label(requested);
    const auto values = property_values(prop, is64 ? sizeof(CrInt64u) : sizeof(CrInt32u));

    for (CrInt64u value : values) {
        const auto label = normalize_shutter_label(
            is64 ? format_shutter_speed64(value) : format_shutter_speed32(static_cast<CrInt32u>(value))
        );
        double candidate_seconds = 0.0;
        if ((have_fraction && value == encoded)
            || (have_fraction
                && shutter_value_seconds(value, is64, candidate_seconds)
                && std::fabs(candidate_seconds - requested_seconds) <= 1e-9)
            || (have_raw && value == raw)
            || (!wanted_label.empty() && label == wanted_label)) {
            out_value = value;
            return true;
        }
    }

    if (values.empty()) {
        if (have_fraction) {
            out_value = encoded;
            return true;
        }
        if (have_raw) {
            out_value = raw;
            return true;
        }
    }
    return false;
}

bool get_select_property(
    SDK::CrDeviceHandle handle,
    CrInt32u code,
    SDK::CrDeviceProperty*& prop,
    CrInt32& count
) {
    prop = nullptr;
    count = 0;
    SDK::CrError rc = SDK::GetSelectDeviceProperties(handle, 1, &code, &prop, &count);
    if (CR_FAILED(rc) || prop == nullptr || count == 0) {
        if (prop != nullptr) SDK::ReleaseDeviceProperties(handle, prop);
        prop = nullptr;
        return false;
    }
    return true;
}

std::string normalize_setting_token(std::string s) {
    s.erase(std::remove_if(s.begin(), s.end(), [](unsigned char c) {
        return std::isspace(c) || c == '-' || c == '_';
    }), s.end());
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return s;
}

std::string format_iso_sensitivity(CrInt32u iso) {
    const CrInt32u mode = (iso >> 24) & 0x0000000f;
    const CrInt32u value = iso & 0x00ffffff;

    std::ostringstream out;
    if (mode == SDK::CrISO_MultiFrameNR) {
        out << "MultiFrameNR ";
    } else if (mode == SDK::CrISO_MultiFrameNR_High) {
        out << "MultiFrameNRHigh ";
    }

    if (value == SDK::CrISO_AUTO) {
        out << "ISO AUTO";
    } else {
        out << "ISO " << value;
    }
    return out.str();
}

std::string format_exposure_program_mode(CrInt32u mode) {
    switch (mode) {
    case SDK::CrExposure_M_Manual:
        return "M/manual";
    case SDK::CrExposure_P_Auto:
        return "P/program";
    case SDK::CrExposure_A_AperturePriority:
        return "A/aperture-priority";
    case SDK::CrExposure_S_ShutterSpeedPriority:
        return "S/shutter-priority";
    case SDK::CrExposure_Auto:
        return "auto";
    case SDK::CrExposure_Auto_Plus:
        return "auto-plus";
    case SDK::CrExposure_P_A:
        return "program-aperture-shift";
    case SDK::CrExposure_P_S:
        return "program-shutter-shift";
    default:
        return std::to_string(mode);
    }
}

bool parse_iso_value(const std::string& requested, CrInt32u& out_value) {
    std::string s = normalize_setting_token(requested);
    if (s.rfind("iso", 0) == 0) s.erase(0, 3);
    if (s == "auto") {
        out_value = SDK::CrISO_AUTO;
        return true;
    }
    if (s.empty()) return false;

    char* end = nullptr;
    errno = 0;
    unsigned long value = std::strtoul(s.c_str(), &end, 0);
    if (errno != 0 || end == s.c_str() || *end != '\0'
        || value > std::numeric_limits<CrInt32u>::max()) {
        return false;
    }
    out_value = static_cast<CrInt32u>(value);
    return true;
}

bool is_lowest_iso_request(const std::string& requested) {
    const std::string s = normalize_setting_token(requested);
    return s == "lowest" || s == "low" || s == "min" || s == "base" || s == "fixedlow";
}

bool is_base_100_or_125_iso_request(const std::string& requested) {
    const std::string s = normalize_setting_token(requested);
    return s == "100or125" || s == "100/125" || s == "base100" ||
        s == "nativebase" || s == "scanbase";
}

CrInt32u iso_numeric_value(CrInt32u iso) {
    return iso & 0x00ffffff;
}

bool find_iso_candidate(
    const SDK::CrDeviceProperty& prop,
    const std::string& requested,
    CrInt32u& out_value
) {
    const auto values = property_values(prop, sizeof(CrInt32u));
    if (is_base_100_or_125_iso_request(requested)) {
        if (values.empty()) {
            out_value = 100;
            return true;
        }

        for (CrInt32u wanted : {static_cast<CrInt32u>(100), static_cast<CrInt32u>(125)}) {
            for (CrInt64u raw_value : values) {
                const auto iso = static_cast<CrInt32u>(raw_value);
                if (iso == wanted || iso_numeric_value(iso) == wanted) {
                    out_value = iso;
                    return true;
                }
            }
        }
        return false;
    }

    if (is_lowest_iso_request(requested)) {
        bool found = false;
        CrInt32u best = 0;
        CrInt32u best_numeric = std::numeric_limits<CrInt32u>::max();
        for (CrInt64u raw_value : values) {
            const auto iso = static_cast<CrInt32u>(raw_value);
            const auto numeric = iso_numeric_value(iso);
            if (numeric == SDK::CrISO_AUTO) continue;
            if (!found || numeric < best_numeric) {
                found = true;
                best = iso;
                best_numeric = numeric;
            }
        }
        if (found) {
            out_value = best;
            return true;
        }
        return false;
    }

    CrInt32u requested_value = 0;
    if (!parse_iso_value(requested, requested_value)) {
        return false;
    }
    if (values.empty()) {
        out_value = requested_value;
        return true;
    }
    for (CrInt64u raw_value : values) {
        const auto iso = static_cast<CrInt32u>(raw_value);
        if (iso == requested_value || iso_numeric_value(iso) == requested_value) {
            out_value = iso;
            return true;
        }
    }
    return false;
}

bool parse_exposure_program_mode(const std::string& requested, CrInt16u& out_value) {
    const std::string s = normalize_setting_token(requested);
    if (s == "m" || s == "manual" || s == "manualexposure") {
        out_value = static_cast<CrInt16u>(SDK::CrExposure_M_Manual);
        return true;
    }
    if (s == "p" || s == "program" || s == "programauto" || s == "pauto") {
        out_value = static_cast<CrInt16u>(SDK::CrExposure_P_Auto);
        return true;
    }
    if (s == "a" || s == "aperture" || s == "aperturepriority") {
        out_value = static_cast<CrInt16u>(SDK::CrExposure_A_AperturePriority);
        return true;
    }
    if (s == "s" || s == "shutter" || s == "shutterpriority") {
        out_value = static_cast<CrInt16u>(SDK::CrExposure_S_ShutterSpeedPriority);
        return true;
    }

    CrInt64u raw = 0;
    if (!parse_raw_u64(requested, raw) || raw > std::numeric_limits<CrInt16u>::max()) {
        return false;
    }
    out_value = static_cast<CrInt16u>(raw);
    return true;
}

bool property_candidate_contains(
    const SDK::CrDeviceProperty& prop,
    std::size_t width,
    CrInt64u wanted
) {
    const auto values = property_values(prop, width);
    if (values.empty()) return true;
    return std::find(values.begin(), values.end(), wanted) != values.end();
}

bool set_exposure_program_mode(SDK::CrDeviceHandle handle, const std::string& requested) {
    CrInt16u selected = 0;
    if (!parse_exposure_program_mode(requested, selected)) {
        log_err("invalid exposure program: " + requested);
        return false;
    }

    SDK::CrDeviceProperty* props = nullptr;
    CrInt32 count = 0;
    if (!get_select_property(handle, SDK::CrDeviceProperty_ExposureProgramMode, props, count)) {
        log_err("camera did not expose exposure-program mode");
        return false;
    }

    const bool writable = props[0].IsSetEnableCurrentValue();
    const bool matched = property_candidate_contains(props[0], sizeof(CrInt32u), selected);
    const auto current = static_cast<CrInt32u>(props[0].GetCurrentValue());
    SDK::ReleaseDeviceProperties(handle, props);

    if (!writable) {
        log_err("exposure program is not writable over the Sony SDK (current="
                + format_exposure_program_mode(current) + " raw=" + std::to_string(current) + ")");
        return false;
    }
    if (!matched) {
        log_err("requested exposure program is not in the camera's candidate list: " + requested);
        return false;
    }

    SDK::CrDeviceProperty prop;
    prop.SetCode(SDK::CrDevicePropertyCode::CrDeviceProperty_ExposureProgramMode);
    prop.SetCurrentValue(selected);
    prop.SetValueType(SDK::CrDataType::CrDataType_UInt16Array);
    auto rc = SDK::SetDeviceProperty(handle, &prop);
    if (CR_FAILED(rc)) {
        log_err("SetDeviceProperty(ExposureProgramMode) failed (CrError "
                + std::to_string(static_cast<unsigned>(rc)) + ")");
        return false;
    }

    std::cout << "exposure_program=" << format_exposure_program_mode(selected) << "\n";
    return true;
}

bool set_iso_sensitivity(SDK::CrDeviceHandle handle, const std::string& requested) {
    SDK::CrDeviceProperty* props = nullptr;
    CrInt32 count = 0;
    if (!get_select_property(handle, SDK::CrDeviceProperty_IsoSensitivity, props, count)) {
        log_err("camera did not expose ISO sensitivity");
        return false;
    }

    CrInt32u selected = 0;
    const bool matched = find_iso_candidate(props[0], requested, selected);
    const bool writable = props[0].IsSetEnableCurrentValue();
    const auto current = static_cast<CrInt32u>(props[0].GetCurrentValue());
    SDK::ReleaseDeviceProperties(handle, props);

    if (!matched) {
        if (is_lowest_iso_request(requested)) {
            log_err("camera did not report any fixed ISO candidates");
        } else if (is_base_100_or_125_iso_request(requested)) {
            log_err("camera did not report ISO 100 or ISO 125 candidates");
        } else if (!parse_iso_value(requested, selected)) {
            log_err("invalid ISO sensitivity: " + requested);
        } else {
            log_err("requested ISO is not in the camera's candidate list: " + requested);
        }
        return false;
    }

    if (!writable) {
        log_err("ISO sensitivity is not writable over the Sony SDK (current="
                + format_iso_sensitivity(current) + " raw=" + std::to_string(current) + ")");
        return false;
    }

    SDK::CrDeviceProperty prop;
    prop.SetCode(SDK::CrDevicePropertyCode::CrDeviceProperty_IsoSensitivity);
    prop.SetCurrentValue(selected);
    prop.SetValueType(SDK::CrDataType::CrDataType_UInt32Array);
    auto rc = SDK::SetDeviceProperty(handle, &prop);
    if (CR_FAILED(rc)) {
        log_err("SetDeviceProperty(IsoSensitivity) failed (CrError "
                + std::to_string(static_cast<unsigned>(rc)) + ")");
        return false;
    }

    std::cout << "iso=" << format_iso_sensitivity(selected) << "\n";
    return true;
}

bool set_shutter_speed(SDK::CrDeviceHandle handle, const std::string& requested) {
    struct Candidate {
        CrInt32u code;
        SDK::CrDataType value_type;
        bool is64;
        const char* label;
    };
    const Candidate candidates[] = {
        {SDK::CrDeviceProperty_ShutterSpeed, SDK::CrDataType::CrDataType_UInt32Array, false, "ShutterSpeed"},
        {SDK::CrDeviceProperty_ShutterSpeedValue, SDK::CrDataType::CrDataType_UInt64Array, true, "ShutterSpeedValue"},
        {SDK::CrDeviceProperty_ExtendedShutterSpeed, SDK::CrDataType::CrDataType_UInt64Array, true, "ExtendedShutterSpeed"},
    };

    bool found_property = false;
    for (const auto& candidate : candidates) {
        SDK::CrDeviceProperty* props = nullptr;
        CrInt32 count = 0;
        if (!get_select_property(handle, candidate.code, props, count)) continue;
        found_property = true;

        const bool writable = props[0].IsSetEnableCurrentValue();
        CrInt64u selected = 0;
        const bool matched = find_shutter_value_for_property(
            props[0], requested, candidate.is64, selected);
        SDK::ReleaseDeviceProperties(handle, props);

        if (!writable || !matched) continue;

        SDK::CrDeviceProperty prop;
        prop.SetCode(static_cast<SDK::CrDevicePropertyCode>(candidate.code));
        prop.SetCurrentValue(selected);
        prop.SetValueType(candidate.value_type);
        auto rc = SDK::SetDeviceProperty(handle, &prop);
        if (CR_FAILED(rc)) {
            log_err(std::string("SetDeviceProperty(") + candidate.label + ") failed (CrError "
                    + std::to_string(static_cast<unsigned>(rc)) + ")");
            continue;
        }

        std::cout << "shutter_speed=" << requested << "\n";
        return true;
    }

    log_err(
        found_property
            ? "requested shutter speed is not writable or not in the camera's candidate list: " + requested
            : "camera did not expose a writable shutter-speed property"
    );
    return false;
}

bool print_shutter_speeds(SDK::CrDeviceHandle handle) {
    struct Candidate {
        CrInt32u code;
        bool is64;
        const char* label;
    };
    const Candidate candidates[] = {
        {SDK::CrDeviceProperty_ShutterSpeed, false, "shutterSpeed"},
        {SDK::CrDeviceProperty_ShutterSpeedValue, true, "shutterSpeedValue"},
        {SDK::CrDeviceProperty_ExtendedShutterSpeed, true, "extendedShutterSpeed"},
    };

    bool printed = false;
    for (const auto& candidate : candidates) {
        SDK::CrDeviceProperty* props = nullptr;
        CrInt32 count = 0;
        if (!get_select_property(handle, candidate.code, props, count)) continue;

        printed = true;
        const auto current = props[0].GetCurrentValue();
        std::cout << candidate.label
                  << " current="
                  << (candidate.is64
                      ? format_shutter_speed64(current)
                      : format_shutter_speed32(static_cast<CrInt32u>(current)))
                  << " raw=" << current
                  << " writable=" << (props[0].IsSetEnableCurrentValue() ? "yes" : "no")
                  << "\n";

        const auto values = property_values(
            props[0], candidate.is64 ? sizeof(CrInt64u) : sizeof(CrInt32u));
        for (CrInt64u value : values) {
            std::cout << "  "
                      << (candidate.is64
                          ? format_shutter_speed64(value)
                          : format_shutter_speed32(static_cast<CrInt32u>(value)))
                      << " raw=" << value
                      << "\n";
        }
        SDK::ReleaseDeviceProperties(handle, props);
    }
    return printed;
}

bool print_exposure_program_modes(SDK::CrDeviceHandle handle) {
    SDK::CrDeviceProperty* props = nullptr;
    CrInt32 count = 0;
    if (!get_select_property(handle, SDK::CrDeviceProperty_ExposureProgramMode, props, count)) {
        return false;
    }

    const auto current = static_cast<CrInt32u>(props[0].GetCurrentValue());
    std::cout << "exposureProgramMode current="
              << format_exposure_program_mode(current)
              << " raw=" << current
              << " writable=" << (props[0].IsSetEnableCurrentValue() ? "yes" : "no")
              << "\n";
    for (CrInt64u value : property_values(props[0], sizeof(CrInt32u))) {
        const auto mode = static_cast<CrInt32u>(value);
        std::cout << "  " << format_exposure_program_mode(mode)
                  << " raw=" << mode << "\n";
    }
    SDK::ReleaseDeviceProperties(handle, props);
    return true;
}

bool print_iso_sensitivities(SDK::CrDeviceHandle handle) {
    SDK::CrDeviceProperty* props = nullptr;
    CrInt32 count = 0;
    if (!get_select_property(handle, SDK::CrDeviceProperty_IsoSensitivity, props, count)) {
        return false;
    }

    const auto current = static_cast<CrInt32u>(props[0].GetCurrentValue());
    std::cout << "isoSensitivity current="
              << format_iso_sensitivity(current)
              << " raw=" << current
              << " writable=" << (props[0].IsSetEnableCurrentValue() ? "yes" : "no")
              << "\n";
    for (CrInt64u value : property_values(props[0], sizeof(CrInt32u))) {
        const auto iso = static_cast<CrInt32u>(value);
        std::cout << "  " << format_iso_sensitivity(iso)
                  << " raw=" << iso << "\n";
    }
    SDK::ReleaseDeviceProperties(handle, props);
    return true;
}

bool print_capture_settings(SDK::CrDeviceHandle handle) {
    bool printed = false;
    printed = print_exposure_program_modes(handle) || printed;
    printed = print_iso_sensitivities(handle) || printed;
    printed = print_shutter_speeds(handle) || printed;
    return printed;
}

std::string format_known_property(CrInt32u code, CrInt64u value) {
    if (code == SDK::CrDeviceProperty_CameraOperatingMode) {
        if (value == SDK::CrCameraOperatingMode_Record) return "record";
        if (value == SDK::CrCameraOperatingMode_Playback) return "playback";
    } else if (code == SDK::CrDeviceProperty_MovieShootingMode) {
        if (value == SDK::CrMovieShootingMode_Off) return "off";
        return "movie-enabled";
    } else if (code == SDK::CrDeviceProperty_StillImageStoreDestination) {
        if (value == SDK::CrStillImageStoreDestination_HostPC) return "host-pc";
        if (value == SDK::CrStillImageStoreDestination_MemoryCard) return "memory-card";
        if (value == SDK::CrStillImageStoreDestination_HostPCAndMemoryCard) return "host-pc-and-memory-card";
    } else if (code == SDK::CrDeviceProperty_MediaSLOT1_ContentsInfoListEnableStatus
               || code == SDK::CrDeviceProperty_MediaSLOT2_ContentsInfoListEnableStatus) {
        if (value == SDK::CrContentsInfoListEnableStatus_Enable) return "enabled";
        if (value == SDK::CrContentsInfoListEnableStatus_Disable) return "disabled";
    } else if (code == SDK::CrDeviceProperty_RecordingMedia) {
        if (value == SDK::CrRecordingMedia_Slot1) return "slot1";
        if (value == SDK::CrRecordingMedia_Slot2) return "slot2";
        if (value == SDK::CrRecordingMedia_SimultaneousRecording) return "simultaneous";
        if (value == SDK::CrRecordingMedia_SortRecording) return "sort";
    } else if (code == SDK::CrDeviceProperty_RAW_J_PC_Save_Image) {
        if (value == SDK::CrPropertyRAWJPCSaveImage_RAWAndJPEG) return "raw-and-jpeg";
        if (value == SDK::CrPropertyRAWJPCSaveImage_JPEGOnly) return "jpeg-only";
        if (value == SDK::CrPropertyRAWJPCSaveImage_RAWOnly) return "raw-only";
        if (value == SDK::CrPropertyRAWJPCSaveImage_RAWAndHEIF) return "raw-and-heif";
        if (value == SDK::CrPropertyRAWJPCSaveImage_HEIFOnly) return "heif-only";
    } else if (code == SDK::CrDeviceProperty_ExposureProgramMode) {
        return format_exposure_program_mode(static_cast<CrInt32u>(value));
    } else if (code == SDK::CrDeviceProperty_IsoSensitivity) {
        return format_iso_sensitivity(static_cast<CrInt32u>(value));
    } else if (code == SDK::CrDeviceProperty_ShutterSpeed) {
        return format_shutter_speed32(static_cast<CrInt32u>(value));
    } else if (code == SDK::CrDeviceProperty_ShutterSpeedValue
               || code == SDK::CrDeviceProperty_ExtendedShutterSpeed) {
        return format_shutter_speed64(value);
    }

    std::ostringstream out;
    out << value;
    return out.str();
}

void print_one_property(SDK::CrDeviceHandle handle, CrInt32u code, const char* label) {
    SDK::CrDeviceProperty* props = nullptr;
    CrInt32 count = 0;
    SDK::CrError err = SDK::GetSelectDeviceProperties(handle, 1, &code, &props, &count);
    if (CR_FAILED(err) || props == nullptr || count == 0) {
        std::cout << label << "=unavailable";
        if (CR_FAILED(err)) std::cout << " err=" << static_cast<unsigned>(err);
        std::cout << "\n";
        return;
    }

    CrInt64u raw = props[0].GetCurrentValue();
    std::cout << label << "=" << format_known_property(code, static_cast<CrInt64u>(raw))
              << " raw=" << raw
              << " writable=" << (props[0].IsSetEnableCurrentValue() ? "yes" : "no")
              << "\n";
    SDK::ReleaseDeviceProperties(handle, props);
}

void print_camera_status(SDK::CrDeviceHandle handle) {
    print_one_property(handle, SDK::CrDeviceProperty_CameraOperatingMode, "cameraOperatingMode");
    print_one_property(handle, SDK::CrDeviceProperty_MovieShootingMode, "movieShootingMode");
    print_one_property(handle, SDK::CrDeviceProperty_ExposureProgramMode, "exposureProgramMode");
    print_one_property(handle, SDK::CrDeviceProperty_IsoSensitivity, "isoSensitivity");
    print_one_property(handle, SDK::CrDeviceProperty_StillImageStoreDestination, "stillImageStoreDestination");
    print_one_property(handle, SDK::CrDeviceProperty_ShutterSpeed, "shutterSpeed");
    print_one_property(handle, SDK::CrDeviceProperty_ShutterSpeedValue, "shutterSpeedValue");
    print_one_property(handle, SDK::CrDeviceProperty_RAW_J_PC_Save_Image, "rawJpcSaveImage");
    print_one_property(handle, SDK::CrDeviceProperty_RecordingMedia, "recordingMedia");
    print_one_property(handle, SDK::CrDeviceProperty_MediaSLOT1_ContentsInfoListEnableStatus, "slot1ContentsInfoList");
    print_one_property(handle, SDK::CrDeviceProperty_MediaSLOT2_ContentsInfoListEnableStatus, "slot2ContentsInfoList");
    print_one_property(handle, SDK::CrDeviceProperty_MediaSLOT1_ContentsInfoListUpdateTime, "slot1ContentsUpdateTime");
    print_one_property(handle, SDK::CrDeviceProperty_MediaSLOT2_ContentsInfoListUpdateTime, "slot2ContentsUpdateTime");
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGTERM, handle_stop_signal);
    std::signal(SIGINT, handle_stop_signal);

    Args args;
    if (!parse_args(argc, argv, args)) {
        print_usage();
        return 2;
    }

    if (args.list_cameras) {
        return list_cameras();
    }

    const bool live_view_snapshot = !args.live_view_out.empty();
    const bool live_view_stream = !args.live_view_stream_out.empty();
    const bool live_view_mode = live_view_snapshot || live_view_stream;
    const bool has_capture_setting_setter =
        !args.exposure_program.empty() || !args.iso.empty() || !args.shutter_speed.empty();
    const bool capture_setting_set_only = has_capture_setting_setter && args.out.empty()
        && !args.connect_only && !args.status_only
        && !args.list_capture_settings && !args.list_shutter_speeds && !live_view_mode;
    const bool needs_capture_output = !args.connect_only && !args.status_only
        && !args.list_capture_settings && !args.list_shutter_speeds
        && !capture_setting_set_only && !live_view_mode;

    fs::path out_path;
    fs::path live_view_path;
    fs::path tmp_dir;
    std::error_code ec;
    if (live_view_mode) {
        live_view_path = fs::absolute(live_view_snapshot
            ? args.live_view_out
            : args.live_view_stream_out);
        fs::path parent = live_view_path.parent_path();
        if (parent.empty()) parent = ".";
        fs::create_directories(parent, ec);
        if (ec) {
            log_err("could not create live-view output directory "
                    + parent.string() + ": " + ec.message());
            return 1;
        }
    } else if (needs_capture_output) {
        out_path = fs::absolute(args.out);
        fs::path parent = out_path.parent_path();
        if (parent.empty()) parent = ".";

        fs::create_directories(parent, ec);
        if (ec) {
            log_err("could not create output directory " + parent.string() + ": " + ec.message());
            return 1;
        }

        // Unique scratch directory adjacent to the final output. Putting it on
        // the same filesystem as `out` means the eventual rename(2) is atomic.
        tmp_dir =
            parent / (".sony-capture-tmp-" + std::to_string(::getpid()) +
                      "-" + std::to_string(
                          std::chrono::steady_clock::now().time_since_epoch().count()));
        fs::create_directories(tmp_dir, ec);
        if (ec) {
            log_err("could not create scratch dir " + tmp_dir.string() + ": " + ec.message());
            return 1;
        }
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
        if (!tmp_dir.empty()) {
            std::error_code _ec;
            fs::remove_all(tmp_dir, _ec);
        }
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
    //   Ethernet path: prefer an enumerated SDK object that matches the
    //                  requested IP/MAC, falling back to
    //                  CreateCameraObjectInfoEthernetConnection.
    SDK::ICrEnumCameraObjectInfo* enum_info = nullptr;
    SDK::ICrCameraObjectInfo* cam_info = nullptr;
    SDK::CrError rc = 0;
    const bool use_ip = !args.ip_address.empty();
    CrInt8u mac_bytes[6] = {0};

    if (use_ip) {
        std::string tcp_error;
        if (!tcp_port_reachable(args.ip_address, 22, 2000, tcp_error)) {
            log_err("camera is not reachable over SDK SSH at " + args.ip_address
                    + ":22 (" + tcp_error + ")");
            cleanup();
            return 1;
        }

        CrInt32u ip_packed = 0;
        if (!parse_ipv4(args.ip_address, ip_packed)) {
            log_err("--ip-address is not a valid IPv4 address: " + args.ip_address);
            cleanup();
            return 1;
        }
        if (!args.mac_address.empty() && !parse_mac(args.mac_address, mac_bytes)) {
            log_err("--mac-address is not a valid MAC: " + args.mac_address);
            cleanup();
            return 1;
        }
        SDK::ICrEnumCameraObjectInfo* direct_enum = nullptr;
        rc = SDK::EnumCameraObjects(&direct_enum, 2);
        if (CR_SUCCEEDED(rc) && direct_enum != nullptr) {
            for (CrInt32u i = 0; i < direct_enum->GetCount(); ++i) {
                const SDK::ICrCameraObjectInfo* candidate = direct_enum->GetCameraObjectInfo(i);
                if (candidate == nullptr) continue;

                std::string candidate_ip = camera_text(
                    candidate->GetIPAddressChar(),
                    candidate->GetIPAddressCharSize()
                );
                if (candidate_ip.empty()) candidate_ip = ip_to_string(candidate->GetIPAddress());
                if (candidate_ip != args.ip_address) continue;

                if (!args.mac_address.empty()) {
                    std::string candidate_mac = mac_to_string(candidate);
                    std::string wanted_mac = args.mac_address;
                    std::transform(candidate_mac.begin(), candidate_mac.end(), candidate_mac.begin(), ::toupper);
                    std::transform(wanted_mac.begin(), wanted_mac.end(), wanted_mac.begin(), ::toupper);
                    if (candidate_mac != wanted_mac) continue;
                }

                cam_info = const_cast<SDK::ICrCameraObjectInfo*>(candidate);
                enum_info = direct_enum;
                direct_enum = nullptr;
                break;
            }
        }
        if (direct_enum != nullptr) {
            direct_enum->Release();
        }

        if (cam_info == nullptr) {
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

    const char* user_id = args.user.empty() ? nullptr : args.user.c_str();
    const char* user_password = args.password.empty() ? nullptr : args.password.c_str();
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
    // Host-PC mode is the working capture-and-download path on the a7CR over
    // Wi-Fi. RemoteTransfer remains available for cameras where card contents
    // listing works reliably.
    const auto control_mode = args.transfer_mode == TransferMode::HostPc
        ? SDK::CrSdkControlMode_Remote
        : SDK::CrSdkControlMode_RemoteTransfer;
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
        const bool connect_ip_error = static_cast<unsigned>(err) == 0x8202u;
        if (err != 0) {
            log_err("camera connect failed asynchronously (CrError "
                    + std::to_string(static_cast<unsigned>(err)) + ")");
            if (connect_ip_error) {
                log_err("SDK connection hint: the camera is reachable on the network, "
                        "but it refused the Sony SDK session. Confirm Network PC Remote "
                        "is active on the camera and close Imaging Edge Desktop or any "
                        "other Sony remote app before retrying.");
            }
        } else {
            log_err("timed out waiting for camera to connect");
        }
        if (ssh_support) {
            if (connect_ip_error) {
                log_err("Access Authentication hint: if Network PC Remote is active and "
                        "no other app is connected, refresh the camera's Access Auth info "
                        "and retry with the current username/password.");
            } else {
                log_err("Access Authentication hint: remove a stale fingerprint cache, "
                        "then retry with --username, --password, and --fingerprint-cache-path");
            }
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

    if (!args.exposure_program.empty()) {
        if (!set_exposure_program_mode(handle, args.exposure_program)) {
            cleanup();
            return 1;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
    }

    if (!args.iso.empty()) {
        if (!set_iso_sensitivity(handle, args.iso)) {
            cleanup();
            return 1;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
    }

    if (!args.shutter_speed.empty()) {
        if (!set_shutter_speed(handle, args.shutter_speed)) {
            cleanup();
            return 1;
        }
        if (capture_setting_set_only) {
            exit_code = 0;
            cleanup();
            return exit_code;
        }
    }

    if (capture_setting_set_only) {
        exit_code = 0;
        cleanup();
        return exit_code;
    }

    if (args.list_capture_settings) {
        if (!print_capture_settings(handle)) {
            log_err("no exposure/ISO/shutter properties available");
            cleanup();
            return 1;
        }
        exit_code = 0;
        cleanup();
        return exit_code;
    }

    if (args.list_shutter_speeds) {
        if (!print_shutter_speeds(handle)) {
            log_err("no shutter-speed properties available");
            cleanup();
            return 1;
        }
        exit_code = 0;
        cleanup();
        return exit_code;
    }

    if (args.connect_only) {
        std::cout << "connected" << std::endl;
        exit_code = 0;
        cleanup();
        return exit_code;
    }

    if (args.status_only) {
        print_camera_status(handle);
        exit_code = 0;
        cleanup();
        return exit_code;
    }

    if (live_view_snapshot) {
        if (!write_live_view_jpeg(handle, live_view_path)) {
            cleanup();
            return 1;
        }

        exit_code = 0;
        std::cout << live_view_path.string() << std::endl;
        cleanup();
        return exit_code;
    }

    if (live_view_stream) {
        exit_code = run_live_view_stream(
            handle,
            live_view_path,
            args.live_view_interval_ms);
        cleanup();
        return exit_code;
    }

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
    if (args.transfer_mode == TransferMode::HostPc) {
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

    // Trigger the shutter using the same S1-lock + Release sequence SonShell
    // uses for Access Auth / RemoteTransfer bodies.
    if (!trigger_full_shutter_press(handle)) {
        cleanup();
        return 1;
    }
    std::cerr << kExposureCompleteMarker << std::endl;

    if (args.transfer_mode == TransferMode::HostPc) {
        std::string downloaded_filename;
        if (!cb.wait_downloaded(std::chrono::seconds(args.timeout_s), downloaded_filename)) {
            const auto err = cb.last_error();
            if (err != 0) {
                log_err("camera reported error before host-PC download completed (CrError "
                        + std::to_string(static_cast<unsigned>(err)) + ")");
            } else {
                log_err("timed out waiting for host-PC auto-download");
            }
            cleanup();
            return 1;
        }

        fs::path src;
        if (!downloaded_filename.empty()) {
            const fs::path reported(downloaded_filename);
            const fs::path tmp_reported = tmp_dir / reported.filename();
            if (reported.is_absolute() && fs::exists(reported)) {
                src = reported;
            } else if (fs::exists(tmp_reported)) {
                src = tmp_reported;
            }
        }
        if (src.empty()) {
            if (auto found = find_one_file(tmp_dir)) {
                src = fs::absolute(*found);
            } else {
                log_err("host-PC download completed but no file found in " + tmp_dir.string());
                cleanup();
                return 1;
            }
        }

        src = fs::absolute(src);
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

    CrInt32u slot_raw = 0;
    bool got_contents_event = cb.wait_for_contents_changed(std::chrono::seconds(args.timeout_s), slot_raw);
    if (!got_contents_event) {
        const auto err = cb.last_error();
        if (err != 0) {
            log_err("camera reported error before RemoteTransfer contents appeared (CrError "
                    + std::to_string(static_cast<unsigned>(err)) + ")");
            cleanup();
            return 1;
        } else {
            std::cerr << "sony-capture: no RemoteTransfer contents-list callback; polling latest contents"
                      << std::endl;
        }
    }

    SDK::CrContentsInfo* contents = nullptr;
    CrInt32u contents_count = 0;
    SDK::CrCaptureDate dummy_date{};
    auto slot = got_contents_event
        ? static_cast<SDK::CrSlotNumber>(slot_raw)
        : SDK::CrSlotNumber_Slot1;
    auto fetch_latest_for_slot = [&](SDK::CrSlotNumber candidate_slot) -> bool {
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::seconds(args.timeout_s);
        do {
            if (contents != nullptr) {
                SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);
                contents = nullptr;
                contents_count = 0;
            }
            slot = candidate_slot;
            rc = SDK::GetRemoteTransferContentsInfoList(
                handle, slot, SDK::CrGetContentsInfoListType_All, &dummy_date, 0,
                &contents, &contents_count);
            if (CR_SUCCEEDED(rc) && contents != nullptr && contents_count > 0) {
                return true;
            }
            if (contents != nullptr) {
                SDK::ReleaseRemoteTransferContentsInfoList(handle, contents);
                contents = nullptr;
                contents_count = 0;
            }
            if (rc != SDK::CrError_RemoteTransfer_GetContentsInfoListProcessing) {
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(500));
        } while (std::chrono::steady_clock::now() < deadline);
        return false;
    };

    bool have_contents = false;
    if (got_contents_event) {
        have_contents = fetch_latest_for_slot(slot);
    } else {
        have_contents = fetch_latest_for_slot(SDK::CrSlotNumber_Slot1)
            || fetch_latest_for_slot(SDK::CrSlotNumber_Slot2);
    }
    if (!have_contents) {
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
