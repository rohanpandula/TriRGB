"""Flask app — single page, one Capture Triplet button, no JS framework.

Intentionally minimal. Phase 3's native Swift app replaces this; don't
invest more here than the operator needs to push a button per frame.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from scanlight import Scanlight

from .orchestrator import CaptureSettings, Orchestrator


def create_app(orchestrator: Orchestrator, composite_worker=None, ready_nonce: str = "") -> Flask:
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
        # ready_nonce lets the Swift launcher confirm it's talking to THIS spawned
        # orchestrator — not a foreign server that grabbed the port in the bind
        # probe's TOCTOU window. Empty string when launched without --ready-nonce.
        state = _settings_dict(orchestrator.settings)
        state["ready_nonce"] = ready_nonce
        return jsonify(state)

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

    @app.post("/api/calibrate/exposure")
    def post_calibrate_exposure():
        orch = app.config["ORCHESTRATOR"]
        # 409 if a capture is already in flight (non-blocking lock probe)
        lock_acquired = orch._lock.acquire(blocking=False)
        if not lock_acquired:
            return jsonify({"error": "capture in progress — try again after scan completes"}), 409
        orch._lock.release()

        data = request.get_json(force=True, silent=True) or {}

        # Parse + validate optional rebate coordinates (T-14-01)
        try:
            rebate_col = int(data["rebate_col"]) if "rebate_col" in data else None
            rebate_row = int(data["rebate_row"]) if "rebate_row" in data else None
            rebate_w = int(data.get("rebate_w", 100))
            rebate_h = int(data.get("rebate_h", 20))
        except (ValueError, TypeError) as e:
            return jsonify({"error": f"rebate parameter must be an integer: {e}"}), 400

        if rebate_col is not None and rebate_col < 0:
            return jsonify({"error": f"rebate_col must be >= 0, got {rebate_col}"}), 400
        if rebate_row is not None and rebate_row < 0:
            return jsonify({"error": f"rebate_row must be >= 0, got {rebate_row}"}), 400
        if rebate_w <= 0:
            return jsonify({"error": f"rebate_w must be > 0, got {rebate_w}"}), 400
        if rebate_h <= 0:
            return jsonify({"error": f"rebate_h must be > 0, got {rebate_h}"}), 400

        from c41_core.contracts import BaseRegionDescriptor
        if rebate_col is not None and rebate_row is not None:
            base_region = BaseRegionDescriptor(
                x=rebate_col, y=rebate_row, w=rebate_w, h=rebate_h,
                base_rgb=(8930.0, 12097.0, 2952.0),
                uniformity_cv=1.5,
                source="manual",
            )
        else:
            # Default centre strip
            base_region = BaseRegionDescriptor(
                x=4, y=4, w=100, h=20,
                base_rgb=(8930.0, 12097.0, 2952.0),
                uniformity_cv=1.5,
                source="auto",
            )

        demosaic_factory = app.config.get("DEMOSAIC_FACTORY", None)

        try:
            import json as _json
            from triplet_capture.calibrate_exposure import calibrate_exposure
            result = calibrate_exposure(
                orch._scanlight,
                orch.settings,
                base_region,
                orchestrator=orch,
                sleep=lambda _: None,
                demosaic_factory=demosaic_factory,
            )
            app.config["LAST_CAL_RESULT"] = result
            return jsonify(_json.loads(result.to_json()))
        except (RuntimeError, ValueError) as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/calibrate/ffc")
    def post_calibrate_ffc():
        orch = app.config["ORCHESTRATOR"]
        lock_acquired = orch._lock.acquire(blocking=False)
        if not lock_acquired:
            return jsonify({"error": "capture in progress — try again after scan completes"}), 409
        orch._lock.release()

        data = request.get_json(force=True, silent=True) or {}
        n_frames = int(data.get("n_frames", 8))

        # Get black levels from last calibration or defaults
        last_cal = app.config.get("LAST_CAL_RESULT", None)
        if last_cal is not None:
            black_levels = (last_cal.r, last_cal.g, last_cal.b)
        else:
            from c41_core import ChannelCalibration
            black_levels = (
                ChannelCalibration(channel="R", led_level=128, black_level=256.0, gain=1.0, clip_fraction=0.0),
                ChannelCalibration(channel="G", led_level=128, black_level=256.0, gain=1.0, clip_fraction=0.0),
                ChannelCalibration(channel="B", led_level=128, black_level=256.0, gain=1.0, clip_fraction=0.0),
            )

        flat_demosaic_fn = app.config.get("FLAT_DEMOSAIC_FN", None)

        try:
            import json as _json
            import importlib.util as _util
            from pathlib import Path as _Path
            from triplet_capture.capture_flats import capture_flats

            _phase2_rgb = _Path(__file__).resolve().parent.parent.parent.parent / "phase2" / "rgb-composite"
            _script_path = _Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "inspect-calibration.py"

            # Pre-load rgb_composite.ffc directly (bypassing rgb_composite/__init__.py which
            # eagerly imports composite.py → rawpy via its __all__ re-exports).
            # Must happen BEFORE calling capture_flats, which lazily imports
            # `from rgb_composite.ffc import _box_filter_2d` inside _cv().
            # Once "rgb_composite.ffc" is in sys.modules Python finds the cached
            # module without executing __init__.py.
            if "rgb_composite.ffc" not in sys.modules:
                _ffc_path = _phase2_rgb / "rgb_composite" / "ffc.py"
                _ffc_spec = _util.spec_from_file_location("rgb_composite.ffc", str(_ffc_path))
                _ffc_mod = _util.module_from_spec(_ffc_spec)
                sys.modules["rgb_composite.ffc"] = _ffc_mod
                _ffc_spec.loader.exec_module(_ffc_mod)

            flat_stack, meta = capture_flats(
                orch._scanlight,
                orch.settings,
                black_levels,
                n_frames=n_frames,
                sleep=lambda _: None,
                sony_capture_runner=orch._explicit_runner,
                demosaic_fn=flat_demosaic_fn,
            )

            # Build inspection dict matching inspect-calibration.py --json output
            # Import measure_channel + _channel_verdict + classify from the script.
            # Use importlib.util with a sanitised module name to avoid the hyphen.
            _ic = sys.modules.get("_inspect_calibration_mod")
            if _ic is None:
                _spec = _util.spec_from_file_location("_inspect_calibration_mod", str(_script_path))
                _ic = _util.module_from_spec(_spec)
                # Register before exec so nested imports can resolve cls.__module__
                sys.modules["_inspect_calibration_mod"] = _ic
                _spec.loader.exec_module(_ic)

            # flat_stack is NxHxWx3; average to get HxWx3
            flat_avg = flat_stack.mean(axis=0).astype(flat_stack.dtype)
            ch_stats = {}
            for ch, ch_idx in (("R", 0), ("G", 1), ("B", 2)):
                stats = _ic.measure_channel(flat_avg[:, :, ch_idx].astype("uint16"), ch)
                verdict = _ic._channel_verdict(stats)
                ch_stats[ch] = {
                    "falloff_pct": round(stats.falloff_pct, 2),
                    "uniformity_pct": round(stats.uniformity_pct, 2),
                    "verdict": verdict,
                }
            all_stats = (
                _ic.measure_channel(flat_avg[:, :, 0].astype("uint16"), "R"),
                _ic.measure_channel(flat_avg[:, :, 1].astype("uint16"), "G"),
                _ic.measure_channel(flat_avg[:, :, 2].astype("uint16"), "B"),
            )
            overall_msg, _ = _ic.classify(all_stats)
            # Extract the leading word (CLEAN / FAIL / OK-with-FFC)
            overall = overall_msg.split("—")[0].strip() if "—" in overall_msg else overall_msg.strip()

            inspection = {"channels": ch_stats, "overall": overall}
            return jsonify({
                "flat_field": _json.loads(meta.to_json()),
                "inspection": inspection,
            })
        except (RuntimeError, ValueError) as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/calibrate/checks")
    def post_calibrate_checks():
        orch = app.config["ORCHESTRATOR"]
        last_cal = app.config.get("LAST_CAL_RESULT", None)
        if last_cal is None:
            # fail-closed: return a well-formed CheckResult indicating "no calibration"
            from c41_core.contracts import CheckResult
            import json as _json
            no_cal = CheckResult(
                name="base_neutrality",
                passed=False,
                deltas={"base_r": 0.0, "base_g": 0.0, "base_b": 0.0},
            )
            return jsonify([_json.loads(no_cal.to_json())]), 409

        try:
            import json as _json
            import importlib.util as _util
            from pathlib import Path as _Path
            from c41_core.contracts import CheckResult

            # Import checks.py directly (bypassing rgb_composite/__init__.py which
            # eagerly imports rawpy via composite.py — breaking the hardware-free invariant).
            _checks_path = _Path(__file__).resolve().parent.parent.parent.parent / "phase2" / "rgb-composite" / "rgb_composite" / "checks.py"
            _checks_mod = sys.modules.get("_rgb_composite_checks")
            if _checks_mod is None:
                _cspec = _util.spec_from_file_location("_rgb_composite_checks", str(_checks_path))
                _checks_mod = _util.module_from_spec(_cspec)
                sys.modules["_rgb_composite_checks"] = _checks_mod
                _cspec.loader.exec_module(_checks_mod)
            check_base_neutrality = _checks_mod.check_base_neutrality
            check_frame_anomaly = _checks_mod.check_frame_anomaly
            check_registration = _checks_mod.check_registration

            checks: list = []

            # check_registration requires a HxWx3 float array (LAST_CAL_FRAME)
            last_frame = app.config.get("LAST_CAL_FRAME", None)
            if last_frame is not None:
                reg = check_registration(last_frame)
                checks.append(reg)
            else:
                # fail-closed but well-formed — registration not available pre-roll
                checks.append(CheckResult(name="registration", passed=False, deltas={}))

            bn = check_base_neutrality(last_cal.base_region)
            checks.append(bn)

            fa = check_frame_anomaly(last_cal.base_region, last_cal.base_region)
            checks.append(fa)

            return jsonify([_json.loads(c.to_json()) for c in checks])
        except (RuntimeError, ValueError) as e:
            return jsonify({"error": str(e)}), 500

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
        "--ready-nonce",
        default="",
        help=(
            "Opaque token echoed on GET /api/state as ready_nonce. The Swift "
            "launcher passes a random value and only treats the server as ready "
            "when it sees the matching token — so a foreign server that grabbed "
            "the port in the launcher's bind-probe gap can't falsely satisfy readiness."
        ),
    )
    p.add_argument(
        "--port-file",
        default="",
        help=(
            "Path to write the actual bound port to once the server is listening "
            "(used with --web-port 0 so the launcher learns the ephemeral port). "
            "Written atomically: a .tmp file is renamed into place so the reader "
            "never sees a partial/empty value."
        ),
    )
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
        app = create_app(orch, composite_worker=composite_worker, ready_nonce=args.ready_nonce)

        if args.web_port == 0:
            # Child-owned ephemeral port path: bind to port 0, let the OS pick,
            # then report the real port back to the Swift launcher via a port-file.
            # This eliminates the TOCTOU window that existed when Swift probed for
            # a free port and then passed it to us.
            from werkzeug.serving import make_server as _make_server

            server = _make_server(args.host, 0, app, threaded=True)
            bound_port = server.server_port

            # Write the real port atomically so the launcher never reads a
            # partial/empty value (write to <path>.tmp then os.replace).
            if args.port_file:
                port_file_path = args.port_file
                tmp_path_str = port_file_path + ".tmp"
                with open(tmp_path_str, "w") as _pf:
                    _pf.write(str(bound_port))
                os.replace(tmp_path_str, port_file_path)

            print(
                f"triplet-capture: web UI on http://{args.host}:{bound_port} "
                f"(ephemeral, --web-port 0)",
                file=sys.stderr,
            )
            if args.no_browser:
                print("triplet-capture: headless mode (--no-browser)", file=sys.stderr)

            # serve_forever() runs on a background daemon thread so that the
            # main thread is free to call server.shutdown() from a DIFFERENT
            # thread. Python's socketserver.BaseServer.shutdown() blocks until
            # serve_forever() returns — if both run on the same thread (i.e.
            # serve_forever blocks the main thread and the signal handler calls
            # shutdown() on that same thread) it deadlocks. The Python docs are
            # explicit: "shutdown() must be called while serve_forever() is
            # running in a different thread."
            #
            # The signal handler only sets a threading.Event (minimal,
            # signal-safe). The main thread parks on stop_event.wait(), then
            # calls server.shutdown() cross-thread — no deadlock.
            stop_event = threading.Event()

            def _shutdown_handler(signum, frame):
                stop_event.set()  # signal-safe: just flip a flag

            signal.signal(signal.SIGTERM, _shutdown_handler)
            signal.signal(signal.SIGINT, _shutdown_handler)

            serve_thread = threading.Thread(
                target=server.serve_forever,
                name="triplet-serve",
                daemon=True,
            )
            serve_thread.start()
            try:
                stop_event.wait()  # park until SIGTERM / SIGINT
            finally:
                server.shutdown()       # cross-thread: serve_thread runs serve_forever
                serve_thread.join(timeout=5)
            return 0
        else:
            # Non-zero port: use Flask's built-in server — existing behaviour,
            # manual launches / the web UI must not regress.
            # `use_reloader=False` so we don't double-open the serial port.
            print(f"triplet-capture: web UI on http://{args.host}:{args.web_port}",
                  file=sys.stderr)
            if args.no_browser:
                print("triplet-capture: headless mode (--no-browser)", file=sys.stderr)
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
