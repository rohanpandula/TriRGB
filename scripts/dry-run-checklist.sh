#!/usr/bin/env bash
# Optical dry-run checklist — interactive walkthrough of docs/optical_dry_run.md.
#
# Walks the operator through every pre-scan verification step defined in
# docs/optical_dry_run.md (sections 1, 2, 3, 3.5, 4, 5).  Each step accepts:
#   y  — pass (step verified)
#   s  — skip (operator bypasses the step; NOT counted as a failure)
#   n  — fail (operator is prompted for a free-text reason; step is recorded
#          as failed; the script continues through remaining steps, then
#          prints a summary and exits 1)
#
# A transcript of all answers is written to ${TMPDIR:-/tmp}/dry-run-checklist-<ts>.log
# AND tee'd to stdout so the operator sees it live.
#
# Relationship to docs/optical_dry_run.md:
#   This script is a MANUAL MIRROR of the doc's checkbox items.  If the doc
#   adds or removes a step, this script must be updated to match.  There is
#   no auto-generation; keeping the two in sync is an operator responsibility.
#
# Usage:
#   scripts/dry-run-checklist.sh             # interactive (prompts per step)
#   scripts/dry-run-checklist.sh --selftest  # non-interactive (auto-y, CI/headless)
#
# Exit codes:
#   0  all steps passed or skipped
#   1  one or more steps failed (with reason captured)
#   2  unexpected argument (usage error)
#
# DO NOT source this script — the exec tee redirect hijacks the calling shell.
# Always invoke directly: bash scripts/dry-run-checklist.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
SELFTEST=0
case "${1:-}" in
    "")
        ;;
    --selftest)
        SELFTEST=1
        ;;
    *)
        echo "usage: scripts/dry-run-checklist.sh [--selftest]" >&2
        exit 2
        ;;
esac

# ---------------------------------------------------------------------------
# Transcript setup
# ---------------------------------------------------------------------------
# Respect $TMPDIR (macOS sets this to a per-user temp dir; hard-coding /tmp
# would write to the wrong place on macOS).
LOG="${TMPDIR:-/tmp}/dry-run-checklist-$(date +%Y%m%d-%H%M%S).log"

# Redirect all output through tee so the operator sees it AND it ends up in
# the transcript.  This must be done BEFORE any echo calls.
# Note: exec > >(tee ...) starts a subprocess; set -e is still active, but
# the subshell inherits the same settings.
exec > >(tee -a "$LOG") 2>&1

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
# FAILS must be initialized before any ask() call.  Under set -u, referencing
# an unset array would trigger "unbound variable".
FAILS=()

# ---------------------------------------------------------------------------
# ask() — prompt for one step
#
# The || FAILS+=("$step") callers rely on bash short-circuit semantics: when
# ask() returns 1, the || branch runs and FAILS gets the step ID appended.
# set -e does NOT fire here because the || short-circuits the non-zero exit.
# ---------------------------------------------------------------------------
ask() {
    local step="$1"
    local prompt="$2"

    if [[ $SELFTEST -eq 1 ]]; then
        echo "✓ $step (selftest: auto-y)"
        return 0
    fi

    # Interactive loop — re-prompt on unrecognised input.
    while true; do
        read -r -p "${prompt} [y/s/n]: " ans
        case "$ans" in
            y|Y)
                echo "✓ $step"
                return 0
                ;;
            s|S)
                echo "⏭ $step (skipped)"
                return 0
                ;;
            n|N)
                read -r -p "  why? " reason
                echo "✗ $step — $reason"
                return 1
                ;;
        esac
        # Any other input: re-prompt.
    done
}

# ---------------------------------------------------------------------------
# Checklist steps — mirrors docs/optical_dry_run.md sections 1, 2, 3, 3.5, 4, 5
# ---------------------------------------------------------------------------
echo "=== Optical dry run checklist ==="
echo "Mirrors docs/optical_dry_run.md. Answer y/s/n per step."
echo "  y = pass | s = skip (not counted as failure) | n = fail (captures reason)"
echo

echo "--- Section 1: Frame and focus ---"
ask "frame_focus" \
    "Is grain in focus across the frame (peaking + 10x mag, focused on grain not image)?" \
    || FAILS+=("frame_focus")

echo
echo "--- Section 2: Per-channel exposure ---"
ask "per_channel_R" \
    "R-lit histogram: film base in upper third, no highlight blink, no shadow crush?" \
    || FAILS+=("per_channel_R")
ask "per_channel_G" \
    "G-lit histogram: film base in upper third, no highlight blink, no shadow crush?" \
    || FAILS+=("per_channel_G")
ask "per_channel_B" \
    "B-lit histogram: film base in upper third, no highlight blink, no shadow crush?" \
    || FAILS+=("per_channel_B")

echo
echo "--- Section 3: Vignetting ---"
ask "vignetting_symmetric" \
    "Vignetting (if any) is symmetric and < 0.5 EV magnitude?" \
    || FAILS+=("vignetting_symmetric")

echo
echo "--- Section 3.5: Per-channel narrowband vignette ---"
ask "narrowband_falloff" \
    "Did you run scripts/inspect-calibration.py and confirm falloff < 30% per channel, drift < 10%?" \
    || FAILS+=("narrowband_falloff")
ask "narrowband_uniformity" \
    "Did inspect-calibration.py report uniformity < 8% per channel (no patchy local variation)?" \
    || FAILS+=("narrowband_uniformity")

echo
echo "--- Section 4: Newton rings ---"
ask "newton_rings" \
    "No rainbow interference patterns visible between film and any glass surface?" \
    || FAILS+=("newton_rings")

echo
echo "--- Section 5: Repeatability ---"
ask "repeatability" \
    "Three consecutive captures of the same frame are pixel-identical (no vibration)?" \
    || FAILS+=("repeatability")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "=== Summary ==="
if [[ ${#FAILS[@]} -eq 0 ]]; then
    echo "PASS — all dry-run steps verified"
    echo "Transcript: $LOG"
    exit 0
else
    echo "FAIL — failed steps: ${FAILS[*]}"
    echo "Re-do each failed step per the corresponding section in docs/optical_dry_run.md."
    echo "Transcript: $LOG"
    exit 1
fi
