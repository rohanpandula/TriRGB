"""Streaming composite worker.

Runs `rgb_composite.composite_triplet` in the background as each triplet
completes, so by the time the operator finishes capturing the last frame
of a roll, all the earlier frames are already composited. Eliminates the
~3-minute end-of-roll wait of the batch path.

Design:
  - A bounded ProcessPoolExecutor (default 4 workers). Submitting more
    triplets than workers just queues them; the pool drains at its own
    pace while capture continues.
  - Process isolation: a composite that crashes (corrupt RAW, FFC shape
    mismatch, rawpy segfault) takes down only its own worker process, not
    the capture orchestrator. The failure is captured and surfaced via
    poll()/drain(), never raised into the capture loop.
  - Idempotent with the batch path: this writes the exact same
    `<roll>/composites/<roll>_Frame<NNN>.{tif,dng}` files that
    `batch-composite` would. If the operator kills the orchestrator
    mid-roll, re-running `batch-composite` on the roll dir fills any gaps.

This module deliberately mirrors batch_composite's worker contract
(`_composite_one`) so both paths produce byte-identical output.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("triplet_capture.composite_worker")

DEFAULT_MAX_WORKERS = 4


@dataclass
class CompositeResult:
    """Outcome of one background composite job."""
    frame_number: int
    output_path: Optional[Path]  # None on failure
    error: Optional[str] = None  # set on failure

    @property
    def ok(self) -> bool:
        return self.error is None and self.output_path is not None


def _composite_job(
    r_path: str,
    g_path: str,
    b_path: str,
    output_path: str,
    output_format: str,
    ffc_calibration_dir: Optional[str],
    dng_camera_model: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Top-level worker entrypoint (must be picklable for ProcessPoolExecutor).

    Returns (output_path_or_None, error_message_or_None). Mirrors
    batch_composite._composite_one's contract so both paths behave the same.
    All args are plain strings/None so they pickle cleanly across the
    process boundary; Paths are reconstructed inside.
    """
    # Imported inside the worker so this module imports without rawpy on
    # the path (arg-parsing, tests that never spawn a real composite).
    from rgb_composite import composite_triplet

    try:
        out = composite_triplet(
            Path(r_path),
            Path(g_path),
            Path(b_path),
            Path(output_path),
            ffc_calibration_dir=Path(ffc_calibration_dir) if ffc_calibration_dir else None,
            output_format=output_format,
            dng_camera_model=dng_camera_model,
        )
        return (str(out), None)
    except Exception as exc:  # noqa: BLE001 — isolate every failure
        return (None, f"{type(exc).__name__}: {exc}")


