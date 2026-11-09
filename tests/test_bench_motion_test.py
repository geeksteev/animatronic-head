"""Tests for bench.motion_test, bench.report and bench.png against a simulated rig."""

import json
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from bench.motion_test import STEPS, run_motion_test
from bench.png import PNG_SIGNATURE, encode_png
from brain.eye_protocol import Center
from tests.bench_fakes import Rig

STEP_NAMES = [n for n, _ in STEPS]
LOOK_STEPS = ["look_left", "look_right", "look_up", "look_down", "failsafe_prep"]
TOP_KEYS = {"schema_version", "started_at", "camera_settings", "command_timeout_ms",
            "stall_amps", "noise_threshold", "steps", "passed", "aborted"}
STEP_KEYS = {"name", "command", "t_sent", "status", "checks", "latency_s", "settle_time_s",
             "max_amps", "key_frames"}


def _by_name(report):
    return {s["name"]: s for s in report["steps"]}


def _png_dims(data: bytes):
    assert data.startswith(PNG_SIGNATURE)
    pos, idat, ihdr = len(PNG_SIGNATURE), b"", None
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n : pos + 12 + n])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    return ihdr, zlib.decompress(idat)


def test_healthy_rig_passes_every_step(tmp_path):
    rig = Rig()
    report = run_motion_test(**rig.kwargs(tmp_path))
    steps = _by_name(report)
    assert list(steps) == STEP_NAMES
    for s in report["steps"]:
        assert s["status"] == "passed", s
        assert False not in s["checks"].values(), s
    assert report["passed"] is True and report["aborted"] is None
    for name in LOOK_STEPS + ["blink", "center", "failsafe"]:
        assert steps[name]["checks"]["moved"] is True
        assert steps[name]["checks"]["settled"] is True
        assert steps[name]["checks"]["current_ok"] is True
    assert steps["blink"]["checks"]["returned_to_reference"] is True
    assert steps["failsafe"]["checks"]["returned_to_neutral"] is True
    assert steps["look_left"]["latency_s"] is not None
    assert steps["failsafe"]["max_amps"] == pytest.approx(0.1)
    # Only look/blink/center (+ heartbeat pings) are sent, and sending really stops at the end.
    kinds = {type(c).__name__ for c in rig.sock.commands}
    assert kinds <= {"Look", "Blink", "Center", "Ping"}
    assert rig.sock.closed


def test_rig_ignoring_look_fails_moved(tmp_path):
    rig = Rig(ignore_look=True)
    report = run_motion_test(**rig.kwargs(tmp_path))
    steps = _by_name(report)
    for name in LOOK_STEPS:
        assert steps[name]["checks"]["moved"] is False
        assert steps[name]["status"] == "failed"
    assert steps["blink"]["status"] == "passed"
    assert report["passed"] is False


def test_rig_without_failsafe_fails_returned_to_neutral(tmp_path):
    rig = Rig(failsafe=False)
    report = run_motion_test(**rig.kwargs(tmp_path))
    step = _by_name(report)["failsafe"]
    assert step["checks"]["returned_to_neutral"] is False
    assert step["status"] == "failed"
    assert report["passed"] is False


def test_failsafe_too_early_is_rejected(tmp_path):
    rig = Rig()
    rig.camera.timeout_s = 0.1  # returns to neutral well before 0.8 * command_timeout_ms
    report = run_motion_test(**rig.kwargs(tmp_path))
    assert _by_name(report)["failsafe"]["checks"]["returned_to_neutral"] is False


def test_serial_failsafe_event_is_evidence(tmp_path):
    from tests.bench_fakes import FakeSerialLog

    rig = Rig()
    rig.serial_log = FakeSerialLog([(1.0, "ENTER"), (2.0, "EXIT")])  # before the failsafe step
    report = run_motion_test(**rig.kwargs(tmp_path))
    assert _by_name(report)["failsafe"]["evidence"] == {"serial_failsafe_enter": False}


def test_no_meter_gives_null_current_ok(tmp_path):
    rig = Rig(meter=False)
    report = run_motion_test(**rig.kwargs(tmp_path))
    for s in report["steps"]:
        assert s["checks"]["current_ok"] is None
        assert s["max_amps"] is None
    assert report["passed"] is True


