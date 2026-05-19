"""End-to-end CLI tests — drive `scanlight.cli.main` with a stubbed Scanlight."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from scanlight import cli


@dataclass
class StubScanlight:
    """Captures calls in lieu of a real Scanlight."""

    port: str = "<stub>"
    calls: list = field(default_factory=list)
    fw_version: tuple = (10, 20)
    default_rgb: tuple = (5, 6, 7)
    last_temp_c: Optional[float] = 25.0
    last_vbus_mv: Optional[int] = 5050

    def set_color(self, r=0, g=0, b=0, w=0, save=False):
        self.calls.append(("set_color", r, g, b, w, save))

    def off(self):
        self.calls.append(("off",))
        self.set_color(0, 0, 0, 0)

    def get_fw_version(self, timeout=2.0):
        self.calls.append(("get_fw_version",))
        return self.fw_version

    def get_default_rgb(self, timeout=2.0):
        self.calls.append(("get_default_rgb",))
        return self.default_rgb

    def pulse_shutter(self, pulse_ms=100):
        self.calls.append(("pulse_shutter", pulse_ms))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def stub(monkeypatch):
    instance = StubScanlight()
    monkeypatch.setattr(cli, "Scanlight", lambda port=None: instance)
    return instance


def test_on_red_default_level(stub):
    rc = cli.main(["on", "r"])
    assert rc == 0
    assert ("set_color", 255, 0, 0, 0, False) in stub.calls


def test_on_green_custom_level(stub):
    rc = cli.main(["on", "g", "--level", "128"])
    assert rc == 0
    assert ("set_color", 0, 128, 0, 0, False) in stub.calls


def test_on_white(stub):
    rc = cli.main(["on", "w", "--level", "200"])
    assert rc == 0
    assert ("set_color", 0, 0, 0, 200, False) in stub.calls


def test_off(stub):
    rc = cli.main(["off"])
    assert rc == 0
    assert any(c[0] == "off" for c in stub.calls)


def test_set_rgb_does_not_save(stub):
    rc = cli.main(["set", "--r", "50", "--g", "100", "--b", "150"])
    assert rc == 0
    assert ("set_color", 50, 100, 150, 0, False) in stub.calls


def test_set_default_sets_save_flag(stub):
    rc = cli.main(["set-default", "--r", "60", "--g", "120", "--b", "180"])
    assert rc == 0
    assert ("set_color", 60, 120, 180, 0, True) in stub.calls


def test_status_prints_and_returns_zero(stub, capsys):
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "firmware:" in out
    assert "10" in out  # firmware version
    assert "default RGB" in out
    assert "5050" in out  # vbus mv


def test_invalid_channel_value_rejected(capsys):
    # argparse exits with code 2 on bad arg; we let it propagate via SystemExit
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["on", "r", "--level", "999"])


def test_pulse_default_100ms(stub):
    rc = cli.main(["pulse"])
    assert rc == 0
    assert ("pulse_shutter", 100) in stub.calls


def test_pulse_explicit_value(stub):
    rc = cli.main(["pulse", "300"])
    assert rc == 0
    assert ("pulse_shutter", 300) in stub.calls


@pytest.mark.parametrize("bad", ["5", "2551", "105", "0.1", "abc"])
def test_pulse_rejects_invalid(bad):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["pulse", bad])


def test_white_with_set_not_supported(stub):
    """`set` always sets W=0 — confirm via inspection of recorded call."""
    cli.main(["set", "--r", "10", "--g", "20", "--b", "30"])
    last = stub.calls[-1]
    assert last == ("set_color", 10, 20, 30, 0, False)


def test_error_path_returns_nonzero(monkeypatch, capsys):
    """A failure inside the context manager produces a clear error and rc=1."""

    class Broken:
        def __enter__(self):
            raise RuntimeError("could not open port")

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(cli, "Scanlight", lambda port=None: Broken())
    rc = cli.main(["off"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not open port" in err
