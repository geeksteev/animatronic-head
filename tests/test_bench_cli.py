"""Tests for bench.cli (python -m bench.motion_test) and the Extech meter adapter. No hardware."""

import json
from types import SimpleNamespace

import pytest

from bench.cli import main
from bench.flash import FlashError
from bench.instruments.extech_383273 import FrameError, Reading
from bench.instruments.extech_meter import ExtechMeter
from bench.meter import MeterError
from tests.bench_fakes import KEY, Rig


class FakeLog:
    def __init__(self, ip="192.0.2.77"):
        self.ip = ip
        self.closed = False
        self.waited = False

    def wait_for(self, regex, timeout_s, since_t=None):
        self.waited = True
        return (0.0, "[wifi] connected, IP %s, UDP port 4210" % self.ip) if self.ip else None

    def ip_address(self):
        return self.ip

    def failsafe_events(self):
        return []


def _factories(rig, log=None, flash=None, calls=None):
    f = {
        "flash": flash or (lambda project, port: (calls or []).append(("flash", port))),
        "serial_log": lambda port: log or FakeLog(),
        "camera": lambda settings: SimpleNamespace(open=lambda: None, close=lambda: None),
        "meter": lambda port: rig.meter,
        "recorder": lambda cam, meter, settings: rig.recorder,
        "sock": lambda: rig.sock,
        "key": lambda: KEY,
        "waiter": rig.waiter,
    }
    return f


def _argv(tmp_path, *extra):
    return ["--port", "/dev/ttyFAKE", "--stall-amps", "1.0", "--no-meter", "--out",
            str(tmp_path / "out"), *extra]


def test_passes_and_writes_report(tmp_path):
    rig = Rig(meter=False)
    assert main(_argv(tmp_path), _factories(rig)) == 0
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["passed"] is True


def test_passes_with_meter(tmp_path):
    rig = Rig()
    argv = ["--port", "p", "--stall-amps", "1.0", "--meter-port", "/dev/ttyUSB9",
            "--skip-flash", "--out", str(tmp_path / "o")]
    assert main(argv, _factories(rig)) == 0
    assert (tmp_path / "o" / "report.json").exists()


@pytest.mark.parametrize("argv", [
    ["--port", "p", "--no-meter"],
    ["--port", "p", "--no-meter", "--stall-amps", "0"],
    ["--port", "p", "--no-meter", "--stall-amps", "nan"],
    ["--port", "p", "--no-meter", "--stall-amps", "inf"],
    ["--port", "p", "--no-meter", "--stall-amps", "-1"],
    ["--port", "p", "--stall-amps", "1"],
    ["--port", "p", "--stall-amps", "1", "--no-meter", "--meter-port", "x"],
])
def test_bad_arguments_fail_parsing(argv):
    with pytest.raises(SystemExit) as e:
        main(argv, {})
    assert e.value.code == 2


def test_flash_failure_is_setup_error(tmp_path):
    rig = Rig(meter=False)

    def bad_flash(project, port):
        raise FlashError("upload failed")

    assert main(_argv(tmp_path), _factories(rig, flash=bad_flash)) == 2
    assert not (tmp_path / "out" / "report.json").exists()
    assert rig.sock.datagrams == []


def test_skip_flash_does_not_flash(tmp_path):
    rig = Rig(meter=False)
    calls = []
    assert main(_argv(tmp_path, "--skip-flash"), _factories(rig, calls=calls)) == 0
    assert calls == []


def test_ip_comes_from_serial_log_without_host(tmp_path, monkeypatch):
    monkeypatch.delenv("EYES_HOST", raising=False)
    rig = Rig(meter=False)
    seen = {}
    import bench.cli as cli

    real = cli.EyeClient

    def spy(host, **kw):
        seen["host"] = host
        return real(host, **kw)

    monkeypatch.setattr(cli, "EyeClient", spy)
    log = FakeLog("192.0.2.77")
    assert main(_argv(tmp_path), _factories(rig, log=log)) == 0
    assert seen["host"] == "192.0.2.77" and log.waited


def test_explicit_host_skips_serial_wait(tmp_path, monkeypatch):
    monkeypatch.delenv("EYES_HOST", raising=False)
    rig = Rig(meter=False)
    log = FakeLog()
    assert main(_argv(tmp_path, "--host", "head.invalid"), _factories(rig, log=log)) == 0
    assert not log.waited


def test_no_ip_is_setup_error(tmp_path, monkeypatch):
    monkeypatch.delenv("EYES_HOST", raising=False)
    rig = Rig(meter=False)
    assert main(_argv(tmp_path), _factories(rig, log=FakeLog(ip=None))) == 2


def test_failed_test_returns_1(tmp_path):
    rig = Rig(meter=False, ignore_look=True)
    assert main(_argv(tmp_path), _factories(rig)) == 1
    assert json.loads((tmp_path / "out" / "report.json").read_text())["passed"] is False


def test_overcurrent_aborts_with_1(tmp_path):
    rig = Rig(spike=5.0, spike_at=3.0)
    argv = ["--port", "p", "--stall-amps", "1.0", "--meter-port", "m", "--skip-flash",
            "--out", str(tmp_path / "o")]
    assert main(argv, _factories(rig)) == 1
    assert json.loads((tmp_path / "o" / "report.json").read_text())["aborted"] == "overcurrent"


def test_default_out_uses_clock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rig = Rig(meter=False)
    f = _factories(rig)
    f["clock"] = lambda: 0.0
    argv = ["--port", "p", "--stall-amps", "1", "--no-meter", "--skip-flash",
            "--limits", str(__import__("pathlib").Path(__file__).parents[1] / "config/limits.yaml")]
    assert main(argv, f) == 0
    assert (tmp_path / "bench/out/19700101T000000Z/report.json").exists()


# -- Extech adapter --------------------------------------------------------------------------


def _meter(function, unit, value, display="x"):
    r = Reading(t=0.0, function=function, unit=unit, value=value, display=display, raw="")
    return ExtechMeter(object(), reader=lambda ser: r)


@pytest.mark.parametrize("function,unit,value,amps", [
    ("DCA 200mA", "mA", 18.4, 0.0184),
    ("DCA 200µA", "µA", 150.0, 150e-6),
    ("DCA 20A", "A", 1.5, 1.5),
])
def test_extech_converts_to_amps(function, unit, value, amps):
    assert _meter(function, unit, value).read() == pytest.approx(amps)


@pytest.mark.parametrize("function,unit", [("DCV 20V", "V"), ("ACA 200mA", "mA"),
                                           ("OHM 2kΩ", "kΩ"), ("HOLD", "")])
def test_extech_wrong_mode_raises(function, unit):
    with pytest.raises(MeterError):
        _meter(function, unit, 1.0).read()


def test_extech_overload_raises():
    with pytest.raises(MeterError):
        _meter("DCA 200mA", "mA", None, "OL/special").read()


@pytest.mark.parametrize("exc", [TimeoutError("no reply"), FrameError("bad")])
def test_extech_read_errors_become_meter_errors(exc):
    def reader(ser):
        raise exc

    with pytest.raises(MeterError):
        ExtechMeter(object(), reader=reader).read()
