#!/usr/bin/env bash
# Capture a Flat Field Correction (FFC) calibration triplet.
#
# Why this exists
# ---------------
# Under narrowband-RGB illumination, each color channel has its own vignette
# profile — red, green, and blue fall off differently at the corners. The
# fix is per-channel FFC, but FFC needs a "blank light" reference: three
# captures of the scanlight at scanning brightness, *no film in the holder*.
#
# This script automates that. The output is a directory of three ARWs:
#
#     ~/.scanlight/calibration/<YYYY-MM-DD>/
#         R.ARW
#         G.ARW
#         B.ARW
#
# Pass that directory to rgb-composite via --ffc-calibration.
#
# When to re-run
# --------------
# - Once per scanning session, before you load film
# - Whenever you change holder, working distance, or lens
# - Whenever you swap the scanlight bulb, replace its diffuser, or move it
#
# Usage:
#   scripts/capture-calibration.sh                       # default location
#   scripts/capture-calibration.sh ~/my/cal/dir          # custom location
#   scripts/capture-calibration.sh --force ~/my/cal/dir  # overwrite existing
#
# Exit codes:
#   0  — all three frames captured successfully
#   1  — capture or sanity-check failed
#   2  — tooling missing (scanlightctl / sony-capture binary not found)
#   3  — operator aborted at the "remove film" confirmation

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Per-channel brightness defaults for the blank-light capture. These
# need to match (or be close to) the levels you'll actually use during
# scanning — the FFC map you compute from these frames is only valid at
# the wavelength response the LEDs produce at this brightness. 200 is
# the default `CaptureSettings.level_*` in `triplet-capture`. Lower the
# level if `scripts/inspect-calibration.py` reports any channel as
# over-exposed (>1% saturated pixels).
LEVEL_R=200
LEVEL_G=200
LEVEL_B=200

# Argument parsing — supports --force / -f and per-channel level overrides.
FORCE=0
TARGET=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f) FORCE=1 ; shift ;;
        --help|-h)
            grep -E '^# ' "$0" | head -50 | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --r-level) LEVEL_R="$2" ; shift 2 ;;
        --g-level) LEVEL_G="$2" ; shift 2 ;;
        --b-level) LEVEL_B="$2" ; shift 2 ;;
        --level)
            # Convenience: --level 180 sets all three channels at once.
            LEVEL_R="$2" ; LEVEL_G="$2" ; LEVEL_B="$2"
            shift 2
            ;;
        -*)
            echo "unknown flag: $1" >&2
            exit 2
            ;;
        *)
            if [[ -n "$TARGET" ]]; then
                echo "only one target dir argument is allowed" >&2
                exit 2
            fi
            TARGET="$1"
            shift
            ;;
    esac
done

# Validate levels — fail-loudly if someone passes a non-numeric or
# out-of-range value rather than letting scanlightctl reject it later.
for lvl in "$LEVEL_R" "$LEVEL_G" "$LEVEL_B"; do
    if ! [[ "$lvl" =~ ^[0-9]+$ ]] || (( lvl < 1 || lvl > 255 )); then
        echo "level must be an integer in [1, 255], got: $lvl" >&2
        exit 2
    fi
done

if [[ -z "$TARGET" ]]; then
    TARGET="${HOME}/.scanlight/calibration/$(date +%Y-%m-%d)"
fi

# Resolve scanlightctl + sony-capture, matching scripts/smoketest.sh's logic.
SCANLIGHTCTL="$(command -v scanlightctl || true)"
if [[ -z "$SCANLIGHTCTL" ]]; then
    SCANLIGHTCTL="python3 -m scanlight.cli"
    export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}${REPO_ROOT}/phase1/scanlightctl"
fi

SONY_CAPTURE="$(command -v sony-capture || true)"
if [[ -z "$SONY_CAPTURE" ]]; then
    SONY_CAPTURE="${REPO_ROOT}/phase1/sony-capture/build/sony-capture"
fi
if [[ ! -x "$SONY_CAPTURE" ]]; then
    echo "sony-capture binary not found at PATH or ${REPO_ROOT}/phase1/sony-capture/build/" >&2
    echo "build it first:  cd phase1/sony-capture && cmake --build build" >&2
    exit 2
fi

# Target-dir conflict handling.
if [[ -d "$TARGET" ]] && [[ -e "$TARGET/R.ARW" || -e "$TARGET/G.ARW" || -e "$TARGET/B.ARW" ]]; then
    if [[ "$FORCE" -eq 1 ]]; then
        echo "overwriting existing calibration in $TARGET (--force)"
        rm -f "$TARGET/R.ARW" "$TARGET/G.ARW" "$TARGET/B.ARW"
    else
        echo "calibration files already exist in $TARGET — re-run with --force to overwrite" >&2
        exit 1
    fi
fi

mkdir -p "$TARGET"

# Operator confirmation. FFC capture with FILM in the holder gives you a
# garbage calibration that you won't notice until conversions come out wrong.
cat <<'EOF'

