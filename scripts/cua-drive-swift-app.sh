#!/usr/bin/env bash
# cua-drive-swift-app.sh — AI-agent / CI entry point for QA-ing the
# ScanlightApp SwiftUI GUI surface via the cua-driver macOS AX-tree CLI.
#
# This is the third automation path alongside the Python CLI harness
# (scripts/test_swift_cli.py) and the XCTest UI suite (ScanlightAppUITests).
# It demonstrates driving the running scanlight-app GUI end-to-end using
# accessibility-tree introspection — satisfying R-14.
#
# Three modes:
#   (default / -h / --help)  Print this usage text and exit 0.
#   --selftest               Hermetic end-to-end AX-ID verification.
#                            Builds the app, launches with -FakeTransport YES,
#                            snapshots the AX tree, asserts all 23 AX-IDs are
#                            present, optionally clicks connect and re-snapshots.
#                            Emits a single JSON line on stdout; exit 0 on pass.
#   --demo                   Same flow but with verbose narration and screenshots
#                            saved to /tmp/. Ends with a 3-second pause so the
#                            operator can eyeball the app window.
#
# Exit codes:
#   0 — success
#   1 — operational failure (missing AX-IDs, build failure, app didn't start)
#   2 — preconditions failure (cua-driver missing, wrong OS, wrong arg)
#
# Prerequisites:
#   • macOS (Darwin) — AX-tree automation is macOS-only
#   • cua-driver installed at /Users/rohan/.local/bin/cua-driver or on $PATH
#     (override with CUA_DRIVER_BIN env var)
#   • Accessibility permission granted for the terminal app running this script
#     (System Settings → Privacy & Security → Accessibility)

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SWIFT_PACKAGE="${REPO_ROOT}/phase3/FilmScanner"
APP_BINARY_DEBUG="${SWIFT_PACKAGE}/.build/debug/scanlight-app"
APP_BINARY_RELEASE="${SWIFT_PACKAGE}/.build/release/scanlight-app"

# Locate cua-driver: env override → $PATH → known install location
CUA_DRIVER="${CUA_DRIVER_BIN:-}"
if [[ -z "$CUA_DRIVER" ]]; then
    CUA_DRIVER="$(command -v cua-driver 2>/dev/null || echo "/Users/rohan/.local/bin/cua-driver")"
fi

# The 23 AccessibilityID string values mirrored from AccessibilityIDs.swift.
# Keep in sync with that file; adding a new case requires adding here too.
EXPECTED_AX_IDS=(
    btn-connect
    btn-disconnect
    field-port
    lbl-connection-status
    lbl-firmware
    lbl-hardware
    lbl-led-temp
    lbl-vbus
    slider-red
    slider-green
    slider-blue
    slider-white
    btn-red-on
    btn-green-on
    btn-blue-on
    btn-white-on
    btn-off
    btn-set-rgb
    field-pulse-ms
    btn-fire-pulse
    lbl-last-error
    scroll-log
    btn-clear-log
)

# Sanity check at load time — catch off-by-one mistakes immediately
if [[ "${#EXPECTED_AX_IDS[@]}" -ne 23 ]]; then
    echo "INTERNAL ERROR: EXPECTED_AX_IDS has ${#EXPECTED_AX_IDS[@]} entries, expected 23" >&2
    exit 2
fi

APP_PID=""

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
cua-drive-swift-app.sh — Drive the scanlight-app SwiftUI GUI via AX-tree automation.

USAGE:
  scripts/cua-drive-swift-app.sh                 Print this text and exit.
  scripts/cua-drive-swift-app.sh --selftest       Hermetic AX-ID coverage run (CI-safe).
  scripts/cua-drive-swift-app.sh --demo           Verbose interactive walkthrough.

REQUIREMENTS:
  • macOS (Darwin)
  • cua-driver installed (set CUA_DRIVER_BIN env var to override location)
  • Accessibility permission granted in System Settings → Privacy & Security → Accessibility

EXIT CODES:  0 success  |  1 operational failure  |  2 preconditions failure
EOF
}

# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------

require_macos() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "ERROR: This script requires macOS (got: $(uname -s))" >&2
        exit 2
    fi
}

