"""Threaded recorder: grabs camera frames and meter samples on one clock.

Implements the recorder interface documented in :mod:`bench.motion_test`. An error in a reader
thread (for example a ``MeterError``) is stored in ``error``; ``check()`` raises it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

__all__ = ["Recorder"]

MAX_FRAMES = 600  # about 20 s at 30 fps
MAX_SAMPLES = 20000


class Recorder:
    def __init__(self, camera: Any, meter: Any = None, camera_settings: Any = None,
                 clock: Callable[[], float] = time.monotonic,
                 sample_period_s: float = 0.05) -> None:
        self._camera = camera
        self._meter = meter
        self.camera_settings = camera_settings
        self.clock = clock
        self._period = sample_period_s
        self._frames: list = []
        self._samples: list[tuple[float, float]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.on_sample: Optional[Callable[[float, float], None]] = None
        self.error: Optional[BaseException] = None

    @property
    def frames(self) -> list:
        with self._lock:
            return list(self._frames)

    @property
    def samples(self) -> list[tuple[float, float]]:
        with self._lock:
            return list(self._samples)

    @property
    def has_meter(self) -> bool:
        return self._meter is not None

    def _grab_frame(self) -> None:
        frame = self._camera.read()
        with self._lock:
            self._frames.append(frame)
            del self._frames[:-MAX_FRAMES]

    def _grab_sample(self) -> None:
        amps = self._meter.read()
        t = self.clock()
        with self._lock:
            self._samples.append((t, amps))
            del self._samples[:-MAX_SAMPLES]
        cb = self.on_sample
        if cb is not None:
            cb(t, amps)

    def poll_once(self) -> None:
        self._grab_frame()
        if self._meter is not None:
            self._grab_sample()

    def _loop(self, fn: Callable[[], None], period: float) -> None:
        try:
            while not self._stop.is_set():
                start = time.monotonic()
                fn()
                self._stop.wait(max(0.0, period - (time.monotonic() - start)))
        except BaseException as exc:
            self.error = exc

    def start(self) -> "Recorder":
        self._threads = [threading.Thread(target=self._loop, args=(self._grab_frame, 0.0),
                                          name="recorder-camera", daemon=True)]
        if self._meter is not None:
            self._threads.append(threading.Thread(target=self._loop,
                                                  args=(self._grab_sample, self._period),
                                                  name="recorder-meter", daemon=True))
        for t in self._threads:
            t.start()
        return self

    def check(self) -> None:
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
