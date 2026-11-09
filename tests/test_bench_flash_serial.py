"""Tests for bench.flash and bench.serial_log. No PlatformIO, no pyserial, no hardware."""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bench.flash import FlashError, FlashResult, flash
from bench.serial_log import MAX_LINE_BYTES, SerialLog, SerialLogError

# ---------------------------------------------------------------------------------- flash


@pytest.fixture
def project(tmp_path: Path) -> Path:
    inc = tmp_path / "eyes" / "include"
    inc.mkdir(parents=True)
    (inc / "secrets.h.example").write_text("// example\n")
    (inc / "secrets.h").write_text("#define WIFI_PASSWORD \"do-not-log-me\"\n")
    return tmp_path / "eyes"


class FakeRunner:
    def __init__(self, returncode=0, stdout="", raise_exc=None):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.raise_exc = raise_exc

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout)


def ticking_clock(step=1.5):
    t = [100.0]

    def clock():
        t[0] += step
        return t[0]

    return clock


def test_flash_argv_without_port(project):
    runner = FakeRunner(stdout="Building\nSUCCESS\n")
    res = flash(project, runner=runner, clock=ticking_clock())
    assert len(runner.calls) == 1
    argv, kwargs = runner.calls[0]
    assert argv == ["pio", "run", "-d", str(project), "-t", "upload"]
    assert isinstance(argv, list)
    assert not kwargs.get("shell", False)
    assert kwargs["timeout"] == 180
    assert res == FlashResult(ok=True, returncode=0, duration_s=1.5, output_tail="Building\nSUCCESS")


def test_flash_argv_with_port(project):
    runner = FakeRunner()
    flash(project, port="/dev/ttyUSB0", runner=runner, timeout_s=30)
    argv, kwargs = runner.calls[0]
    assert argv == ["pio", "run", "-d", str(project), "-t", "upload", "--upload-port", "/dev/ttyUSB0"]
    assert not kwargs.get("shell", False)
    assert kwargs["timeout"] == 30


def test_flash_default_project_dir_argv(tmp_path, monkeypatch):
    inc = tmp_path / "firmware" / "eyes_esp32" / "include"
    inc.mkdir(parents=True)
    (inc / "secrets.h").write_text("")
    monkeypatch.chdir(tmp_path)
    runner = FakeRunner()
    flash(runner=runner)
    assert runner.calls[0][0] == ["pio", "run", "-d", str(Path("firmware/eyes_esp32")), "-t", "upload"]


def test_flash_missing_secrets_raises_before_runner(project):
    (project / "include" / "secrets.h").unlink()
    runner = FakeRunner()
    with pytest.raises(FlashError, match=r"secrets\.h\.example"):
        flash(project, runner=runner)
    assert runner.calls == []


def test_flash_never_reads_secrets(project, monkeypatch):
    secrets = project / "include" / "secrets.h"
    real_open = Path.open
    real_read = Path.read_text
    opened = []

    def spy_open(self, *a, **k):
        opened.append(self)
        return real_open(self, *a, **k)

    def spy_read(self, *a, **k):
        opened.append(self)
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read)
    res = flash(project, runner=FakeRunner(stdout="ok\n"))
    assert secrets not in opened
    assert "do-not-log-me" not in res.output_tail


def test_flash_nonzero_exit_raises_with_tail(project):
    out = "".join(f"line {i}\n" for i in range(80)) + "*** [upload] Error 2\n"
    runner = FakeRunner(returncode=1, stdout=out)
    with pytest.raises(FlashError) as ei:
        flash(project, runner=runner)
    err = ei.value
    assert err.result is not None
    assert err.result.ok is False and err.result.returncode == 1
    tail = err.result.output_tail.splitlines()
    assert len(tail) == 50
    assert tail[0] == "line 31" and tail[-1] == "*** [upload] Error 2"
    assert "Error 2" in str(err) and "line 0\n" not in str(err)


def test_flash_timeout_raises_with_tail(project):
    exc = subprocess.TimeoutExpired(["pio"], 5, output=b"Connecting....\xff___\n")
    runner = FakeRunner(raise_exc=exc)
    with pytest.raises(FlashError, match="timed out") as ei:
        flash(project, runner=runner, timeout_s=5)
    res = ei.value.result
    assert res.ok is False and res.returncode is None
    assert res.output_tail.startswith("Connecting....")
    assert "Connecting" in str(ei.value)


def test_flash_pio_missing(project):
    with pytest.raises(FlashError, match="PlatformIO"):
        flash(project, runner=FakeRunner(raise_exc=FileNotFoundError("pio")))


# ----------------------------------------------------------------------------- serial_log


class FakeSerial:
    """readline() hands out queued chunks, then b'' (like a pyserial timeout)."""

    def __init__(self, chunks=()):
        self._chunks = list(chunks)
        self._lock = threading.Lock()
        self.closed = False
        self.opened_with = None

    def feed(self, *chunks):
        with self._lock:
            self._chunks.extend(chunks)

    def readline(self):
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        time.sleep(0.002)
        return b""

    def close(self):
        self.closed = True


def make_log(chunks=(), **kw):
    fake = FakeSerial(chunks)

    def factory(port, baud):
        fake.opened_with = (port, baud)
        return fake

    return SerialLog("/dev/ttyUSB0", serial_factory=factory, **kw), fake