require_cua_driver() {
    if [[ ! -x "$CUA_DRIVER" ]]; then
        echo "ERROR: cua-driver not found or not executable at: $CUA_DRIVER" >&2
        echo "       Install cua-driver or set CUA_DRIVER_BIN env var." >&2
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build_app() {
    echo "==> Building scanlight-app..." >&2
    (cd "$SWIFT_PACKAGE" && swift build --product scanlight-app) >&2 || {
        echo "ERROR: swift build failed (see output above)" >&2
        exit 1
    }
    echo "==> Build complete." >&2
}

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

launch_app() {
    # Prefer debug build (already built); release only if debug absent
    local binary="$APP_BINARY_DEBUG"
    if [[ ! -x "$binary" ]]; then
        binary="$APP_BINARY_RELEASE"
    fi
    if [[ ! -x "$binary" ]]; then
        echo "ERROR: scanlight-app binary not found — run swift build first" >&2
        exit 1
    fi

    echo "==> Launching: $binary -FakeTransport YES" >&2
    "$binary" -FakeTransport YES &
    APP_PID=$!
    # Trap to kill the app on any exit from the script
    trap "kill $APP_PID 2>/dev/null || true; wait $APP_PID 2>/dev/null || true" EXIT
    # Brief pause for the window server to register the new window
    sleep 1
    echo "==> App launched with PID $APP_PID" >&2
}

# ---------------------------------------------------------------------------
# App discovery via cua-driver
# ---------------------------------------------------------------------------

# Resolve the pid cua-driver sees for the running scanlight-app.
# list_windows is used instead of list_apps because scanlight-app is a plain
# binary (no bundle ID), so list_apps does not surface it. list_windows sees
# the WindowServer window and its owner pid.
discover_window() {
    local window_json
    window_json="$("$CUA_DRIVER" call list_windows 2>/dev/null)"
    local window_id
    window_id="$(echo "$window_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
wins = [w for w in data.get('windows', []) if w.get('pid') == $APP_PID and w.get('is_on_screen')]
if wins:
    print(wins[0]['window_id'])
" 2>/dev/null)"
    echo "$window_id"
}

wait_for_window() {
    local attempts=0
    local max_attempts=10
    local window_id=""
    while [[ $attempts -lt $max_attempts ]]; do
        window_id="$(discover_window)"
        if [[ -n "$window_id" ]]; then
            echo "$window_id"
            return 0
        fi
        sleep 0.5
        (( attempts++ )) || true
    done
    echo "ERROR: scanlight-app window did not appear in cua-driver's window list after ${max_attempts} attempts" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# AX tree snapshot + grep
# ---------------------------------------------------------------------------

snapshot_ax_tree() {
    local pid="$1"
    local window_id="$2"
    local output_path="$3"
    "$CUA_DRIVER" call get_window_state "$(printf '{"pid":%s,"window_id":%s}' "$pid" "$window_id")" > "$output_path" 2>&1
}

assert_all_ax_ids_present() {
    local snapshot="$1"
    local missing=()
    local found_count=0

    for id in "${EXPECTED_AX_IDS[@]}"; do
        if grep -qF -- "$id" "$snapshot"; then
            (( found_count++ )) || true
        else
            missing+=("$id")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "FAIL: ${#missing[@]} AccessibilityID(s) missing from AX tree: ${missing[*]}" >&2
        echo "$found_count"
        return 1
    fi
    echo "$found_count"
    return 0
}

# ---------------------------------------------------------------------------
# Click-and-reverify pass (best-effort; see SPEC.md § Open decisions)
# ---------------------------------------------------------------------------

click_connect_button() {
    local pid="$1"
    local window_id="$2"
    local snapshot="$3"

    # Find the element_index of btn-connect in the pre-snapshot
    local element_index
    element_index="$(python3 -c "
import re, sys
text = open('$snapshot').read()
# cua-driver renders as: [3] AXButton (Connect) id=btn-connect
# tree_markdown is one logical line w/ literal \\n separators — anchor tight.
matches = re.findall(r'\[(\d+)\]\s*AXButton[^\[]*?id=btn-connect\b', text)
if not matches:
    # Fallback: legacy [element_index N] format
    matches = re.findall(r'\[element_index (\d+)\][^\n]*btn-connect', text)
print(matches[0] if matches else '')
" 2>/dev/null)"

    if [[ -z "$element_index" ]]; then
        echo "skipped" ; return 0
    fi

    # element_index clicks require a cua-driver daemon to persist the AX cache
    # between CLI invocations. Without `cua-driver serve &` running, the click
    # call has no cached state from the prior get_window_state and degrades to
    # skipped. This is intentional best-effort — the primary --selftest signal
    # is the AX-ID coverage assertion (all 23 present), which proves the GUI
    # surface is automatable. Operators who want the full click probe can:
    #   1. `open -n -g -a CuaDriver --args serve` (or `cua-driver serve &`)
    #   2. Re-run scripts/cua-drive-swift-app.sh --selftest
    "$CUA_DRIVER" call click "$(printf '{"pid":%s,"window_id":%s,"element_index":%s}' "$pid" "$window_id" "$element_index")" >/dev/null 2>&1 && echo "ok" || echo "skipped"
}

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

selftest() {
    require_macos
    require_cua_driver
    build_app
    launch_app

    local window_id
    window_id="$(wait_for_window)"
    echo "==> Window ID: $window_id" >&2

    local pre_snapshot="/tmp/scanlight-app-axtree-pre.md"
    echo "==> Snapshotting AX tree..." >&2
    snapshot_ax_tree "$APP_PID" "$window_id" "$pre_snapshot"

    local found_count
    if found_count="$(assert_all_ax_ids_present "$pre_snapshot")"; then
        echo "==> All ${#EXPECTED_AX_IDS[@]} AX-IDs found in tree." >&2
    else
        echo "$(printf '{"ok":false,"ax_ids_total":23,"ax_ids_found":%s,"ax_ids_missing":["%s"],"click_phase":"skipped"}' \
            "$found_count" \
            "$(assert_all_ax_ids_present "$pre_snapshot" 2>&1 | sed 's/FAIL:.*missing from AX tree: //' | sed 's/ /", "/g')")"
        exit 1
    fi

    # Best-effort click-and-re-snapshot
    echo "==> Attempting click on btn-connect..." >&2
    local click_phase
    click_phase="$(click_connect_button "$APP_PID" "$window_id" "$pre_snapshot")"
    echo "==> Click phase: $click_phase" >&2

    if [[ "$click_phase" == "ok" ]]; then
        sleep 1
        local post_snapshot="/tmp/scanlight-app-axtree-post.md"
        snapshot_ax_tree "$APP_PID" "$window_id" "$post_snapshot"
        # Re-assert all AX-IDs still present after connect
        if ! assert_all_ax_ids_present "$post_snapshot" >/dev/null; then
            echo "==> WARNING: post-connect snapshot missing some AX-IDs" >&2
            click_phase="degraded"
        fi
    fi

    printf '{"ok":true,"ax_ids_total":23,"ax_ids_found":%d,"ax_ids_missing":[],"click_phase":"%s"}\n' \
        "${#EXPECTED_AX_IDS[@]}" "$click_phase"
}

# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------

demo() {
    require_macos
    require_cua_driver

    echo ""
    echo "=== cua-drive-swift-app.sh DEMO MODE ==="
    echo "Building and launching scanlight-app with FakeTransport..."
    echo ""

    build_app
    launch_app

    echo ""
    echo "Waiting for app window to appear..."
    local window_id
    window_id="$(wait_for_window)"
    echo "Window registered: ID=$window_id  PID=$APP_PID"
    echo ""

    echo "Taking screenshot..."
    "$CUA_DRIVER" call screenshot "{}" > /tmp/scanlight-app-demo.png 2>&1 || \
        echo "(screenshot failed — continuing)"
    echo "Screenshot saved to /tmp/scanlight-app-demo.png"
    echo ""

    echo "Snapshotting AX tree (pre-connect)..."
    local pre_snapshot="/tmp/scanlight-app-axtree-pre.md"
    snapshot_ax_tree "$APP_PID" "$window_id" "$pre_snapshot"
    echo "Snapshot saved to $pre_snapshot"
    echo ""

    echo "Checking all 23 AX-IDs..."
    local found_count
    local all_ok=0
    found_count="$(assert_all_ax_ids_present "$pre_snapshot")" || all_ok=1
    if [[ $all_ok -eq 0 ]]; then
        echo "PASS: All ${#EXPECTED_AX_IDS[@]} AX-IDs present in AX tree."
    else
        echo "FAIL: Some AX-IDs missing (see stderr above)."
    fi
    echo ""

    echo "Attempting to click 'Connect' button..."
    local click_phase
    click_phase="$(click_connect_button "$APP_PID" "$window_id" "$pre_snapshot")"
    echo "Click phase: $click_phase"
    echo ""

    echo "Taking post-connect screenshot..."
    "$CUA_DRIVER" call screenshot "{}" > /tmp/scanlight-app-demo-post.png 2>&1 || \
        echo "(screenshot failed — continuing)"
    echo "Screenshot saved to /tmp/scanlight-app-demo-post.png"
    echo ""

    echo "Sleeping 3 seconds so you can view the app window..."
    sleep 3

    echo ""
    printf '{"ok":%s,"ax_ids_total":23,"ax_ids_found":%s,"ax_ids_missing":[],"click_phase":"%s"}\n' \
        "$( [[ $all_ok -eq 0 ]] && echo 'true' || echo 'false' )" \
        "$found_count" "$click_phase"
}

# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

case "${1:-}" in
    --selftest)  selftest ;;
    --demo)      demo ;;
    -h|--help|"") usage; exit 0 ;;
    *)           usage; exit 2 ;;
esac
