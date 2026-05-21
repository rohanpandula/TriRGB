"""Walk a roll directory and composite every frame.

Phase 2 / Deliverable 2C of the film scanner build — see `../../PROJECT.md`.

Per PROJECT.md §"File and directory conventions":

    {output_root}/{roll_name}/
        {roll_name}_Frame{NNN}_R.ARW
        {roll_name}_Frame{NNN}_G.ARW
        {roll_name}_Frame{NNN}_B.ARW
        scan_log.jsonl
        composites/
            {roll_name}_Frame{NNN}.tif

`composite_roll(path)` reads the per-frame triplets and writes TIFFs into
the sibling `composites/` directory. Missing-channel frames are logged and
skipped, not fatal. libraw is not thread-safe within a frame but cleanly
parallelizable across frames, so we use process-pool concurrency.
"""
from __future__ import annotations

import argparse
import enum
import logging
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


logger = logging.getLogger("batch-composite")

# A single 61MP frame holds ~350MB of uint16 per demosaic; the compositor
# briefly has three in flight plus the output array (~1.4GB per worker).
# Default to a conservative cap so a full roll on a 16GB Mac doesn't OOM —
# users can opt into more parallelism with --workers.
_DEFAULT_WORKER_CAP = 4


FRAME_PATTERN = re.compile(
    r"""
    ^
    (?P<roll>.+?)_Frame(?P<frame>\d{3})_(?P<channel>[RGB])\.ARW
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


class SkipReason(str, enum.Enum):
    MISSING_CHANNEL = "missing_channel"
    OUTPUT_EXISTS = "output_exists"


@dataclass(frozen=True)
class FrameGroup:
    """All ARWs for a single frame.

    Channels start out as None and are populated as files are discovered.
    A frame is "complete" only when all three channels are present.
    """
    roll: str
    frame_number: int
    r: Optional[Path] = None
    g: Optional[Path] = None
    b: Optional[Path] = None

    @property
    def complete(self) -> bool:
        return self.r is not None and self.g is not None and self.b is not None

    @property
    def missing_channels(self) -> list[str]:
        out = []
        if self.r is None: out.append("R")
        if self.g is None: out.append("G")
        if self.b is None: out.append("B")
        return out


@dataclass
class BatchResult:
    composited: list[Path] = field(default_factory=list)
    skipped: list[tuple[FrameGroup, SkipReason]] = field(default_factory=list)
    failed: list[tuple[FrameGroup, str]] = field(default_factory=list)


def discover_frames(roll_dir: Path) -> list[FrameGroup]:
    """Scan `roll_dir` for ARW files and group by (roll_name, frame_number).

    Files that don't match the `{roll}_Frame{NNN}_{R|G|B}.ARW` convention
    are ignored (debug log). Files from different rolls are grouped
    independently — a directory containing both `RollA_Frame001_R.ARW` and
    `RollB_Frame001_G.ARW` produces two distinct (incomplete) groups, never
    a silent merge. (Per project convention there is one roll per
    directory, but defending against operator error is cheap.)

    Returns a list sorted first by roll name then by frame number.
    """
    by_key: dict[tuple[str, int], dict[str, Path]] = {}

    for entry in sorted(roll_dir.iterdir()):
        if not entry.is_file():
            continue
        m = FRAME_PATTERN.match(entry.name)
        if not m:
            logger.debug("ignoring non-matching file: %s", entry.name)
            continue
        frame = int(m.group("frame"))
        ch = m.group("channel").upper()
        roll_name = m.group("roll")
        channels = by_key.setdefault((roll_name, frame), {})
        if ch in channels:
            # The pattern is case-insensitive, so e.g. `_r.ARW` and `_R.ARW`
            # both map to channel "R". Don't silently drop one — warn so the
            # operator can fix the stray-case file.
            logger.warning(
                "duplicate %s channel for %s Frame%03d: %s overwrites %s "
                "(case-insensitive filename match)",
                ch, roll_name, frame, entry.name, channels[ch].name,
            )
        channels[ch] = entry

    distinct_rolls = {roll for (roll, _) in by_key}
    if len(distinct_rolls) > 1:
        logger.warning(
            "found multiple roll names in %s: %s — treating as separate frames",
            roll_dir, sorted(distinct_rolls),
        )

    groups = []
    for (roll, frame) in sorted(by_key):
        ch = by_key[(roll, frame)]
        groups.append(
            FrameGroup(
                roll=roll,
                frame_number=frame,
                r=ch.get("R"),
                g=ch.get("G"),
                b=ch.get("B"),
            )
        )
    return groups


def _composite_one(args: tuple) -> tuple[FrameGroup, Optional[Path], Optional[str]]:
    """Worker entrypoint — runnable in a separate process.

    Returns (group, output_path_or_None, error_message_or_None).
    """
    group, output_path, ffc_dir, output_format, dng_camera_model = args
    # Imported inside the worker so the top-level module can be imported
    # without rawpy on the path (e.g. for arg parsing).
    from rgb_composite import composite_triplet

    try:
        out = composite_triplet(
            group.r,
            group.g,
            group.b,
            output_path,
            ffc_calibration_dir=ffc_dir,
            output_format=output_format,
            dng_camera_model=dng_camera_model,
        )
        return group, out, None
    except Exception as exc:
        return group, None, f"{type(exc).__name__}: {exc}"


def _output_path(roll_dir: Path, group: FrameGroup) -> Path:
    """Composites land in a sibling `composites/` directory.

    Path is always `.tif`; if `output_format` is `dng`, `composite_triplet`
    swaps the suffix internally. If `both`, the TIFF lives here and the
    DNG sibling lives next to it.
    """
    return (
        roll_dir
        / "composites"
        / f"{group.roll}_Frame{group.frame_number:03d}.tif"
    )


def _existing_output_for_format(out_tif: Path, output_format: str) -> bool:
    """Decide if a frame is already done given the desired output format."""
    if output_format == "tiff":
        return out_tif.exists()
    if output_format == "dng":
        return out_tif.with_suffix(".dng").exists()
    # both — only "done" if both files are on disk
    return out_tif.exists() and out_tif.with_suffix(".dng").exists()


def composite_roll(
    roll_dir: Path,
    *,
    workers: Optional[int] = None,
    overwrite: bool = False,
    ffc_calibration_dir: Optional[Path] = None,
    output_format: str = "tiff",
    dng_camera_model: Optional[str] = None,
) -> BatchResult:
    """Composite every complete frame in `roll_dir`.

    Args:
        roll_dir: directory containing `{roll}_Frame{NNN}_{R|G|B}.ARW` files.
        workers: process-pool size. None → `min(os.cpu_count(), 4)` to keep
            memory peak bounded (each worker holds ~1.4GB of decoded RAW
            data at peak). Pass an explicit value to override. Pass `1` to
            run inline, single-threaded (useful for tests and debugging).
        overwrite: if False, skip frames whose output already exists (for
            the chosen `output_format`).
        ffc_calibration_dir: passed straight through to `composite_triplet`
            — directory containing R.ARW, G.ARW, B.ARW blank-light cal
            frames. Required for clean narrowband-RGB scans.
        output_format: "tiff" (default), "dng", or "both" — passed to
            `composite_triplet`.

    Returns:
        BatchResult summarizing composited / skipped / failed frames.
    """
    roll_dir = Path(roll_dir)
    if not roll_dir.is_dir():
        raise NotADirectoryError(f"not a directory: {roll_dir}")

    groups = discover_frames(roll_dir)
    composites_dir = roll_dir / "composites"
    composites_dir.mkdir(exist_ok=True)

    result = BatchResult()
    work: list[tuple] = []

    for g in groups:
        if not g.complete:
            logger.warning(
                "skipping Frame%03d (%s): missing channels %s",
                g.frame_number, g.roll, ",".join(g.missing_channels),
            )
            result.skipped.append((g, SkipReason.MISSING_CHANNEL))
            continue
        out = _output_path(roll_dir, g)
        if _existing_output_for_format(out, output_format) and not overwrite:
            logger.info("skipping Frame%03d: output exists (%s)", g.frame_number, out)
            result.skipped.append((g, SkipReason.OUTPUT_EXISTS))
            continue
        # `None` flows straight through to composite_triplet, which
        # resolves it to its own DEFAULT_DNG_CAMERA_MODEL. Keeps the
        # default in one place and avoids cross-package imports.
        work.append(
            (g, out, ffc_calibration_dir, output_format, dng_camera_model)
        )

    if not work:
        return result

    if workers is None:
        cpu = os.cpu_count() or 1
        workers = min(cpu, _DEFAULT_WORKER_CAP)

    if workers == 1:
        # Inline path — useful for tests, debugging, and very small jobs.
        for w in work:
            group, out, err = _composite_one(w)
            if err is None:
                result.composited.append(out)
                logger.info("composited Frame%03d → %s", group.frame_number, out)
            else:
                result.failed.append((group, err))
                logger.error("Frame%03d failed: %s", group.frame_number, err)
        return result

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_composite_one, w): w[0] for w in work}
        for fut in as_completed(futures):
            group, out, err = fut.result()
            if err is None:
                result.composited.append(out)
                logger.info("composited Frame%03d → %s", group.frame_number, out)
            else:
                result.failed.append((group, err))
                logger.error("Frame%03d failed: %s", group.frame_number, err)

    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="batch-composite",
        description=(
            "Walk a roll directory of narrowband-RGB triplets and composite "
            "every frame. Outputs land in a sibling `composites/` directory."
        ),
    )
    p.add_argument("roll_dir", help="Directory containing {roll}_Frame{NNN}_{R|G|B}.ARW files")
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            f"Process-pool size (default: min(cpu_count, {_DEFAULT_WORKER_CAP}) "
            "to keep peak memory bounded; pass 1 for inline)"
        ),
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-composite frames even if the output already exists.",
    )
    p.add_argument(
        "--ffc-calibration",
        metavar="DIR",
        default=None,
        help=(
            "Directory containing R.ARW, G.ARW, B.ARW blank-light captures. "
            "Per-channel Flat Field Correction is applied to every frame in "
            "the roll. Strongly recommended for narrowband-RGB scanning."
        ),
    )
    p.add_argument(
        "--format",
        choices=("tiff", "dng", "both"),
        default="tiff",
        help=(
            "Output format. 'tiff' (default) = 16-bit linear ProPhoto TIFF. "
            "'dng' = Linear DNG (LR/Capture One open it as RAW). 'both' = "
            "write both side-by-side."
        ),
    )
    p.add_argument(
        "--camera-model",
        default=None,
        help=(
            "Value for the DNG UniqueCameraModel tag. Defaults to the "
            "Scanlight composite identifier; set to \"Sony ILCE-7CR\" "
            "to let Lightroom offer Sony camera profiles (Cobalt "
            "Spectre, Adobe Standard, etc.). Ignored for --format tiff."
        ),
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="DEBUG-level logging.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        result = composite_roll(
            Path(args.roll_dir),
            workers=args.workers,
            overwrite=args.overwrite,
            ffc_calibration_dir=Path(args.ffc_calibration) if args.ffc_calibration else None,
            output_format=args.format,
            dng_camera_model=args.camera_model,
        )
    except NotADirectoryError as e:
        print(f"batch-composite: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"batch-composite: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(
        f"composited: {len(result.composited)}  "
        f"skipped: {len(result.skipped)}  "
        f"failed: {len(result.failed)}"
    )
    # Surface dimension-mismatch failures distinctly — these mean the film
    # physically moved between the R/G/B exposures (reshoot the frame), not a
    # decode/IO error. _composite_one tags each failure with the exception type.
    dim_mismatch = [
        g for (g, err) in result.failed
        if err.startswith("DimensionMismatchError")
    ]
    if dim_mismatch:
        print(
            "batch-composite: %d frame(s) had a dimension mismatch (film likely "
            "moved between exposures — reshoot): %s"
            % (
                len(dim_mismatch),
                ", ".join(f"Frame{g.frame_number:03d}" for g in dim_mismatch),
            ),
            file=sys.stderr,
        )
    # Empty-roll guard (codex audit): if discovery found zero matching
    # files at all, the operator probably has a misnamed directory or
    # wrong file extension. Don't return silent success.
    if not result.composited and not result.failed and not result.skipped:
        print(
            f"batch-composite: no frames matching "
            "'{roll}_Frame{NNN}_{R|G|B}.ARW' were found in "
            f"{args.roll_dir} — check the directory and filename pattern.",
            file=sys.stderr,
        )
        return 2
    return 0 if not result.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
