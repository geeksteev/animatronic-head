import importlib
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from bench.camera import (
    Camera,
    CameraError,
    CameraSettings,
    FakeCamera,
    Frame,
    OpenCVCamera,
    fourcc_code,
)

# OpenCV's videoio IDs, spelled out so the test does not depend on cv2.
CAP_V4L2 = 200
FRAME_WIDTH, FRAME_HEIGHT, FPS, FOURCC = 3, 4, 5, 6
GAIN, EXPOSURE, AUTO_EXPOSURE, FOCUS = 14, 15, 21, 28
AUTOFOCUS, AUTO_WB, WB_TEMPERATURE = 39, 44, 45


def img(value, h=4, w=6):
    return np.full((h, w, 3), value, dtype=np.uint8)


class FakeCapture:
    """cv2.VideoCapture stand-in: stores set() values, replays frames, records calls."""

    def __init__(self, frames=None, opened=True, override=None, fail_at=None):
        self.props = {}
        self.sets = []
        self.reads = 0
        self.released = False
        self.opened = opened
        self.override = override or {}   # prop -> value get() returns regardless of set()
        self.fail_at = fail_at           # read() number (0-based) that fails
        self.frames = frames if frames is not None else (lambda n: img(n % 256))

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.sets.append((prop, value))
        self.props[prop] = value
        return True

    def get(self, prop):
        return self.override.get(prop, self.props.get(prop, 0.0))

    def read(self):
        n = self.reads
        self.reads += 1
        if self.fail_at is not None and n >= self.fail_at:
            return False, None
        return True, self.frames(n)

    def release(self):
        self.released = True


class Factory:
    def __init__(self, cap):
        self.cap = cap
        self.calls = []

    def __call__(self, device, api):
        self.calls.append((device, api))
        return self.cap


class Clock:
    def __init__(self, start=100.0, step=0.5):
        self.t = start - step
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def make(settings=None, **cap_kwargs):
    cap = FakeCapture(**cap_kwargs)
    factory = Factory(cap)
    cam = OpenCVCamera(settings or CameraSettings(warmup_frames=3), factory, clock=Clock())
    return cam, cap, factory


# --- import without OpenCV ---------------------------------------------------------------

def test_import_without_cv2(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)   # makes `import cv2` raise ImportError
    monkeypatch.delitem(sys.modules, "bench.camera", raising=False)
    mod = importlib.import_module("bench.camera")
    with pytest.raises(ImportError):
        import cv2  # noqa: F401
    # An injected capture factory works without cv2 too.
    cam = mod.OpenCVCamera(mod.CameraSettings(warmup_frames=0), Factory(FakeCapture()))
    with cam:
        assert cam.read().image.shape == (4, 6, 3)


def test_import_in_fresh_interpreter_without_cv2():
    code = textwrap.dedent("""
        import sys
        sys.modules["cv2"] = None
        import bench.camera
        assert "cv2" not in [m for m, v in sys.modules.items() if v is not None]
    """)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_open_without_cv2_and_no_factory_is_a_camera_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    with pytest.raises(CameraError, match="OpenCV is not installed"):
        OpenCVCamera(CameraSettings()).open()


# --- OpenCVCamera ------------------------------------------------------------------------

def test_requests_v4l2_backend_on_configured_device():
    cam, _, factory = make(CameraSettings(device="/dev/video2", warmup_frames=0))
    cam.open()
    assert factory.calls == [("/dev/video2", CAP_V4L2)]


def test_sets_format_and_manual_controls():
    s = CameraSettings(width=800, height=600, fps=24, fourcc="YUYV", exposure=120, gain=17,
                       focus=45, wb_temperature=5000, warmup_frames=0)
    cam, cap, _ = make(s)
    cam.open()
    assert cap.props == {
        FOURCC: float(fourcc_code("YUYV")),
        FRAME_WIDTH: 800.0, FRAME_HEIGHT: 600.0, FPS: 24.0,
        AUTO_EXPOSURE: 1.0, EXPOSURE: 120.0, GAIN: 17.0,
        AUTOFOCUS: 0.0, FOCUS: 45.0,
        AUTO_WB: 0.0, WB_TEMPERATURE: 5000.0,
    }


def test_auto_controls_are_switched_off_before_manual_values():
    cam, cap, _ = make()
    cam.open()
    order = [p for p, _ in cap.sets]
    assert order.index(AUTO_EXPOSURE) < order.index(EXPOSURE)
    assert order.index(AUTOFOCUS) < order.index(FOCUS)
    assert order.index(AUTO_WB) < order.index(WB_TEMPERATURE)
    assert order.index(FOURCC) < order.index(FRAME_WIDTH)


def test_defaults_are_c922_manual_values():
    s = CameraSettings()
    assert (s.device, s.width, s.height, s.fps, s.fourcc) == ("/dev/video0", 640, 480, 30, "MJPG")
    assert s.warmup_frames == 30
    assert s.focus % 5 == 0          # C922 focus step
    assert 3 <= s.exposure <= 333    # keeps 30 fps
    assert 0 <= s.gain <= 255
    assert 2000 <= s.wb_temperature <= 6500


def test_settings_are_frozen():
    with pytest.raises(AttributeError):
        CameraSettings().exposure = 1


def test_warmup_frames_are_discarded():
    cam, cap, _ = make(CameraSettings(warmup_frames=5))
    cam.open()
    assert cap.reads == 5
    f = cam.read()
    assert cap.reads == 6
    assert f.index == 0
    assert int(f.image[0, 0, 0]) == 5   # frame 5 of the capture: 0..4 were thrown away


