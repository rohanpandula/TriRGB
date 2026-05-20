"""Flask app — single page, one Capture Triplet button, no JS framework.

Intentionally minimal. Phase 3's native Swift app replaces this; don't
invest more here than the operator needs to push a button per frame.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from scanlight import Scanlight

from .orchestrator import CaptureSettings, Orchestrator


def create_app(orchestrator: Orchestrator, composite_worker=None) -> Flask:
    """Build the Flask app around a pre-built Orchestrator.

    Passing the orchestrator in (rather than constructing inside) makes
    integration tests trivial: hand it an orchestrator that's been
    fed a fake Scanlight + fake runner.
    """
    app = Flask(__name__)
    app.config["ORCHESTRATOR"] = orchestrator

    @app.get("/")
    def index():
        return render_template("index.html", settings=orchestrator.settings)

    @app.get("/api/state")
    def get_state():
        return jsonify(_settings_dict(orchestrator.settings))

    @app.post("/api/settings")
    def post_settings():
        data = request.get_json(force=True, silent=True) or {}
        try:
            # Whitelist fields so HTTP can't set arbitrary attrs.
            allowed = {
                "roll_name", "frame_number", "output_folder",
                "level_r", "level_g", "level_b", "settle_ms",
            }
            updates = {k: v for k, v in data.items() if k in allowed}
            # Coerce ints
            for k in ("frame_number", "level_r", "level_g", "level_b", "settle_ms"):
                if k in updates:
                    updates[k] = int(updates[k])
            settings = orchestrator.update_settings(**updates)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(_settings_dict(settings))

    @app.post("/api/capture")
    def post_capture():
        retake_str = request.args.get("retake", "")
        retake = retake_str.lower() in ("1", "true", "yes")
        result = orchestrator.capture_triplet(retake=retake)
        return jsonify({
            "success": result.success,
            "frame_number": result.frame_number,
            "files": {k: str(v) for k, v in result.files.items()},
            "error": result.error,
            "duration_s": round(result.duration_s, 2),
            "next_frame": orchestrator.settings.frame_number,
        }), (200 if result.success else 500)

    @app.get("/api/composite-status")
    def get_composite_status():
        if composite_worker is None:
            return jsonify({"enabled": False})
        # Drain any freshly-finished jobs into the worker's own history, then return
        # that full history. The worker is the single source of truth, so results
        # the capture-loop logger's poll() drained first are NOT lost here.
        composite_worker.poll()
        results = [
            {
                "frame_number": cr.frame_number,
                "status": "done" if cr.ok else "failed",
                "output_path": str(cr.output_path) if cr.output_path else None,
                "error": cr.error,
            }
            for cr in composite_worker.history
        ]
        return jsonify({
            "enabled": True,
            "pending": composite_worker.pending,
            "results": results,
        })

    return app


def _settings_dict(s: CaptureSettings) -> dict:
    return {
        "roll_name": s.roll_name,
        "frame_number": s.frame_number,
        "output_folder": str(s.output_folder),
        "level_r": s.level_r,
        "level_g": s.level_g,
        "level_b": s.level_b,
        "settle_ms": s.settle_ms,
    }


# ---------- standalone entry point ----------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="triplet-capture",
        description=(
            "Per-frame R/G/B capture orchestrator. Starts a local web UI; "
            "one Capture Triplet button per frame. Advance film by hand "
            "between frames."
        ),
    )
    p.add_argument("--roll-name", default="Roll001")
    p.add_argument(
        "--output-folder",
        default="/tmp/scans",
        help="Where the captured RAWs land. {output_folder}/{roll_name}/ is used.",
    )
    p.add_argument("--port", help="Scanlight serial port (auto-discovered by default).")
    p.add_argument("--host", default="127.0.0.1", help="Bind address for the web UI.")
    p.add_argument("--web-port", type=int, default=8765)
    p.add_argument(
        "--trigger-mode",
        choices=("sdk", "hw"),
        default="sdk",
        help=(
            "How to fire the camera. 'sdk' = Mac → USB → SDK (default, "
            "uses sony-capture). 'hw' = Scanlight pulses its 3.5mm trigger "
            "output; the camera saves over Wi-Fi to Imaging Edge Desktop, "
            "and we pick the file up from --ied-inbox."
        ),
    )
    p.add_argument(
        "--ied-inbox",
        default=None,
        help=(
            "Imaging Edge Desktop's save folder (required when "
            "--trigger-mode hw). Files arriving here are moved into "
            "the roll's canonical naming convention."
        ),
    )
    p.add_argument(
        "--shutter-pulse-ms",
        type=int,
        default=100,
        help=(
            "Pulse length for the Scanlight's 3.5mm trigger output. "
            "Must be a multiple of 10 in [10, 2550] ms. Default 100 ms "
            "matches the canonical app_bsl Vue app. Sony bodies usually "
            "work at 100; bump to 300 if you see missed shots."
        ),
    )
    p.add_argument(
        "--capture-timeout-s",
        type=int,
        default=30,
        help=(
            "Per-channel capture timeout. In sdk mode this caps the "
            "sony-capture subprocess; in hw mode this caps how long we "
            "wait for a file to land in --ied-inbox after pulsing. "
            "Bump to 60+ on slow Wi-Fi."
        ),
    )
    p.add_argument(
        "--inbox-stable-for-s",
        type=float,
        default=3.0,
        help=(
            "How long an ARW file's size must hold steady in --ied-inbox "
            "before we treat the download as complete. Default 3.0 s — "
            "bump if you see partial files moved (signals: downstream "
            "decode failures, truncated RAFs). hw mode only."
        ),
    )
    p.add_argument(
        "--inbox-poll-interval-s",
        type=float,
        default=0.2,
        help="How often to re-scan --ied-inbox while waiting. hw mode only.",
    )
    p.add_argument("--sony-capture", default="sony-capture",
                   help="Path to the sony-capture binary (default: search $PATH).")
    p.add_argument(
        "--stream-composite",
        action="store_true",
        help=(
            "Composite each triplet in the background as it completes, so the "
            "roll is fully composited by the time you capture the last frame "
            "(no end-of-roll wait). Outputs land in <output>/<roll>/composites/ "
            "— identical to running batch-composite afterward."
        ),
    )
    p.add_argument(
        "--composite-format",
        choices=("tiff", "dng", "both"),
        default="dng",
        help="Output format for --stream-composite (default dng).",
    )
    p.add_argument(
        "--composite-workers",
        type=int,
        default=4,
        help="Max concurrent background composites for --stream-composite (default 4).",
    )
    p.add_argument(
        "--ffc-calibration",
        default=None,
        help=(
            "FFC calibration triplet directory (R/G/B blanks). Passed to "
            "--stream-composite. Omit to skip flat-field correction."
        ),
    )
    p.add_argument(
        "--camera-model",
        default=None,
        help=(
            "UniqueCameraModel string for the composite DNG (e.g. "
            "'Sony ILCE-7CR' or 'FUJIFILM GFX100 II') so Lightroom offers the "
            "matching camera profile. Only used with --stream-composite."
        ),
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Start in headless mode; suppresses the startup browser-launch hint.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.trigger_mode == "hw" and not args.ied_inbox:
        print(
            "triplet-capture: --trigger-mode hw requires --ied-inbox PATH "
            "(Imaging Edge Desktop's save folder).",
            file=sys.stderr,
        )
        return 2

    output_folder = Path(args.output_folder) / args.roll_name
    settings_kwargs = dict(
        roll_name=args.roll_name,
        frame_number=1,
        output_folder=output_folder,
        sony_capture_path=args.sony_capture,
        sony_capture_timeout_s=args.capture_timeout_s,
        trigger_mode=args.trigger_mode,
        shutter_pulse_ms=args.shutter_pulse_ms,
        inbox_stable_for_s=args.inbox_stable_for_s,
        inbox_poll_interval_s=args.inbox_poll_interval_s,
    )
    if args.ied_inbox:
        settings_kwargs["ied_inbox"] = Path(args.ied_inbox)
    try:
        settings = CaptureSettings(**settings_kwargs)
    except ValueError as e:
        print(f"triplet-capture: {e}", file=sys.stderr)
        return 2

    try:
        scanlight = Scanlight(port=args.port)
    except Exception as e:
        print(f"triplet-capture: could not open Scanlight: {e}", file=sys.stderr)
        return 1

    # Optional streaming compositor: kicks off rgb-composite per triplet as
    # it completes, so the roll is done by the time the last frame is shot.
    composite_worker = None
    on_triplet_complete = None
    if args.stream_composite:
        from .composite_worker import CompositeWorker

        composite_worker = CompositeWorker(
            output_folder / "composites",
            args.roll_name,
            max_workers=args.composite_workers,
            output_format=args.composite_format,
            ffc_calibration_dir=Path(args.ffc_calibration) if args.ffc_calibration else None,
            dng_camera_model=args.camera_model,
        )

        def on_triplet_complete(result):
            if result.success:
                composite_worker.submit(result.frame_number, result.files)
            # Surface any finished composites' failures to the log promptly.
            for cr in composite_worker.poll():
                if not cr.ok:
                    logging.warning("background composite frame %d failed: %s",
                                    cr.frame_number, cr.error)

        print(
            f"triplet-capture: streaming composites → {output_folder / 'composites'} "
            f"({args.composite_format}, {args.composite_workers} workers)",
            file=sys.stderr,
        )

    try:
        orch = Orchestrator(scanlight, settings, on_triplet_complete=on_triplet_complete)
        app = create_app(orch, composite_worker=composite_worker)
        print(f"triplet-capture: web UI on http://{args.host}:{args.web_port}",
              file=sys.stderr)
        if args.no_browser:
            print("triplet-capture: headless mode (--no-browser)", file=sys.stderr)
        # Use Flask's built-in server — disposable tool, no production
        # concerns. `use_reloader=False` so we don't double-open the serial port.
        app.run(host=args.host, port=args.web_port, debug=False, use_reloader=False)
        return 0
    finally:
        if composite_worker is not None:
            # Let outstanding composites finish before we exit so the roll is
            # complete on disk. Logs each result; never raises.
            for cr in composite_worker.drain():
                if not cr.ok:
                    logging.warning("background composite frame %d failed: %s",
                                    cr.frame_number, cr.error)
            composite_worker.shutdown(wait=True)
        scanlight.close()


if __name__ == "__main__":
    sys.exit(main())
