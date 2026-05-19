"""End-to-end automation harness for `scanlight-swift-cli`.

This is the bridge between the Swift driver and an AI agent / CI: every
test here builds and invokes the real CLI binary as a subprocess, parses
JSON, and asserts on the result. No XCTest, no in-process Swift bridging
— exactly the surface a Claude/Codex agent or a CI runner would use.

Run from the repo root:
  python3 -m pytest scripts/test_swift_cli.py

Tests use `--fake` for the transport so no hardware is required. The
real-port path is left for plug-in day.

Design notes for future automation:
- The Swift CLI emits one-line JSON with a stable schema:
    {"ok": bool, "command": str, ...command-specific fields...}
- `selftest` emits a `steps` array, one dict per check; each step has
  `name`, `ok`, and `message`.
- Exit codes are reliable: 0 success, 1 operational failure, 2 bad args.
- The CLI is hermetic in `--fake` mode (no filesystem side effects,
  no real serial port, no environment dependence).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SWIFT_PROJECT = REPO_ROOT / "phase3" / "FilmScanner"
CLI_BINARY_NAME = "scanlight-swift-cli"


def _swift_executable_path() -> Path:
    """Resolve the built CLI binary path. We expect a `swift build` to
    have produced it in `.build/debug/` or `.build/release/`."""
    for build_flavor in ("debug", "release"):
        candidate = SWIFT_PROJECT / ".build" / build_flavor / CLI_BINARY_NAME
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        f"Swift CLI binary not found. Run `swift build` in {SWIFT_PROJECT} first."
    )


@pytest.fixture(scope="session")
def swift_cli() -> Path:
    """Build (once per session) and return the path to `scanlight-swift-cli`.

    Done in a session-scoped fixture so 10+ test invocations don't each
    pay the build cost. A clean `swift build` is ~1.5 s; incremental is
    sub-second.
    """
    try:
        subprocess.run(
            ["swift", "build", "--package-path", str(SWIFT_PROJECT)],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        pytest.skip("swift toolchain not on $PATH; can't build the CLI for tests")
    except subprocess.CalledProcessError as e:
        pytest.fail(f"swift build failed:\n{e.stderr.decode()}")
    return _swift_executable_path()


def run_cli(swift_cli: Path, *args: str, expect_rc: int = 0) -> dict:
    """Invoke the CLI and return parsed JSON. Asserts on the exit code.

    All test invocations should use `--json` so this parser is stable.
    """
    cmd = [str(swift_cli), *args]
    if "--json" not in args:
        cmd.append("--json")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == expect_rc, (
        f"unexpected rc {proc.returncode} (expected {expect_rc})\n"
        f"args: {args}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    # `--help` and bad-arg paths print usage rather than JSON. Skip
    # parsing when stdout is clearly not JSON.
    stdout = proc.stdout.strip()
    if not stdout or not stdout.startswith("{"):
        return {"_raw_stdout": stdout, "_raw_stderr": proc.stderr}
    return json.loads(stdout)


# ---------- selftest: the canonical "does this work at all" check ----------

def test_selftest_passes(swift_cli):
    """The whole point of selftest: it should pass without hardware.
    Every AI agent and CI hook will call this first."""
    result = run_cli(swift_cli, "selftest")
    assert result["ok"] is True
    assert result["command"] == "selftest"
    assert result["pass_count"] == result["step_count"]
    assert result["pass_count"] >= 6  # at least the core protocol checks


def test_selftest_step_names_are_stable(swift_cli):
    """Lock down the step names so external dashboards can pattern-match."""
    result = run_cli(swift_cli, "selftest")
    names = {step["name"] for step in result["steps"]}
    expected = {
        "fw_version_request",
        "default_rgb_request",
        "set_color_packet_bytes",
        "pulse_shutter_packet_bytes",
        "pulse_shutter_rejects_invalid",
        "telemetry_led_temp",
        "telemetry_vbus",
        "white_with_rgb_rejected",
    }
    missing = expected - names
    assert not missing, f"selftest is missing steps: {missing}"


# ---------- status: fake-transport round-trip ----------

def test_status_returns_versions_and_telemetry(swift_cli):
    result = run_cli(swift_cli, "status", "--fake")
    assert result["ok"] is True
    assert result["firmware_id"] == 1
    assert result["hardware_id"] == 1
    # Default RGB synthesized by FakeTransport
    assert result["default_rgb"] == [255, 200, 180]
    # Telemetry synthesized on each interaction
    assert result["led_temp_c"] == pytest.approx(32.5, abs=0.01)
    assert result["vbus_mv"] == 5050


# ---------- on / off / set ----------

def test_on_red_default_level(swift_cli):
    result = run_cli(swift_cli, "on", "r", "--fake")
    assert result["ok"] is True
    assert result["command"] == "on"
    assert result["channel"] == "r"
    assert result["level"] == 255


def test_on_green_custom_level(swift_cli):
    result = run_cli(swift_cli, "on", "g", "--level", "128", "--fake")
    assert result["ok"] is True
    assert result["channel"] == "g"
    assert result["level"] == 128


def test_on_white_high_level(swift_cli):
    result = run_cli(swift_cli, "on", "w", "--level", "200", "--fake")
    assert result["ok"] is True
    assert result["channel"] == "w"


def test_off_succeeds(swift_cli):
    result = run_cli(swift_cli, "off", "--fake")
    assert result["ok"] is True
    assert result["command"] == "off"


def test_set_all_three_channels(swift_cli):
    result = run_cli(swift_cli, "set", "--r", "50", "--g", "100", "--b", "150", "--fake")
    assert result["ok"] is True
    assert result["r"] == 50 and result["g"] == 100 and result["b"] == 150


# ---------- pulse ----------

def test_pulse_default_works(swift_cli):
    result = run_cli(swift_cli, "pulse", "100", "--fake")
    assert result["ok"] is True
    assert result["pulse_ms"] == 100


def test_pulse_max_value(swift_cli):
    result = run_cli(swift_cli, "pulse", "2550", "--fake")
    assert result["ok"] is True
    assert result["pulse_ms"] == 2550


@pytest.mark.parametrize("bad_ms", ["5", "7", "2551", "105", "abc"])
def test_pulse_rejects_invalid(swift_cli, bad_ms):
    """Out-of-range, non-multiple-of-10, or non-numeric should exit 2 (bad args)
    when the value can't be parsed, or 1 (operational failure) when the
    Swift driver rejects it. Either way: non-zero, with a non-success JSON."""
    proc = subprocess.run(
        [str(swift_cli), "pulse", bad_ms, "--fake", "--json"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0


# ---------- error / argument-handling paths ----------

def test_unknown_command_returns_2(swift_cli):
    proc = subprocess.run(
        [str(swift_cli), "doesnotexist", "--fake", "--json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


def test_missing_subject_on(swift_cli):
    """`on` without a channel is invalid."""
    proc = subprocess.run(
        [str(swift_cli), "on", "--fake", "--json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


def test_help_succeeds(swift_cli):
    proc = subprocess.run([str(swift_cli), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "scanlight-swift-cli" in proc.stdout


# ---------- JSON contract stability ----------

def test_json_output_is_single_line(swift_cli):
    """AI agents and log scrapers depend on one JSON object per invocation."""
    proc = subprocess.run(
        [str(swift_cli), "status", "--fake", "--json"],
        capture_output=True, text=True,
    )
    stdout = proc.stdout.strip()
    # Should be exactly one line of JSON.
    assert stdout.count("\n") == 0
    json.loads(stdout)  # raises on bad JSON


def test_json_has_ok_field_on_every_command(swift_cli):
    """The `ok` key is the universal success/fail signal."""
    for args in (
        ("status",),
        ("on", "r"),
        ("off",),
        ("set", "--r", "10", "--g", "20", "--b", "30"),
        ("pulse", "100"),
        ("selftest",),
    ):
        result = run_cli(swift_cli, *args, "--fake")
        assert "ok" in result, f"{args} response missing 'ok': {result}"
        assert isinstance(result["ok"], bool)


def test_command_field_echoes_request(swift_cli):
    """Every response includes the command name for correlation."""
    for cmd in ("status", "off", "selftest"):
        result = run_cli(swift_cli, cmd, "--fake")
        assert result["command"] == cmd