def test_frame_timestamps_come_from_injected_clock():
    cam, _, _ = make()
    with cam:
        frames = [cam.read() for _ in range(3)]
    assert [f.t for f in frames] == [100.0, 100.5, 101.0]
    assert [f.index for f in frames] == [0, 1, 2]
    assert all(isinstance(f, Frame) and f.image.dtype == np.uint8 for f in frames)


def test_readback_mismatch_raises_and_lists_every_property():
    cam, cap, _ = make(override={EXPOSURE: 156.0, AUTO_WB: 1.0})
    with pytest.raises(CameraError) as e:
        cam.open()
    msg = str(e.value)
    assert "exposure: requested 250, got 156.0" in msg
    assert "auto_wb" in msg
    assert "focus" not in msg.replace("autofocus", "")
    assert cap.released
    assert cap.reads == 0   # no warmup on a failed configure


def test_fourcc_mismatch_is_reported_as_text():
    cam, _, _ = make(override={FOURCC: float(fourcc_code("YUYV"))})
    with pytest.raises(CameraError, match="fourcc: requested 'MJPG', got 'YUYV'"):
        cam.open()


def test_non_strict_warns_on_stderr_and_continues(capsys):
    cap = FakeCapture(override={FOCUS: 35.0})
    cam = OpenCVCamera(CameraSettings(warmup_frames=1), Factory(cap), clock=Clock(), strict=False)
    with cam:
        cam.read()
        assert cam.applied_settings()["focus"] == 35.0
    err = capsys.readouterr().err
    assert "warning" in err and "focus: requested 30, got 35.0" in err


def test_applied_settings_reports_readback():
    cam, _, _ = make()
    assert cam.applied_settings() == {}
    cam.open()
    applied = cam.applied_settings()
    assert applied["fourcc"] == "MJPG"
    assert applied["exposure"] == 250.0
    assert applied["auto_exposure"] == 1.0
    assert applied["autofocus"] == 0.0
    assert applied["wb_temperature"] == 4000.0
    applied["exposure"] = -1
    assert cam.applied_settings()["exposure"] == 250.0   # returns a copy


def test_failed_grab_raises():
    cam, _, _ = make(CameraSettings(warmup_frames=2), fail_at=3)
    cam.open()
    cam.read()
    with pytest.raises(CameraError, match="grab failed"):
        cam.read()


def test_failed_warmup_grab_raises_and_releases():
    cam, cap, _ = make(CameraSettings(warmup_frames=4), fail_at=2)
    with pytest.raises(CameraError, match="warmup grab failed"):
        cam.open()
    assert cap.released


def test_unopened_device_raises():
    cam, cap, _ = make(opened=False)
    with pytest.raises(CameraError, match="could not open"):
        cam.open()
    assert cap.released


def test_read_before_open_raises():
    cam, _, _ = make()
    with pytest.raises(CameraError, match="not open"):
        cam.read()


def test_wrong_image_shape_raises():
    cam, _, _ = make(CameraSettings(warmup_frames=0), frames=lambda n: np.zeros((4, 6), np.uint8))
    cam.open()
    with pytest.raises(CameraError, match="expected uint8 HxWx3"):
        cam.read()


def test_context_manager_releases():
    cam, cap, _ = make()
    with cam as c:
        assert c is cam
    assert cap.released
    with pytest.raises(CameraError):
        cam.read()


def test_satisfies_camera_protocol():
    cam, _, _ = make()
    assert isinstance(cam, Camera)
    assert isinstance(FakeCamera([img(0)]), Camera)


def test_fourcc_code_matches_opencv_packing():
    assert fourcc_code("MJPG") == 0x47504A4D
    with pytest.raises(ValueError):
        fourcc_code("MJP")


# --- FakeCamera --------------------------------------------------------------------------

def test_fake_camera_list_source():
    images = [img(1), img(2), img(3)]
    with FakeCamera(images, clock=Clock(start=5.0, step=1.0)) as cam:
        frames = [cam.read() for _ in range(3)]
        with pytest.raises(CameraError, match="exhausted"):
            cam.read()
    assert [int(f.image[0, 0, 0]) for f in frames] == [1, 2, 3]
    assert [f.t for f in frames] == [5.0, 6.0, 7.0]
    assert [f.index for f in frames] == [0, 1, 2]
    frames[0].image[:] = 99
    assert int(images[0][0, 0, 0]) == 1   # frames do not alias the source


def test_fake_camera_callable_source_gets_timestamp():
    seen = []

    def source(t):
        seen.append(t)
        return img(int(t * 10))

    with FakeCamera(source, clock=Clock(start=1.0, step=0.5)) as cam:
        frames = [cam.read() for _ in range(3)]
    assert seen == [1.0, 1.5, 2.0]
    assert [int(f.image[0, 0, 0]) for f in frames] == [10, 15, 20]


def test_fake_camera_is_deterministic_with_default_clock():
    def run():
        with FakeCamera(lambda t: img(int(t * 30))) as cam:
            return [(f.index, f.t, int(f.image[0, 0, 0])) for f in (cam.read() for _ in range(4))]
    assert run() == run() == [(0, 0.0, 0), (1, 1 / 30, 1), (2, 2 / 30, 2), (3, 3 / 30, 3)]


def test_fake_camera_requires_open_and_valid_images():
    cam = FakeCamera([img(0)])
    with pytest.raises(CameraError, match="not open"):
        cam.read()
    with FakeCamera([np.zeros((2, 2, 3), dtype=np.float32)]) as bad:
        with pytest.raises(CameraError, match="expected uint8"):
            bad.read()
