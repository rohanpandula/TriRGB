#!/usr/bin/env python3
"""Automate the Cobalt vs Adobe DCP A/B test for Scanlight composites.

Accepts a triplet directory (containing R/G/B ARW files), shells out to
`rgb-composite` twice — once with the Scanlight v4 camera-model string (Adobe
Standard only in Lightroom) and once with the Sony ILCE-7CR camera-model
string (Cobalt Spectre + other Sony profiles available in Lightroom) — then
parses both resulting DNGs and writes a tag-by-tag diff report.

Usage:
  python3 scripts/compare-dcp.py <triplet-dir>
  python3 scripts/compare-dcp.py <triplet-dir> --keep-dngs

Exit codes:
  0  Both composites produced, DNGs parsed, report written.
  1  One or both rgb-composite invocations failed, or DNG parse failed.
  2  Triplet not found in the input directory, or input dir does not exist.

Notes:
  - This script does NOT modify the input directory except for writing
    `.compare-dcp-report.txt` (and optionally a `_compare-dcp-output/`
    subdirectory when --keep-dngs is set).
  - Both DNG outputs live in a tempfile.TemporaryDirectory during the run.
    They are cleaned up automatically unless --keep-dngs is passed.
  - The script shells out to rgb-composite as an external process (subprocess.run)
    rather than importing composite_triplet directly, so the test harness can
    stub subprocess.run without needing real ARW files.
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup — add phase2/rgb-composite to sys.path so we can import
# rgb_composite.dng.read_linear_dng_tags without installing the package.
# ---------------------------------------------------------------------------
_RGB_COMPOSITE_PARENT = Path(__file__).resolve().parent.parent / "phase2" / "rgb-composite"
sys.path.insert(0, str(_RGB_COMPOSITE_PARENT))
from rgb_composite.dng import read_linear_dng_tags  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODEL_SCANLIGHT = "Scanlight v4 Narrowband-RGB Composite"
MODEL_SONY = "Sony ILCE-7CR"
REPORT_FILENAME = ".compare-dcp-report.txt"

# ARW channel suffix patterns accepted by _find_triplet.
_CHANNEL_SUFFIXES = {"R": "_R.ARW", "G": "_G.ARW", "B": "_B.ARW"}


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _find_triplet(input_dir: Path) -> tuple[Path, Path, Path]:
    """Locate the first R/G/B ARW triplet in `input_dir` (non-recursive).

    Triplet convention: files whose names end in `_R.ARW`, `_G.ARW`, `_B.ARW`
    with the same stem prefix (e.g. `Roll_Frame001_R.ARW` and friends).

    Returns:
        (r_path, g_path, b_path) — the first triplet in lexicographic order.

    Raises:
        FileNotFoundError: if no complete triplet is found.
    """
    if not input_dir.is_dir():
        raise FileNotFoundError(f"not a directory: {input_dir}")

    # Build a case-insensitive map of filename → Path.
    files_upper: dict[str, Path] = {
        p.name.upper(): p for p in input_dir.iterdir() if p.is_file()
    }

    # Find all stems that have a _R.ARW partner.
    r_candidates: list[str] = []
    for name_upper, path in files_upper.items():
        if name_upper.endswith("_R.ARW"):
            r_candidates.append(name_upper[: -len("_R.ARW")])  # strip suffix

    r_candidates.sort()

    for stem in r_candidates:
        g_key = (stem + "_G.ARW").upper()
        b_key = (stem + "_B.ARW").upper()
        if g_key in files_upper and b_key in files_upper:
            # Reconstruct from original-case map.
            r_upper = (stem + "_R.ARW").upper()
            return (
                files_upper[r_upper],
                files_upper[g_key],
                files_upper[b_key],
            )

    raise FileNotFoundError(
        f"no complete R/G/B triplet found in {input_dir}. "
        "Expected files matching <prefix>_R.ARW, <prefix>_G.ARW, <prefix>_B.ARW."
    )


def _run_composite(
    r: Path,
    g: Path,
    b: Path,
    out_dng: Path,
    model: str,
) -> tuple[int, str, str]:
    """Shell out to rgb_composite.composite with the given camera-model.

    Uses the PYTHONPATH approach so the module resolves even if rgb-composite
    is not installed via pip.

    Returns:
        (returncode, stdout, stderr)
    """
    env = {**os.environ, "PYTHONPATH": str(_RGB_COMPOSITE_PARENT)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rgb_composite.composite",
            "--r",
            str(r),
            "--g",
            str(g),
            "--b",
            str(b),
            "--out",
            str(out_dng),
            "--format",
            "dng",
            "--camera-model",
            model,
            "--no-sidecar",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _diff_tags(
    tags_a: dict,
    tags_b: dict,
) -> list[tuple[str, str, str, str]]:
    """Produce a sorted list of tag-diff rows from two tag dicts.

    Each row: (tag_name, rendered_value_a, rendered_value_b, "SAME"|"DIFF").
    Keys present in only one dict are included with an empty string for the
    missing side; they always report "DIFF".
    """
    all_keys = sorted(set(tags_a) | set(tags_b))
    rows = []
    for key in all_keys:
        val_a = tags_a.get(key)
        val_b = tags_b.get(key)
        result = "SAME" if (val_a is not None and val_b is not None and val_a == val_b) else "DIFF"
        rows.append((key, _render_value(val_a), _render_value(val_b), result))
    return rows


def _render_value(value: object) -> str:
    """Render a tag value as a concise string for the report."""
    if value is None:
        return "(absent)"
    if isinstance(value, bytes):
        hex_str = value.hex()
        if len(hex_str) > 32:
            return hex_str[:32] + "..."
        return hex_str
    rendered = str(value)
    if len(rendered) > 80:
        return rendered[:80] + "..."
    return rendered


def _format_report(
    input_dir: Path,
    triplet: tuple[Path, Path, Path],
    dng_a: Path,
    dng_b: Path,
    tags_a: dict,
    tags_b: dict,
    model_a: str,
    model_b: str,
    size_a: int,
    size_b: int,
) -> str:
    """Build the text content of the comparison report."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    diff_rows = _diff_tags(tags_a, tags_b)

    # Tag diff table — measure column widths dynamically.
    col_tag = max(len("Tag"), *(len(row[0]) for row in diff_rows))
    col_a = max(len(model_a[:40]), *(len(row[1]) for row in diff_rows))
    col_b = max(len(model_b[:40]), *(len(row[2]) for row in diff_rows))
    # Cap for readability
    col_a = min(col_a, 60)
    col_b = min(col_b, 60)

    sep = "-" * (col_tag + col_a + col_b + 13)
    hdr_model_a = model_a[:col_a]
    hdr_model_b = model_b[:col_b]
    tag_table_lines = [
        f"  {'Tag':<{col_tag}} | {hdr_model_a:<{col_a}} | {hdr_model_b:<{col_b}} | Result",
        f"  {'-' * col_tag}-+-{'-' * col_a}-+-{'-' * col_b}-+-------",
    ]
    for tag_name, va, vb, result in diff_rows:
        tag_table_lines.append(
            f"  {tag_name:<{col_tag}} | {va:<{col_a}} | {vb:<{col_b}} | {result}"
        )

    size_delta_pct = (
        100.0 * (size_b - size_a) / max(size_a, 1)
        if size_a > 0
        else 0.0
    )
    size_delta_str = f"{size_delta_pct:+.1f}% vs model A"

    lines = [
        "=" * 72,
        "  compare-dcp: DCP / camera-model A/B comparison report",
        f"  Generated: {timestamp}",
        "=" * 72,
        "",
        "=== Summary ===",
        f"  Input dir : {input_dir}",
        f"  Triplet   : {triplet[0].name}, {triplet[1].name}, {triplet[2].name}",
        f"  Model A   : {model_a}",
        f"  Model B   : {model_b}",
        "",
        "=== File sizes ===",
        f"  DNG A  ({model_a[:40]}): {size_a:,} bytes  [{dng_a}]",
        f"  DNG B  ({model_b[:40]}): {size_b:,} bytes  [{dng_b}]",
        f"  Delta: {size_delta_str}",
        "",
        "=== Tag diff (alphabetical) ===",
        *tag_table_lines,
        "",
        "=== Instructions ===",
        "  These two DNGs contain IDENTICAL image data — only the camera-model",
        "  tag (UniqueCameraModel) differs. They are an A/B test for which",
        "  DNG tag set makes Lightroom offer more useful Camera Profiles.",
        "",
        f"  DNG A ({model_a}):",
        f"    {dng_a}",
        "    In Lightroom Develop module → Profile → Camera Matching:",
        "    You will see 'Adobe Standard' only (default Scanlight model).",
        "",
        f"  DNG B ({model_b}):",
        f"    {dng_b}",
        "    In Lightroom Develop module → Profile → Camera Matching:",
        "    You should see Cobalt Spectre profiles (Sony ILCE-7CR / A7CR).",
        "    Choose 'Cobalt Spectre Standard' or 'Cobalt Spectre Faithful'.",
        "",
        "  Steps:",
        "  1. Import BOTH DNGs into Lightroom (File > Import).",
        "  2. Select DNG A, go to Develop module, open Profile Browser.",
        "     Note which Camera Matching profiles are available.",
        "  3. Switch to DNG B and compare profiles available.",
        "  4. If Cobalt Spectre profiles appear on DNG B: use MODEL_SONY for",
        "     production scans that will be processed with Cobalt profiles.",
        "  5. If profiles differ between A and B: the tag is working.",
        "     Pick whichever model+profile combination gives best NLP results.",
        "",
        "  NOTE: --keep-dngs must be passed to compare-dcp.py for the DNG",
        "  files listed above to persist after the script exits.",
        "=" * 72,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """Run the DCP A/B comparison.

    Args:
        argv: argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 / 1 / 2).
    """
    parser = argparse.ArgumentParser(
        prog="compare-dcp",
        description=(
            "Run rgb-composite twice (Scanlight v4 and Sony ILCE-7CR camera "
            "models) on the first ARW triplet found in INPUT_DIR, then write a "
            "tag-by-tag diff report to INPUT_DIR/.compare-dcp-report.txt."
        ),
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing R/G/B ARW triplet files",
    )
    parser.add_argument(
        "--keep-dngs",
        action="store_true",
        default=False,
        help=(
            "Copy both DNGs into INPUT_DIR/_compare-dcp-output/ before the "
            "temp directory is cleaned, so you can open them in Lightroom."
        ),
    )
    args = parser.parse_args(argv)
    input_dir = args.input_dir.resolve()

    # 1. Locate triplet.
    try:
        triplet = _find_triplet(input_dir)
    except FileNotFoundError as exc:
        print(f"compare-dcp: {exc}", file=sys.stderr)
        return 2

    r_path, g_path, b_path = triplet

    # 2. Run rgb-composite twice inside a temp directory.
    with tempfile.TemporaryDirectory(prefix="compare-dcp-") as tmp_dir:
        tmp = Path(tmp_dir)
        dng_a_tmp = tmp / "model_scanlight.dng"
        dng_b_tmp = tmp / "model_sony.dng"

        # Run model A (Scanlight).
        rc_a, stdout_a, stderr_a = _run_composite(
            r_path, g_path, b_path, dng_a_tmp, MODEL_SCANLIGHT
        )
        # Run model B (Sony ILCE-7CR).
        rc_b, stdout_b, stderr_b = _run_composite(
            r_path, g_path, b_path, dng_b_tmp, MODEL_SONY
        )

        # 3. Surface any subprocess failures.
        if rc_a != 0:
            print(
                f"compare-dcp: rgb-composite failed for model={MODEL_SCANLIGHT!r}:\n{stderr_a}",
                file=sys.stderr,
            )
        if rc_b != 0:
            print(
                f"compare-dcp: rgb-composite failed for model={MODEL_SONY!r} (Sony ILCE-7CR):\n{stderr_b}",
                file=sys.stderr,
            )
        if rc_a != 0 or rc_b != 0:
            return 1

        # 4. Parse DNG tags.
        try:
            tags_a = read_linear_dng_tags(dng_a_tmp)
            tags_b = read_linear_dng_tags(dng_b_tmp)
        except Exception as exc:
            print(f"compare-dcp: failed to read DNG tags: {exc}", file=sys.stderr)
            return 1

        size_a = os.path.getsize(dng_a_tmp)
        size_b = os.path.getsize(dng_b_tmp)

        # 5. Build and write report.
        report_body = _format_report(
            input_dir=input_dir,
            triplet=triplet,
            dng_a=dng_a_tmp,
            dng_b=dng_b_tmp,
            tags_a=tags_a,
            tags_b=tags_b,
            model_a=MODEL_SCANLIGHT,
            model_b=MODEL_SONY,
            size_a=size_a,
            size_b=size_b,
        )
        report_path = input_dir / REPORT_FILENAME
        report_path.write_text(report_body)

        # 6. Optionally copy DNGs out of the temp dir before it auto-cleans.
        if args.keep_dngs:
            out_subdir = input_dir / "_compare-dcp-output"
            out_subdir.mkdir(exist_ok=True)
            dng_a_dest = out_subdir / "model_scanlight.dng"
            dng_b_dest = out_subdir / "model_sony.dng"
            shutil.copy2(dng_a_tmp, dng_a_dest)
            shutil.copy2(dng_b_tmp, dng_b_dest)

    # 7. Print report location.
    print(str(report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
