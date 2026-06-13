"""Flask app — single page, one Capture Triplet button, no JS framework.

Intentionally minimal. Phase 3's native Swift app replaces this; don't
invest more here than the operator needs to push a button per frame.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    app.config["PROGRESS_STARTED_AT"] = None
    app.config["LAST_CAL_RESULT"] = None
    app.config["LAST_CAL_CALL_ID"] = None
    app.config["CURRENT_CAL_CALL_ID"] = None

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
        # F1: which channel the operator should fire right now (null when idle).
        state["waiting_for_channel"] = orchestrator.waiting_for_channel
        return jsonify(state)

    @app.post("/api/settings")
    def post_settings():
        data = request.get_json(force=True, silent=True) or {}
        # A valid JSON list/string/number has no .items(); reject non-objects as
        # a 400 client error rather than letting it surface as a 500.
        if not isinstance(data, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        # Claim the activity slot for the (brief) update so it cannot interleave
        # with a capture or a blocking calibration. Those hold the activity but
        # release Orchestrator._lock between single-channel captures, so mutating
        # levels/shutters/output mid-run could corrupt an exposure bisection or
        # the roll. Same guard the capture/calibrate/preview-light routes use.
        if not orchestrator.try_begin_activity("update_settings"):
            return jsonify({"error": "A capture or calibration is in progress — wait for it to finish."}), 409
        try:
            # Whitelist fields so HTTP can't set arbitrary attrs.
            allowed = {
                "roll_name", "frame_number", "output_folder",
                "level_r", "level_g", "level_b", "settle_ms",
                "shutter_r", "shutter_g", "shutter_b",
                "inbox_stable_for_s",  # F3: expose stability window
            }
            updates = {k: v for k, v in data.items() if k in allowed}
            # Coerce ints
            for k in ("frame_number", "level_r", "level_g", "level_b", "settle_ms"):
                if k in updates:
                    updates[k] = int(updates[k])
            for k in ("shutter_r", "shutter_g", "shutter_b"):
                if k in updates:
                    updates[k] = str(updates[k]).strip() or None
            # F3: inbox_stable_for_s is a float
            if "inbox_stable_for_s" in updates:
                updates["inbox_stable_for_s"] = float(updates["inbox_stable_for_s"])
            settings = orchestrator.update_settings(**updates)
        except (ValueError, TypeError) as e:
            # TypeError covers JSON null reaching int()/float() coercion — a
            # client input error (400), not an internal server error (500).
            return jsonify({"error": str(e)}), 400
        finally:
            orchestrator.end_activity()
        return jsonify(_settings_dict(settings))

    @app.post("/api/capture")
    def post_capture():
        retake_str = request.args.get("retake", "")
        retake = retake_str.lower() in ("1", "true", "yes")
        # Bug-2: claim the exclusive-activity slot. A calibration holding the
        # slot will cause this to return 409 so camera commands don't interleave.
        if not orchestrator.try_begin_activity("capture"):
            return jsonify({
                "success": False,
                "frame_number": orchestrator.settings.frame_number,
                "files": {},
                "error": "a capture or calibration is already in progress",
                "duration_s": 0.0,
                "next_frame": orchestrator.settings.frame_number,
            }), 409
        try:
            result = orchestrator.capture_triplet(retake=retake)
        finally:
            orchestrator.end_activity()
        return jsonify({
            "success": result.success,
            "frame_number": result.frame_number,
            "files": {k: str(v) for k, v in result.files.items()},
            "error": result.error,
            "duration_s": round(result.duration_s, 2),
            "next_frame": orchestrator.settings.frame_number,
        }), (200 if result.success else 500)

    @app.get("/api/calibrate/progress")
    def get_calibrate_progress():
        call_id = request.args.get("call_id") or None
        recent_events = _recent_scan_log_events(
            orchestrator.settings.output_folder,
            since=app.config.get("PROGRESS_STARTED_AT"),
            call_id=call_id,
        )
        if not recent_events:
            return jsonify({
                "event": "idle",
                "message": "Waiting for calibration capture to start.",
                "recent_events": [],
            })

        summarized_events = [_summarized_calibration_event(e) for e in recent_events]
        response = dict(summarized_events[-1])
        response["recent_events"] = summarized_events
        return jsonify(response)

    @app.get("/api/calibrate/exposure-result")
    def get_calibrate_exposure_result():
        requested_call_id = request.args.get("call_id") or None
        result = app.config.get("LAST_CAL_RESULT", None)
        if result is None:
            return jsonify({"error": "No completed exposure calibration result yet."}), 404
        if requested_call_id is not None and requested_call_id != app.config.get("LAST_CAL_CALL_ID"):
            return jsonify({"error": "No completed exposure calibration result for this run."}), 404

        body = json.loads(result.to_json())
        if app.config.get("LAST_CAL_CALL_ID"):
            body["call_id"] = app.config["LAST_CAL_CALL_ID"]
        return jsonify(body)

    @app.post("/api/calibrate/preview-light")
    def post_calibrate_preview_light():
        orch = app.config["ORCHESTRATOR"]
        # Claim the single activity slot for the whole operation. This is
        # atomically mutually exclusive with capture and the blocking calibrate
        # routes (they claim the same slot), closing the check-then-lock TOCTOU
        # a separate current_activity probe + _lock.acquire would leave open.
        if not orch.try_begin_activity("preview_light"):
            return jsonify({"error": "A capture or calibration is in progress — wait for it to finish."}), 409

        try:
            data = request.get_json(force=True, silent=True) or {}
            enabled = bool(data.get("enabled", False))
            try:
                level = int(data.get("level", 200))
            except (TypeError, ValueError) as e:
                return jsonify({"error": f"level must be an integer: {e}"}), 400
            if not 0 <= level <= 255:
                return jsonify({"error": f"level must be 0-255, got {level}"}), 400

            if enabled and level > 0:
                orch._scanlight.set_color(r=0, g=0, b=0, w=level)
                return jsonify({"enabled": True, "level": level})

            orch._scanlight.off()
            return jsonify({"enabled": False, "level": 0})
        except Exception as e:  # noqa: BLE001 - surface hardware errors as JSON
            return jsonify({"error": str(e)}), 500
        finally:
            orch.end_activity()

    @app.post("/api/calibrate/exposure")
    def post_calibrate_exposure():
        orch = app.config["ORCHESTRATOR"]
        # Bug-2: claim the exclusive-activity slot so concurrent captures
        # are rejected (409) for the entire calibration duration, not just
        # the brief TOCTOU window of the old check-then-unlock pattern.
        if not orch.try_begin_activity("calibrate_exposure"):
            return jsonify({
                "error": "A scan or calibration is in progress — wait for it to finish.",
                "call_id": app.config.get("CURRENT_CAL_CALL_ID"),
            }), 409

        try:
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

            try:
                target_fraction = float(data.get("target_fraction", 0.85))
            except (ValueError, TypeError) as e:
                return jsonify({"error": f"target_fraction must be numeric: {e}"}), 400
            if not 0.5 <= target_fraction <= 0.9:
                return jsonify({
                    "error": (
                        "target_fraction must be between 0.50 and 0.90 "
                        f"(got {target_fraction:.3f})"
                    )
                }), 400

            try:
                seed_recipe = _parse_exposure_seed_recipe(data)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            call_id = str(data.get("call_id") or uuid.uuid4())
            app.config["CURRENT_CAL_CALL_ID"] = call_id
            app.config["LAST_CAL_RESULT"] = None
            app.config["LAST_CAL_CALL_ID"] = None

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
                _append_calibration_log(
                    orch,
                    "calibration_started",
                    call_id=call_id,
                    rebate_region={
                        "x": base_region.x,
                        "y": base_region.y,
                        "w": base_region.w,
                        "h": base_region.h,
                        "source": base_region.source,
                    },
                    has_seed=seed_recipe is not None,
                    target_fraction=target_fraction,
                )
                result = calibrate_exposure(
                    orch._scanlight,
                    orch.settings,
                    base_region,
                    orchestrator=orch,
                    sleep=lambda _: None,
                    demosaic_factory=demosaic_factory,
                    seed_recipe=seed_recipe,
                    call_id=call_id,
                    target_fraction=target_fraction,
                )
                app.config["LAST_CAL_RESULT"] = result
                app.config["LAST_CAL_CALL_ID"] = call_id
                body = _json.loads(result.to_json())
                body["call_id"] = call_id
                return jsonify(body)
            except (RuntimeError, ValueError) as e:
                return jsonify({"error": str(e)}), 500
            finally:
                if app.config.get("CURRENT_CAL_CALL_ID") == call_id:
                    app.config["CURRENT_CAL_CALL_ID"] = None
        finally:
            orch.end_activity()

    @app.post("/api/calibrate/ffc")
    def post_calibrate_ffc():
        orch = app.config["ORCHESTRATOR"]
        # Bug-2: use the activity guard for the full FFC duration.
        if not orch.try_begin_activity("calibrate_ffc"):
            return jsonify({"error": "A scan or calibration is in progress — wait for it to finish."}), 409

        try:
            data = request.get_json(force=True, silent=True) or {}
            try:
                n_frames = int(data.get("n_frames", 8))
            except (ValueError, TypeError):
                return jsonify({"error": "n_frames must be an integer"}), 400
            if n_frames < 1:
                # capture_flats() raises ValueError for n_frames < 1; reject it
                # here as a client error (400), not an unhandled 500.
                return jsonify({"error": "n_frames must be >= 1"}), 400

            # Get black levels from last calibration or defaults
            last_cal = app.config.get("LAST_CAL_RESULT", None)
            if last_cal is not None:
                black_levels = (last_cal.r, last_cal.g, last_cal.b)
                # FIX-C: restore the orchestrator to calibrated per-channel LED levels before
                # capturing flats. calibrate_exposure leaves the orchestrator at its last
                # single-channel probe state (only one channel lit at a time), so flats would
                # be captured at wrong/dark levels on hardware without this restore.
                # Pre-hardware tests use an injected FLAT_DEMOSAIC_FN so levels don't affect them.
                orch.update_settings(
                    level_r=last_cal.r.led_level,
                    level_g=last_cal.g.led_level,
                    level_b=last_cal.b.led_level,
                    shutter_r=last_cal.r.shutter_speed or None,
                    shutter_g=last_cal.g.shutter_speed or None,
                    shutter_b=last_cal.b.shutter_speed or None,
                )
            elif all(k in data for k in (
                "led_level_r", "led_level_g", "led_level_b",
                "black_level_r", "black_level_g", "black_level_b",
            )):
                from c41_core import ChannelCalibration
                try:
                    black_levels = (
                        ChannelCalibration(
                            channel="R",
                            led_level=int(data["led_level_r"]),
                            black_level=float(data["black_level_r"]),
                            gain=1.0,
                            clip_fraction=0.0,
                            shutter_speed=str(data.get("shutter_r", "") or ""),
                        ),
                        ChannelCalibration(
                            channel="G",
                            led_level=int(data["led_level_g"]),
                            black_level=float(data["black_level_g"]),
                            gain=1.0,
                            clip_fraction=0.0,
                            shutter_speed=str(data.get("shutter_g", "") or ""),
                        ),
                        ChannelCalibration(
                            channel="B",
                            led_level=int(data["led_level_b"]),
                            black_level=float(data["black_level_b"]),
                            gain=1.0,
                            clip_fraction=0.0,
                            shutter_speed=str(data.get("shutter_b", "") or ""),
                        ),
                    )
                except (ValueError, TypeError) as e:
                    return jsonify({"error": f"calibration levels must be numeric: {e}"}), 400
                try:
                    orch.update_settings(
                        level_r=black_levels[0].led_level,
                        level_g=black_levels[1].led_level,
                        level_b=black_levels[2].led_level,
                        shutter_r=black_levels[0].shutter_speed or None,
                        shutter_g=black_levels[1].shutter_speed or None,
                        shutter_b=black_levels[2].shutter_speed or None,
                    )
                except ValueError as e:
                    # Out-of-range levels (numeric but invalid) raise from
                    # __post_init__ — a 400, not an unhandled 500.
                    return jsonify({"error": f"calibration levels out of range: {e}"}), 400
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
                #
                # WR-02 note: this bypass keeps the route hardware-free for TESTS (via the
                # injected FLAT_DEMOSAIC_FN). The PRODUCTION real-demosaic path calls
                # _demosaic_cal_frame → `from .composite import demosaic_linear` (relative
                # import inside ffc.py), which requires rawpy at runtime. rawpy IS present on
                # real hardware (M2). Do NOT pre-load rgb_composite.composite here — composite.py
                # imports rawpy at module level, which would break the hardware-free invariant.
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
                    # Reuse the MAIN orchestrator's runner (orch._runner), not
                    # orch._explicit_runner (None in production). In SDK mode that
                    # runner owns the live `sony-capture --persist` session, so
                    # capture_flats shares it instead of spawning a SECOND persist
                    # process that would fight the first for the camera and leak.
                    sony_capture_runner=orch._runner,
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
        finally:
            orch.end_activity()

    @app.post("/api/calibrate/checks")
    def post_calibrate_checks():
        orch = app.config["ORCHESTRATOR"]
        # Bug-2: guard against running checks while a capture is in progress
        # (checks reads LAST_CAL_FRAME which may be updating concurrently).
        if not orch.try_begin_activity("calibrate_checks"):
            return jsonify({"error": "A scan or calibration is in progress — wait for it to finish."}), 409

        try:
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
                    try:
                        _cspec.loader.exec_module(_checks_mod)
                    except ImportError as _ie:
                        # cv2 (or another checks.py dependency) is missing; return a clean error
                        # rather than an opaque traceback. cv2 is present via rgb-composite on real
                        # hardware, so this is a defensive guard for non-standard environments.
                        del sys.modules["_rgb_composite_checks"]
                        return jsonify({"error": f"checks module missing dependency: {_ie}"}), 500
                check_base_neutrality = _checks_mod.check_base_neutrality
                check_registration = _checks_mod.check_registration
                # frame_anomaly (per-frame vs roll baseline) is a roll-level check — deferred
                # to Phase 15; no roll baseline exists during single-calibration.

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

                return jsonify([_json.loads(c.to_json()) for c in checks])
            except (RuntimeError, ValueError) as e:
                return jsonify({"error": str(e)}), 500
        finally:
            orch.end_activity()

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
                "positive_output_path": str(cr.positive_path) if cr.positive_path else None,
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
        "shutter_r": s.shutter_r,
        "shutter_g": s.shutter_g,
        "shutter_b": s.shutter_b,
        # F3: inbox stability window, surfaced so the Swift app can display
        # and configure it.  Default 3.0s; lower on fast 5GHz Wi-Fi.
        "inbox_stable_for_s": s.inbox_stable_for_s,
        # Trigger mode is read-only via state; write via POST /api/settings.
        "trigger_mode": s.trigger_mode,
    }


def _append_calibration_log(orch: Orchestrator, event: str, **kwargs: Any) -> None:
    log_path = orch.settings.output_folder / "scan_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    Orchestrator._append_log(
        log_path,
        event,
        frame=orch.settings.frame_number,
        roll=orch.settings.roll_name,
        **kwargs,
    )


def _latest_scan_log_event(output_folder: Path) -> Optional[dict[str, Any]]:
    events = _recent_scan_log_events(output_folder, limit=1)
    return events[-1] if events else None


def _recent_scan_log_events(
    output_folder: Path,
    limit: int = 40,
    since: Optional[datetime] = None,
    call_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    log_path = output_folder / "scan_log.jsonl"
    if not log_path.exists():
        return []

    try:
        lines = log_path.read_text().splitlines()
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            if call_id is not None and parsed.get("call_id") != call_id:
                continue
            if since is not None and _event_is_before(parsed, since):
                break
            events.append(parsed)
            if len(events) >= limit:
                break
    events.reverse()
    return events


def _event_is_before(event: dict[str, Any], since: datetime) -> bool:
    raw_ts = event.get("ts")
    if not isinstance(raw_ts, str) or not raw_ts:
        return False
    try:
        event_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if event_ts.tzinfo is None:
        event_ts = event_ts.replace(tzinfo=timezone.utc)
    return event_ts < since


def _parse_exposure_seed_recipe(data: dict[str, Any]) -> Optional[dict[str, tuple[int, str]]]:
    raw = data.get("seed")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("seed must be an object keyed by R/G/B")

    seed: dict[str, tuple[int, str]] = {}
    for channel in ("R", "G", "B"):
        raw_channel = raw.get(channel) or raw.get(channel.lower())
        if raw_channel is None:
            continue
        if not isinstance(raw_channel, dict):
            raise ValueError(f"seed.{channel} must be an object")
        try:
            level = int(raw_channel.get("led_level", raw_channel.get("level")))
        except (TypeError, ValueError) as e:
            raise ValueError(f"seed.{channel}.led_level must be an integer: {e}") from e
        if not 0 <= level <= 255:
            raise ValueError(f"seed.{channel}.led_level must be 0-255, got {level}")
        shutter = str(raw_channel.get("shutter_speed", "") or "").strip()
        seed[channel] = (level, shutter)

    return seed or None


def _summarized_calibration_event(event: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "ts",
        "event",
        "call_id",
        "channel",
        "phase",
        "level",
        "shutter_speed",
        "label",
        "p99",
        "p999",
        "target",
        "clip_fraction",
        "sensor_clip_fraction",
        "output_clip_fraction",
        "next_level",
        "next_shutter",
        "source_level",
        "source_shutter",
        "source_p999",
        "solve_hint",
        "exposure_status",
        "limit_reason",
        "converged",
        "error",
    }
    summarized = {k: event[k] for k in keys if k in event}
    summarized["message"] = _calibration_progress_message(event)
    return summarized


def _calibration_progress_message(event: dict[str, Any]) -> str:
    name = str(event.get("event", "idle"))
    channel = str(event.get("channel") or "")
    level = event.get("level")
    shutter = event.get("shutter_speed")
    label = str(event.get("label") or "")
    try:
        numeric_level = int(level or 0)
    except (TypeError, ValueError):
        numeric_level = 0

    if name == "calibration_started":
        return "Exposure calibration started; preparing the camera and Scanlight."
    if name == "single_capture_start":
        if label == "dark-frame":
            return "Capturing dark frame with the Scanlight off."
        return f"Starting {channel} exposure probe at LED {level}, shutter {shutter or 'camera-current'}."
    if name == "scanlight_on":
        if label == "dark-frame":
            return "Scanlight is off for the dark frame."
        if numeric_level <= 0:
            return f"Scanlight is off for {label or channel or 'dark'} capture."
        return f"Scanlight {channel} is on at LED {level}, shutter {shutter or 'camera-current'}."
    if name == "sony_capture_start":
        if label == "dark-frame":
            return f"Camera is capturing/downloading the dark frame at shutter {shutter or 'camera-current'}."
        return f"Camera is capturing/downloading {channel} RAW at shutter {shutter or 'camera-current'}."
    if name == "sony_capture_ok":
        return f"Captured {channel} RAW; measuring rebate exposure."
    if name == "calibration_probe":
        p99 = event.get("p99")
        p999 = event.get("p999")
        clip_fraction = event.get("clip_fraction")
        sensor_clip_fraction = event.get("sensor_clip_fraction")
        target = event.get("target")
        next_level = event.get("next_level")
        status = str(event.get("exposure_status") or "measured")
        clip_text = f"clip {clip_fraction}"
        if sensor_clip_fraction is not None:
            clip_text = f"clip {clip_fraction}, sensor clip {sensor_clip_fraction}"
        if event.get("converged"):
            return (
                f"{channel} exposure is target at LED {level}, shutter {shutter or 'camera-current'}; "
                f"{clip_text}."
            )
        if next_level is not None:
            if status == "clipped":
                return (
                    f"{channel} is clipped at LED {level}, shutter {shutter or 'camera-current'}; "
                    f"{clip_text}. Backing off to LED {next_level}."
                )
            if status == "under":
                return (
                    f"{channel} is under target at LED {level}; p99.9 {p999} / {target}. "
                    f"Trying LED {next_level}."
                )
            if status == "hot":
                return (
                    f"{channel} is hot at LED {level}; p99.9 {p999} / {target}. "
                    f"Trying LED {next_level}."
                )
            return (
                f"Measured {channel} p99.9 {p999} / {target}, p99 {p99}, "
                f"{clip_text}; trying LED {next_level} next."
            )
        return f"Measured {channel} p99.9 {p999} / {target}, p99 {p99}, {clip_text}."
    if name == "calibration_solve":
        source_level = event.get("source_level")
        source_shutter = event.get("source_shutter")
        source_p999 = event.get("source_p999")
        next_level = event.get("next_level")
        next_shutter = event.get("next_shutter")
        target = event.get("target")
        return (
            f"Solved {channel} from LED {source_level}, shutter {source_shutter or 'camera-current'}, "
            f"p99.9 {source_p999} / {target}: try LED {next_level}, "
            f"shutter {next_shutter or 'camera-current'}."
        )
    if name == "calibration_shutter_escalate":
        source_level = event.get("source_level")
        source_shutter = event.get("source_shutter")
        source_p999 = event.get("source_p999")
        next_level = event.get("next_level")
        next_shutter = event.get("next_shutter")
        target = event.get("target")
        return (
            f"{channel} hit LED ceiling at shutter {source_shutter}; "
            f"p99.9 {source_p999} / {target}. Trying slower shutter "
            f"{next_shutter} at LED {next_level}."
        )
    if name == "calibration_channel_complete":
        raw_status = str(event.get("exposure_status") or "measured")
        status = raw_status.replace("_", " ")
        p99 = event.get("p99")
        target = event.get("target")
        clip_fraction = event.get("clip_fraction")
        if raw_status == "source_limited":
            status = "source RAW limited"
        elif raw_status == "clip_limited":
            status = "clean output limited"
        return (
            f"{channel} calibration finished: {status} at LED {level}, "
            f"shutter {shutter or 'camera-current'}; p99.9 {p99} / {target}, clip {clip_fraction}."
        )
    if name == "calibration_complete":
        levels = event.get("levels") or {}
        shutters = event.get("shutters") or {}
        statuses = event.get("statuses") or {}
        return (
            "Exposure calibration finished. "
            f"R {levels.get('R')} @ {shutters.get('R')} ({statuses.get('R')}), "
            f"G {levels.get('G')} @ {shutters.get('G')} ({statuses.get('G')}), "
            f"B {levels.get('B')} @ {shutters.get('B')} ({statuses.get('B')})."
        )
    if name in {"sony_capture_fail", "single_capture_abort", "triplet_abort"}:
        error = event.get("error")
        return f"Capture failed: {error}" if error else "Capture failed."
    if name == "single_capture_complete":
        if label == "dark-frame":
            return "Dark frame captured; starting RGB exposure probes."
        return f"{channel} exposure probe captured; measuring next adjustment."
    return name.replace("_", " ").capitalize() + "."


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
        choices=("sdk", "hw", "manual"),
        default="manual",
        help=(
            "How to fire the camera. 'manual' = operator fires the shutter in "
            "Imaging Edge Desktop while we set R/G/B and wait on --ied-inbox "
            "(default, no SDK and no Scanlight shutter pulse). 'hw' = Scanlight "
            "pulses its 3.5mm trigger output; the camera saves over Wi-Fi to "
            "IED and we pick the file up from --ied-inbox. 'sdk' = Sony SDK "
            "via sony-capture over Wi-Fi."
        ),
    )
    p.add_argument(
        "--ied-inbox",
        default=None,
        help=(
            "Imaging Edge Desktop's save folder (required when "
            "--trigger-mode hw or manual). Files arriving here are moved into "
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
            "sony-capture subprocess; in hw/manual mode this caps how long we "
            "wait for a file to land in --ied-inbox after the pulse or manual "
            "IED trigger. Bump to 60+ on slow Wi-Fi."
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
            "decode failures, truncated RAFs). hw/manual modes only."
        ),
    )
    p.add_argument(
        "--inbox-poll-interval-s",
        type=float,
        default=0.2,
        help="How often to re-scan --ied-inbox while waiting. hw/manual modes only.",
    )
    p.add_argument("--sony-capture", default="sony-capture",
                   help="Path to the sony-capture binary (default: search $PATH).")
    p.add_argument(
        "--sony-ip-address",
        default=None,
        help="Sony Camera Remote SDK Wi-Fi IP address for --trigger-mode sdk.",
    )
    p.add_argument(
        "--sony-mac-address",
        default=None,
        help="Optional Sony camera MAC address for SDK direct-IP sessions.",
    )
    p.add_argument(
        "--sony-user",
        default=None,
        help="Sony Access Authentication username for SDK Wi-Fi sessions.",
    )
    p.add_argument(
        "--sony-password",
        default=None,
        help="Sony Access Authentication password for SDK Wi-Fi sessions.",
    )
    p.add_argument(
        "--sony-iso",
        default="100or125",
        help=(
            "ISO value passed to sony-capture in SDK mode. Default '100or125' "
            "selects ISO 100 when available, otherwise ISO 125; ISO 50 is not used."
        ),
    )
    p.add_argument(
        "--shutter-r",
        default=None,
        help="SDK shutter speed for red captures, e.g. 1/4. Empty uses camera-current.",
    )
    p.add_argument(
        "--shutter-g",
        default=None,
        help="SDK shutter speed for green captures, e.g. 1/4. Empty uses camera-current.",
    )
    p.add_argument(
        "--shutter-b",
        default=None,
        help="SDK shutter speed for blue captures, e.g. 1/4. Empty uses camera-current.",
    )
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
        "--positive-profile-json",
        default=None,
        help=(
            "Base-region JSON from the selected stock calibration profile. "
            "Only used with --stream-composite. When present, each composite "
            "also gets a sibling *_positive.tif rendered with d-min balance, "
            "inversion, automatic levels, and a display curve."
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

    if args.trigger_mode in ("hw", "manual") and not args.ied_inbox:
        print(
            f"triplet-capture: --trigger-mode {args.trigger_mode} requires --ied-inbox PATH "
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
        sony_ip_address=args.sony_ip_address,
        sony_mac_address=args.sony_mac_address,
        sony_user=args.sony_user,
        sony_password=args.sony_password,
        sony_iso=args.sony_iso,
        shutter_r=args.shutter_r,
        shutter_g=args.shutter_g,
        shutter_b=args.shutter_b,
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
            positive_profile_json=args.positive_profile_json,
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

    orch = None
    try:
        orch = Orchestrator(scanlight, settings, on_triplet_complete=on_triplet_complete)
        app = create_app(orch, composite_worker=composite_worker, ready_nonce=args.ready_nonce)
        app.config["PROGRESS_STARTED_AT"] = datetime.now(timezone.utc)

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
        # Deterministically close the persistent sony-capture session (if any) so
        # the camera SDK session is released before this process exits — otherwise
        # a Swift-restarted backend can race a lingering --persist child for the
        # camera. No-op when no session was opened.
        if orch is not None:
            orch.close()
        scanlight.close()


if __name__ == "__main__":
    sys.exit(main())
