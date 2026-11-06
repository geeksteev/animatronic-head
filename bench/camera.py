"""Bench camera capture with fixed exposure, focus and white balance.

A Logitech C922 points at the eye mechanism. To compare frames between runs, every automatic
control (exposure, focus, white balance) is turned off and set to a fixed manual value.

``import bench.camera`` needs no OpenCV: ``cv2`` is imported only when an :class:`OpenCVCamera`
is opened without an injected ``capture_factory``. Install it with ``pip install -e '.[bench]'``.

:class:`FakeCamera` is a deterministic stand-in for tests that need frames but no hardware.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence, Union, runtime_checkable

import numpy as np

__all__ = [
    "Camera",
    "CameraError",
    "CameraSettings",
    "FakeCamera",
    "Frame",
    "OpenCVCamera",
    "fourcc_code",
]


class CameraError(RuntimeError):
    """The camera could not be opened, configured or read."""


@dataclass(frozen=True, eq=False)
class Frame:
    """One captured image. (``eq=False``: comparing numpy arrays with ``==`` is ambiguous.)

    ``index`` counts frames returned by ``read()`` (warmup frames are not counted), starting at 0.
    ``t`` is monotonic seconds from the camera's injected clock, taken right after the grab.
    ``image`` is a ``uint8`` array of shape (H, W, 3) in BGR order.
    """

    index: int
    t: float
    image: np.ndarray


@dataclass(frozen=True)
class CameraSettings:
    """Capture settings. Every field can be overridden.

    The defaults suit a Logitech C922 on the V4L2 backend (``v4l2-ctl -d /dev/video0 -L`` lists
    the ranges). They are starting points; tune them for the bench lighting and record the result.

    - ``device``: ``/dev/video0``.
    - ``width`` x ``height`` @ ``fps``: 640 x 480 @ 30. ``fourcc`` ``MJPG``, so the USB link
      is not the limit at 30 fps.
    - ``exposure``: 250. V4L2 ``exposure_time_absolute`` in units of 100 us (25 ms; C922 range
      3..2047). Keep it at or below 333, or the camera drops below 30 fps.
    - ``gain``: 0 (range 0..255). Add light rather than gain, so noise stays low.
    - ``focus``: 30 (range 0..250, step 5; 0 is infinity, larger values focus closer).
      Must be a multiple of 5 or the read-back will not match.
    - ``wb_temperature``: 4000 K (range 2000..6500, roughly neutral under white LED light).
    - ``warmup_frames``: 30 (one second at 30 fps). These frames are discarded after
      configuring, while the sensor settles on the new settings.
    """

    device: str = "/dev/video0"
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"
    exposure: int = 250
    gain: int = 0
    focus: int = 30
    wb_temperature: int = 4000
    warmup_frames: int = 30


@runtime_checkable
class Camera(Protocol):
    """Anything that produces :class:`Frame` objects. Usable as a context manager."""

    def open(self) -> None: ...

    def read(self) -> Frame: ...

    def close(self) -> None: ...

    def __enter__(self) -> "Camera": ...

    def __exit__(self, *exc: object) -> None: ...


class _ContextManagerMixin:
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def fourcc_code(fourcc: str) -> int:
    """Pack a four-character code the same way ``cv2.VideoWriter_fourcc`` does."""
    if len(fourcc) != 4:
        raise ValueError(f"fourcc must be 4 characters, got {fourcc!r}")
    return sum((ord(c) & 0xFF) << (8 * i) for i, c in enumerate(fourcc))


def _fourcc_str(code: float) -> str:
    n = int(code)
    return "".join(chr((n >> (8 * i)) & 0xFF) for i in range(4))


# OpenCV's numeric IDs (videoio enums, stable across 4.x). Used when cv2 is not importable,
# which only happens with an injected capture_factory (tests). With cv2, its own values win.
_CV_IDS = {
    "CAP_V4L2": 200,
    "CAP_PROP_FRAME_WIDTH": 3,
    "CAP_PROP_FRAME_HEIGHT": 4,
    "CAP_PROP_FPS": 5,
    "CAP_PROP_FOURCC": 6,
    "CAP_PROP_GAIN": 14,
    "CAP_PROP_EXPOSURE": 15,
    "CAP_PROP_AUTO_EXPOSURE": 21,
    "CAP_PROP_FOCUS": 28,
    "CAP_PROP_AUTOFOCUS": 39,
    "CAP_PROP_AUTO_WB": 44,
    "CAP_PROP_WB_TEMPERATURE": 45,
}

# On the V4L2 backend, CAP_PROP_AUTO_EXPOSURE = 1 selects manual exposure
# (V4L2_EXPOSURE_MANUAL); 3 is aperture priority (auto).
V4L2_MANUAL_EXPOSURE = 1

# V4L2 controls are integers; a read-back within this distance of the request counts as applied.
_READBACK_TOLERANCE = 0.5


def _import_cv2():
    try:
        import cv2  # noqa: PLC0415 - lazy on purpose, OpenCV is an optional dependency
    except ImportError:
        return None
    return cv2


class OpenCVCamera(_ContextManagerMixin):
    """C922 (or any V4L2 webcam) through OpenCV with every automatic control off.

    ``capture_factory(device, api_preference)`` returns an object with the
    ``cv2.VideoCapture`` interface (``isOpened``, ``set``, ``get``, ``read``, ``release``).
    It defaults to ``cv2.VideoCapture``. ``clock`` timestamps frames.

    If a property does not read back as requested, ``open()`` raises :class:`CameraError`
    listing every mismatch. With ``strict=False`` it prints a warning to stderr instead.
    """

    def __init__(
        self,
        settings: CameraSettings | None = None,
        capture_factory: Callable[[str, int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        *,
        strict: bool = True,
    ) -> None:
        self.settings = settings if settings is not None else CameraSettings()
        self._capture_factory = capture_factory
        self._clock = clock
        self.strict = strict
        self._cap = None
        self._index = 0
        self._applied: dict[str, Any] = {}

    def _ids(self) -> tuple[dict[str, int], Callable[[str, int], Any]]:
        cv2 = _import_cv2()
        factory = self._capture_factory
        if factory is None:
            if cv2 is None:
                raise CameraError(
                    "OpenCV is not installed; run: pip install -e '.[bench]'"
                )
            factory = cv2.VideoCapture
        ids = {name: getattr(cv2, name, default) if cv2 is not None else default
               for name, default in _CV_IDS.items()}
        return ids, factory

    def _requested(self, ids: dict[str, int]) -> list[tuple[str, int, float]]:
        """(name, prop id, value) in the order they must be set.

        Each auto control is switched off before its manual value is written; the driver
        ignores or rejects manual values while the auto mode is on.
        """
        s = self.settings
        return [
            ("fourcc", ids["CAP_PROP_FOURCC"], fourcc_code(s.fourcc)),
            ("width", ids["CAP_PROP_FRAME_WIDTH"], s.width),
            ("height", ids["CAP_PROP_FRAME_HEIGHT"], s.height),
            ("fps", ids["CAP_PROP_FPS"], s.fps),
            ("auto_exposure", ids["CAP_PROP_AUTO_EXPOSURE"], V4L2_MANUAL_EXPOSURE),
            ("exposure", ids["CAP_PROP_EXPOSURE"], s.exposure),
            ("gain", ids["CAP_PROP_GAIN"], s.gain),
            ("autofocus", ids["CAP_PROP_AUTOFOCUS"], 0),
            ("focus", ids["CAP_PROP_FOCUS"], s.focus),
            ("auto_wb", ids["CAP_PROP_AUTO_WB"], 0),
            ("wb_temperature", ids["CAP_PROP_WB_TEMPERATURE"], s.wb_temperature),
        ]

    def open(self) -> None:
        if self._cap is not None:
            return
        ids, factory = self._ids()
        cap = factory(self.settings.device, ids["CAP_V4L2"])
        try:
            if cap is None or not cap.isOpened():
                raise CameraError(f"could not open {self.settings.device} with V4L2")
            self._configure(cap, ids)
            for _ in range(self.settings.warmup_frames):
                self._grab(cap, "warmup")
        except BaseException:
            if cap is not None:
                cap.release()
            raise
        self._cap = cap
        self._index = 0

    def _configure(self, cap: Any, ids: dict[str, int]) -> None:
        requested = self._requested(ids)
        for _name, prop, value in requested:
            cap.set(prop, float(value))

        applied: dict[str, Any] = {}
        mismatches = []
        for name, prop, value in requested:
            got = cap.get(prop)
            if name == "fourcc":
                applied[name] = _fourcc_str(got)
                ok = int(got) == value
                shown = (repr(self.settings.fourcc), repr(applied[name]))
            else:
                applied[name] = got
                ok = got is not None and math.isfinite(got) and abs(got - value) <= _READBACK_TOLERANCE
                shown = (repr(value), repr(got))
            if not ok:
                mismatches.append(f"{name}: requested {shown[0]}, got {shown[1]}")
        self._applied = applied

        if mismatches:
            msg = (f"camera {self.settings.device} did not apply: " + "; ".join(mismatches))
            if self.strict:
                raise CameraError(msg)
            print(f"warning: {msg}", file=sys.stderr)

    def _grab(self, cap: Any, what: str) -> np.ndarray:
        ok, image = cap.read()
        if not ok or image is None:
            raise CameraError(f"{what} grab failed on {self.settings.device}")
        image = np.asarray(image)
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise CameraError(
                f"{what} grab returned {image.dtype} {image.shape}, expected uint8 HxWx3"
            )
        return image

    def read(self) -> Frame:
        if self._cap is None:
            raise CameraError("camera is not open")
        image = self._grab(self._cap, "frame")
        frame = Frame(index=self._index, t=self._clock(), image=image)
        self._index += 1
        return frame

    def close(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()

    def applied_settings(self) -> dict[str, Any]:
        """Values read back from the camera after configuring (empty before ``open()``)."""
        return dict(self._applied)


ImageSource = Union[Sequence[np.ndarray], Callable[[float], np.ndarray]]


class _StepClock:
    """Deterministic clock: advances 1/30 s per call, starting at 0."""

    def __init__(self, step: float = 1 / 30) -> None:
        self._step = step
        self._n = 0

    def __call__(self) -> float:
        t = self._n * self._step
        self._n += 1
        return t


class FakeCamera(_ContextManagerMixin):
    """Deterministic camera for tests.

    ``source`` is either a list of images, returned in order (``read()`` raises
    :class:`CameraError` when it runs out, like a failed grab), or a callable ``(t) -> image``
    called with the timestamp of each frame. Images must be ``uint8`` H x W x 3.
    ``clock`` defaults to a step clock that advances 1/30 s per frame, starting at 0.
    """

    def __init__(self, source: ImageSource, clock: Callable[[], float] | None = None) -> None:
        if not callable(source):
            source = list(source)
        self._source = source
        self._clock = clock if clock is not None else _StepClock()
        self._open = False
        self._index = 0

    def open(self) -> None:
        self._open = True

    def read(self) -> Frame:
        if not self._open:
            raise CameraError("camera is not open")
        t = self._clock()
        if callable(self._source):
            image = self._source(t)
        else:
            if self._index >= len(self._source):
                raise CameraError("fake camera source exhausted")
            image = self._source[self._index]
        image = np.array(image, copy=True)
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise CameraError(f"fake image is {image.dtype} {image.shape}, expected uint8 HxWx3")
        frame = Frame(index=self._index, t=t, image=image)
        self._index += 1
        return frame

    def close(self) -> None:
        self._open = False