╔════════════════════════════════════════════════════════════════╗
║  FFC calibration — blank-light reference capture               ║
╠════════════════════════════════════════════════════════════════╣
║  Before continuing:                                            ║
║    1. REMOVE FILM from the holder.                             ║
║    2. Keep the camera, lens, holder, and scanlight in EXACTLY  ║
║       the scanning configuration — same focus, same crop,      ║
║       same distance. This calibration only valid for THIS      ║
║       setup.                                                   ║
║    3. Camera must be tethered + in PC Remote mode.             ║
║    4. Scanlight powered via wall PSU on the RIGHT port.        ║
╚════════════════════════════════════════════════════════════════╝

EOF
read -r -p "Type 'blank' to confirm the holder is empty and continue: " CONFIRM
if [[ "$CONFIRM" != "blank" ]]; then
    echo "aborted by operator" >&2
    exit 3
fi
echo

# Plausible RAW size band (matches PROJECT.md / smoketest.sh).
MIN_BYTES=$((40 * 1024 * 1024))
MAX_BYTES=$((120 * 1024 * 1024))

check_file() {
    local path="$1"
    local channel="$2"
    if [[ ! -f "$path" ]]; then
        echo "FAIL: $channel capture missing file $path" >&2
        return 1
    fi
    local size
    size=$(stat -f%z "$path" 2>/dev/null || stat -c%s "$path")
    if (( size < MIN_BYTES || size > MAX_BYTES )); then
        echo "FAIL: $channel file size ${size} bytes outside plausible range" >&2
        return 1
    fi
    echo "  $channel ok: $path ($((size / 1024 / 1024))MB)"
}

run_one() {
    local channel="$1"     # r|g|b for scanlightctl
    local label="$2"       # R|G|B for filename
    local level="$3"       # 1–255
    local out="${TARGET}/${label}.ARW"
    echo "→ scanlightctl on $channel --level $level"
    $SCANLIGHTCTL on "$channel" --level "$level"
    sleep 0.1  # let LEDs settle
    echo "→ sony-capture → $out"
    "$SONY_CAPTURE" --out "$out" --timeout 30
    check_file "$out" "$label"
}

# Always turn the scanlight off on exit, even if something fails halfway.
trap '$SCANLIGHTCTL off || true' EXIT

run_one r R "$LEVEL_R"
run_one g G "$LEVEL_G"
run_one b B "$LEVEL_B"

echo "→ scanlightctl off"
$SCANLIGHTCTL off

# Capture device telemetry post-calibration. If VBUS dipped below 4500 mV
# or LED temp climbed near the firmware's 80 °C threshold during the cal
# sequence, the calibration data may not represent normal scanning
# conditions and should be re-shot. We capture status AFTER scanlight off
# so the temperature reading is the worst-case (LEDs warm from R+G+B).
echo "→ recording post-cal telemetry"
STATUS_OUT="$( $SCANLIGHTCTL status 2>&1 || echo "status: scanlightctl status failed" )"

# Extract the relevant numbers from the status output (key: value format).
# Best-effort parsing — if scanlightctl output format ever changes, the
# manifest still gets written, just with raw lines.
FW_LINE=$(echo "$STATUS_OUT" | grep -E '^firmware:' || echo "firmware: (unknown)")
HW_LINE=$(echo "$STATUS_OUT" | grep -E '^hardware:' || echo "hardware: (unknown)")
TEMP_LINE=$(echo "$STATUS_OUT" | grep -E '^LED temp:' || echo "LED temp: (unknown)")
VBUS_LINE=$(echo "$STATUS_OUT" | grep -E '^VBUS:' || echo "VBUS: (unknown)")

# Stamp a manifest so we can tell at a glance when this was captured AND
# whether the device was healthy at the time.
MANIFEST="${TARGET}/manifest.txt"
cat > "$MANIFEST" <<EOF
captured_at: $(date -Iseconds)
host: $(hostname)
target: $TARGET
levels:
  R: $LEVEL_R
  G: $LEVEL_G
  B: $LEVEL_B
files:
  R: $TARGET/R.ARW
  G: $TARGET/G.ARW
  B: $TARGET/B.ARW

device_telemetry:
  # Captured immediately after the R, G, B blanks were shot. If VBUS is
  # below 4500 mV or LED temp is above 70 °C, suspect the calibration —
  # see HANDOFF.md §"Firmware behavior gotchas" for context.
  $FW_LINE
  $HW_LINE
  $TEMP_LINE
  $VBUS_LINE

note: |
  Blank-light calibration for FFC. Pass this directory to:
    rgb-composite ... --ffc-calibration $TARGET
    batch-composite ... --ffc-calibration $TARGET
  Verify quality numerically with:
    python3 scripts/inspect-calibration.py $TARGET
EOF

echo
echo "PASS — calibration triplet written to:"
echo "  $TARGET"
echo
echo "Next: pass this path to rgb-composite or batch-composite, e.g."
echo "  batch-composite /Volumes/SSD/Scans/Roll001 --ffc-calibration $TARGET"
