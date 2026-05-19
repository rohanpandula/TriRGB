#!/usr/bin/env bash
# Phase 1 exit-criteria smoke test.
#
# Cycles R/G/B exposures and verifies three RAW files land on disk.
# Run this AFTER scripts/diagnose.py reports all green.
#
# usage:
#   scripts/smoketest.sh [output_dir]
#
# Default output_dir: /tmp/film-scanner-smoketest

set -euo pipefail

OUT_DIR="${1:-/tmp/film-scanner-smoketest}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve scanlightctl (Python entrypoint) and sony-capture (built binary).
# Prefer $PATH; fall back to the repo's expected locations.
SCANLIGHTCTL="$(command -v scanlightctl || true)"
if [[ -z "$SCANLIGHTCTL" ]]; then
    echo "scanlightctl not on \$PATH — falling back to python -m scanlight.cli"
    SCANLIGHTCTL="python3 -m scanlight.cli"
    export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}${REPO_ROOT}/phase1/scanlightctl"
fi

SONY_CAPTURE="$(command -v sony-capture || true)"
if [[ -z "$SONY_CAPTURE" ]]; then
    SONY_CAPTURE="${REPO_ROOT}/phase1/sony-capture/build/sony-capture"
fi
if [[ ! -x "$SONY_CAPTURE" ]]; then
    echo "sony-capture binary not found at \$PATH or ${REPO_ROOT}/phase1/sony-capture/build/" >&2
    echo "build it first: cd phase1/sony-capture && cmake --build build" >&2
    exit 2
fi

mkdir -p "$OUT_DIR"
echo "smoketest output: $OUT_DIR"

# Plausible RAW size band (matches PROJECT.md).
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
        echo "FAIL: $channel file size ${size} bytes outside plausible range (${MIN_BYTES}-${MAX_BYTES})" >&2
        return 1
    fi
    echo "  $channel ok: $path ($((size / 1024 / 1024))MB)"
}

run_one() {
    local channel="$1"  # r|g|b
    local label="$2"    # R|G|B (for filename)
    local out="${OUT_DIR}/Smoke_Frame001_${label}.ARW"
    echo "→ scanlightctl on $channel"
    $SCANLIGHTCTL on "$channel"
    sleep 0.05
    echo "→ sony-capture → $out"
    "$SONY_CAPTURE" --out "$out" --timeout 30
    check_file "$out" "$label"
}

echo
echo "=== Phase 1 exit-criteria smoke test ==="
trap '$SCANLIGHTCTL off || true' EXIT

run_one r R
run_one g G
run_one b B

echo "→ scanlightctl off"
$SCANLIGHTCTL off

echo
echo "PASS — three RAW files produced in plausible size range."
echo "Next step: roll through a real frame on the optical bench, then run"
echo "rgb-composite on the three RAWs to verify the compositor end-to-end."