def test_overcurrent_aborts_with_center_and_skips_rest(tmp_path):
    rig = Rig(spike=3.0, spike_at=8.0)  # during the look steps
    report = run_motion_test(**rig.kwargs(tmp_path, stall_amps=1.0))
    assert report["aborted"] == "overcurrent" and report["passed"] is False
    assert isinstance(rig.sock.commands[-1], Center)
    assert rig.sock.closed
    statuses = [s["status"] for s in report["steps"]]
    assert list(_by_name(report)) == STEP_NAMES
    assert statuses.count("failed") == 1
    first_bad = statuses.index("failed")
    assert all(s == "skipped" for s in statuses[first_bad + 1 :])
    assert statuses[first_bad + 1 :], "some steps must remain skipped"
    assert report["steps"][first_bad]["checks"]["current_ok"] is False
    assert (tmp_path / "report.json").is_file()


def test_exception_centers_closes_writes_report_and_reraises(tmp_path):
    rig = Rig()
    rig.recorder.fail_at_poll = 100  # mid-sequence
    with pytest.raises(RuntimeError, match="unplugged"):
        run_motion_test(**rig.kwargs(tmp_path))
    assert isinstance(rig.sock.commands[-1], Center)
    assert rig.sock.closed
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["aborted"] == "exception"
    assert "skipped" in {s["status"] for s in report["steps"]}


def test_keyboard_interrupt_centers_and_reraises(tmp_path):
    rig = Rig()
    calls = []

    def waiter(seconds):
        calls.append(seconds)
        if len(calls) > 20:
            raise KeyboardInterrupt
        rig.waiter(seconds)

    kwargs = rig.kwargs(tmp_path)
    kwargs["waiter"] = waiter
    with pytest.raises(KeyboardInterrupt):
        run_motion_test(**kwargs)
    assert isinstance(rig.sock.commands[-1], Center)
    assert json.loads((tmp_path / "report.json").read_text())["aborted"] == "interrupted"


def test_report_schema_and_pngs(tmp_path):
    rig = Rig()
    run_motion_test(**rig.kwargs(tmp_path))
    text = (tmp_path / "report.json").read_text()
    report = json.loads(text)
    assert set(report) == TOP_KEYS
    assert report["schema_version"] == 1
    assert report["started_at"].endswith("+00:00")
    assert report["command_timeout_ms"] == rig.limits.command_timeout_ms
    assert report["stall_amps"] == 1.0
    assert report["noise_threshold"] > 0
    assert report["camera_settings"] == {"exposure": -6, "focus": 0}
    assert "head.invalid" not in text
    for s in report["steps"]:
        assert STEP_KEYS <= set(s)
        assert s["status"] in {"passed", "failed", "skipped"}
        for rel in s["key_frames"].values():
            assert not Path(rel).is_absolute()
            data = (tmp_path / rel).read_bytes()
            assert data.startswith(PNG_SIGNATURE)
            (w, h, depth, color, *_), raw = _png_dims(data)
            assert (w, h, depth, color) == (80, 60, 8, 2)
            assert len(raw) == h * (1 + w * 3)
    kf = _by_name(report)["look_left"]["key_frames"]
    assert set(kf) == {"before", "peak", "settled"}
    assert kf["before"] == "frames/look_left_before.png"


@pytest.mark.parametrize("shape,color,channels", [((5, 7), 0, 1), ((5, 7, 3), 2, 3)])
def test_png_encoder(shape, color, channels):
    img = np.arange(np.prod(shape), dtype=np.uint8).reshape(shape)
    (w, h, depth, ctype, *_), raw = _png_dims(encode_png(img))
    assert (w, h, depth, ctype) == (7, 5, 8, color)
    assert len(raw) == 5 * (1 + 7 * channels)
    assert raw[0] == 0
    if channels == 3:  # BGR is stored as RGB
        assert list(raw[1:4]) == [2, 1, 0]


def test_png_encoder_rejects_bad_input():
    with pytest.raises(ValueError):
        encode_png(np.zeros((4, 4), dtype=np.float64))
    with pytest.raises(ValueError):
        encode_png(np.zeros((4, 4, 4), dtype=np.uint8))