class CompositeWorker:
    """Bounded background compositor.

    Args:
        output_dir: where composites land. Created if missing. Convention
            is `<roll_dir>/composites/` to match batch-composite.
        roll_name: used to name outputs `<roll>_Frame<NNN>.tif`.
        max_workers: concurrent composite processes (default 4).
        output_format: "tiff" | "dng" | "both" — passed to composite_triplet.
        ffc_calibration_dir: optional FFC cal triplet dir.
        dng_camera_model: optional UniqueCameraModel override (Sony/Fuji).
    """

    def __init__(
        self,
        output_dir: Path,
        roll_name: str,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        output_format: str = "dng",
        ffc_calibration_dir: Optional[Path] = None,
        dng_camera_model: Optional[str] = None,
        executor=None,
        job_fn=None,
    ):
        """
        Args (testing hooks):
            executor: a concurrent.futures Executor. Defaults to
                ProcessPoolExecutor(max_workers) — process isolation so a
                crashing composite can't take down the capture orchestrator.
                Tests inject a ThreadPoolExecutor for determinism.
            job_fn: the per-triplet worker function. Defaults to the
                module-level `_composite_job`. Tests inject a fake so they
                don't need rawpy or real ARWs. (Injecting it also sidesteps
                the ProcessPool + monkeypatch limitation: child processes
                re-import the original module, so a top-level monkeypatch is
                invisible to them; explicit injection works regardless of
                executor type.)
        """
        self._output_dir = Path(output_dir)
        self._roll_name = roll_name
        self._output_format = output_format
        self._ffc = Path(ffc_calibration_dir) if ffc_calibration_dir else None
        self._camera_model = dng_camera_model
        self._executor = executor or ProcessPoolExecutor(max_workers=max_workers)
        self._job_fn = job_fn or _composite_job
        self._futures: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._closed = False
        # Full history of every collected result, in completion order. Both
        # poll() callers (the capture-loop logger in main() and the
        # /api/composite-status route) append here, so neither permanently loses
        # results the other drained first.
        self._history: list[CompositeResult] = []
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _output_path(self, frame_number: int) -> Path:
        # Always .tif here; composite_triplet swaps the suffix to .dng when
        # output_format == "dng" (matches batch-composite's _output_path).
        return self._output_dir / f"{self._roll_name}_Frame{frame_number:03d}.tif"

    def submit(self, frame_number: int, files: dict[str, Path]) -> None:
        """Queue a triplet for background compositing. Non-blocking.

        `files` must have 'R', 'G', 'B' keys → final RAW paths (the
        TripletResult.files dict from the orchestrator). Re-submitting the
        same frame_number replaces the prior pending job (last write wins —
        matters for retakes).
        """
        for ch in ("R", "G", "B"):
            if ch not in files:
                raise ValueError(f"submit() needs R/G/B keys; missing {ch!r}")
        out_path = self._output_path(frame_number)
        with self._lock:
            if self._closed:
                raise RuntimeError("CompositeWorker is closed; cannot submit")
            fut = self._executor.submit(
                self._job_fn,
                str(files["R"]),
                str(files["G"]),
                str(files["B"]),
                str(out_path),
                self._output_format,
                str(self._ffc) if self._ffc else None,
                self._camera_model,
            )
            self._futures[frame_number] = fut
        logger.info("queued composite for frame %d → %s", frame_number, out_path.name)

    def poll(self) -> list[CompositeResult]:
        """Return results for jobs finished since the last poll. Non-blocking.

        Completed futures are removed from the tracking set so a long roll
        doesn't accumulate handles. Call this periodically (e.g., after each
        capture_triplet) to surface failures to the operator promptly.

        Thread-safe: Flask runs with threaded=True by default, so /api/capture
        and /api/composite-status may call poll() concurrently.
        """
        with self._lock:
            done: list[CompositeResult] = []
            for frame_number in list(self._futures.keys()):
                fut = self._futures[frame_number]
                if fut.done():
                    done.append(self._collect(frame_number, fut))
                    del self._futures[frame_number]
            # Record every collected result so a second poll() caller (the
            # capture-loop logger vs /api/composite-status) can't make the other
            # permanently miss it.
            self._history.extend(done)
        return done

    def drain(self, timeout: Optional[float] = None) -> list[CompositeResult]:
        """Block until every outstanding composite finishes. Returns all
        results. Call at end-of-roll. `timeout` is per-future (seconds).
        """
        with self._lock:
            pending = list(self._futures.items())
            self._futures.clear()
        results: list[CompositeResult] = []
        for frame_number, fut in pending:
            try:
                fut.result(timeout=timeout)
            except Exception:  # noqa: BLE001 — _collect re-reads the outcome
                pass
            results.append(self._collect(frame_number, fut))
        # End-of-roll results belong in the history too.
        with self._lock:
            self._history.extend(results)
        return results

    @staticmethod
    def _collect(frame_number: int, fut: Future) -> CompositeResult:
        try:
            out_str, err = fut.result(timeout=0)
        except Exception as exc:  # noqa: BLE001 — worker process died hard
            return CompositeResult(frame_number, None, f"{type(exc).__name__}: {exc}")
        if err is not None:
            logger.warning("composite frame %d failed: %s", frame_number, err)
            return CompositeResult(frame_number, None, err)
        logger.info("composite frame %d complete: %s", frame_number, out_str)
        return CompositeResult(frame_number, Path(out_str) if out_str else None, None)

    @property
    def pending(self) -> int:
        """Number of submitted-but-not-yet-collected jobs."""
        with self._lock:
            return len(self._futures)

    @property
    def history(self) -> list[CompositeResult]:
        """Snapshot of every collected result so far, in completion order.

        Non-destructive — read it as often as you like. This is the single
        source of truth for /api/composite-status, independent of which caller
        drained a given result via poll().
        """
        with self._lock:
            return list(self._history)

    def shutdown(self, *, wait: bool = False) -> None:
        """Tear down the pool. `wait=False` (default) cancels queued jobs and
        returns immediately — used when the operator kills the session
        mid-roll. `wait=True` lets running jobs finish (use drain() first if
        you also want their results)."""
        # Flip _closed under the same lock submit() checks it under, so a
        # concurrent submit() can't slip a job onto a pool that's shutting down.
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
        except TypeError:
            # cancel_futures kwarg added in 3.9; fall back for older runtimes.
            self._executor.shutdown(wait=wait)

    # Context-manager sugar so callers can `with CompositeWorker(...) as w:`
    def __enter__(self) -> "CompositeWorker":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # On clean exit, let outstanding jobs finish; on exception, bail fast.
        if exc_type is None:
            self.drain()
        self.shutdown(wait=exc_type is None)