def counter_clock():
    n = [0.0]

    def clock():
        n[0] += 1.0
        return n[0]

    return clock


BOOT = [
    b"[eyes] boot\r\n",
    b"[eyes] servos at neutral\r\n",
    b"[wifi] connecting\r\n",
    b"[failsafe] active until first command\r\n",
    b"[wifi] connected, IP 192.0.2.10, UDP port 4210\r\n",
    b"[failsafe] EXIT: commands received\r\n",
    b"[failsafe] ENTER: no commands, servos to neutral\r\n",
    b"[wifi] disconnected, retrying\r\n",
    b"[wifi] connected, IP 192.0.2.23, UDP port 4210\r\n",
]


def test_parses_failsafe_and_ip():
    log, fake = make_log(BOOT, clock=counter_clock())
    with log:
        assert log.wait_for(r"192\.0\.2\.23", timeout_s=2) == (
            9.0,
            "[wifi] connected, IP 192.0.2.23, UDP port 4210",
        )
        assert fake.opened_with == ("/dev/ttyUSB0", 115200)
        assert log.failsafe_events() == [(6.0, "EXIT"), (7.0, "ENTER")]
        assert log.ip_address() == "192.0.2.23"
    assert fake.closed
    assert not log.running


def test_ip_none_before_report():
    log, _ = make_log([b"[eyes] boot\n"])
    with log:
        assert log.wait_for("boot", 2) is not None
        assert log.ip_address() is None
        assert log.failsafe_events() == []


def test_partial_lines_are_joined():
    log, _ = make_log([b"[failsafe] EN", b"", b"TER: no commands, servos to neutral\n[wifi] conn",
                       b"ected, IP 10.0.0.5, UDP port 4210\n"])
    with log:
        assert log.wait_for("UDP port", 2) is not None
        assert [e for _, e in log.failsafe_events()] == ["ENTER"]
        assert log.ip_address() == "10.0.0.5"


def test_wait_for_times_out_cleanly():
    log, _ = make_log([b"[eyes] boot\n"])
    with log:
        t0 = time.monotonic()
        assert log.wait_for(r"never", timeout_s=0.15) is None
        assert 0.1 <= time.monotonic() - t0 < 2.0
        assert log.running


def test_wait_for_sees_later_lines_and_since_t():
    log, fake = make_log([b"[failsafe] EXIT: commands received\n"], clock=counter_clock())
    with log:
        first = log.wait_for(r"\[failsafe\]", 2)
        assert first == (1.0, "[failsafe] EXIT: commands received")
        threading.Timer(0.05, fake.feed, args=(b"[failsafe] ENTER: no commands, servos to neutral\n",)).start()
        got = log.wait_for(r"\[failsafe\]", 2, since_t=first[0] + 0.5)
        assert got == (2.0, "[failsafe] ENTER: no commands, servos to neutral")


def test_invalid_utf8_is_replaced():
    log, _ = make_log([b"\xff\xfe garbage \xc3\n", b"[eyes] boot\n"])
    with log:
        assert log.wait_for("boot", 2) is not None
        line = log.lines()[0][1]
        assert "�" in line and "garbage" in line


def test_overlong_line_truncated():
    long = b"A" * (MAX_LINE_BYTES * 3)
    log, _ = make_log([long[:1500], long[1500:] + b"\n", b"[eyes] boot\n"])
    with log:
        assert log.wait_for("boot", 2) is not None
        lines = [l for _, l in log.lines()]
        assert lines == ["A" * MAX_LINE_BYTES, "[eyes] boot"]


def test_buffer_cap():
    chunks = [f"line {i}\n".encode() for i in range(25)]
    log, _ = make_log(chunks, max_lines=10)
    with log:
        assert log.wait_for("line 24", 2) is not None
        lines = [l for _, l in log.lines()]
        assert len(lines) == 10
        assert lines[0] == "line 15" and lines[-1] == "line 24"
        # evicted lines are no longer found
        assert log.wait_for(r"^line 3$", 0.05) is None


def test_ip_survives_buffer_eviction():
    chunks = [b"[wifi] connected, IP 192.0.2.10, UDP port 4210\n"] + [b"x\n"] * 5 + [b"end\n"]
    log, _ = make_log(chunks, max_lines=2)
    with log:
        assert log.wait_for("end", 2) is not None
        assert log.ip_address() == "192.0.2.10"


def test_read_error_stops_reader():
    class Broken(FakeSerial):
        def readline(self):
            raise OSError("device disconnected")

    log = SerialLog("/dev/ttyUSB0", serial_factory=lambda p, b: Broken())
    with log:
        assert log.wait_for("x", 2) is None
        assert isinstance(log.error, OSError)


def test_import_without_pyserial(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", None)  # makes `import serial` raise ImportError
    monkeypatch.delitem(sys.modules, "bench.serial_log", raising=False)
    mod = importlib.import_module("bench.serial_log")
    log = mod.SerialLog("/dev/ttyUSB0")
    with pytest.raises(mod.SerialLogError, match="pyserial"):
        log.start()
    assert SerialLogError  # original import still fine
