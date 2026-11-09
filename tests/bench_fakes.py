"""A simulated bench rig for tests: no hardware, no real sleeping.

``Rig`` ties together a real ``EyeClient`` (fake socket recording decoded datagrams), a camera
that draws the eye, a current meter, a recorder and a waiter, all on one fake clock.
The head simulated here moves on ``look``/``center``/``blink`` datagrams and, like the real
firmware, returns to neutral when no datagram arrives for ``command_timeout_ms``.
"""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np

from bench.camera import Frame
from brain import eye_protocol
from brain.eye_client import EyeClient
from brain.eye_protocol import Blink, Center, Look
from brain.limits import load_limits

KEY = bytes(range(1, 33))
FRAME_PERIOD = 1 / 30
HEIGHT, WIDTH = 60, 80
EYE = 12  # eye rectangle side, pixels
RANGE_X, RANGE_Y = 25, 15  # pixels of eye travel for a full-scale look
BACKGROUND, EYE_LEVEL, LID_LEVEL = 20, 220, 120


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeSocket:
    """Records every datagram (decoded) with the fake time it was sent."""

    def __init__(self, clock: FakeClock, on_datagram: Callable) -> None:
        self._clock = clock
        self._on_datagram = on_datagram
        self.datagrams: list[tuple[float, int, object]] = []  # (t, seq, command)
        self.closed = False

    def sendto(self, data: bytes, addr) -> int:
        seq, cmd = eye_protocol.decode_signed(data, KEY)
        self.datagrams.append((self._clock(), seq, cmd))
        self._on_datagram(self._clock(), cmd)
        return len(data)

    def close(self) -> None:
        self.closed = True

    @property
    def commands(self) -> list:
        return [c for _, _, c in self.datagrams]


class FakeCamera:
    """Draws an eye rectangle at the last decoded ``look`` and lid bars while a blink is active.

    ``ignore_look`` simulates a head that does not move; ``failsafe`` enables the head's
    return to neutral after ``timeout_s`` without datagrams.
    """

    def __init__(self, clock: FakeClock, timeout_s: float, failsafe: bool = True,
                 ignore_look: bool = False) -> None:
        self.clock = clock
        self.timeout_s = timeout_s
        self.failsafe = failsafe
        self.ignore_look = ignore_look
        self.pos = (0.0, 0.0)
        self.blink_until = -1.0
        self.last_datagram_t: Optional[float] = None
        self.index = 0

    def on_datagram(self, t: float, cmd) -> None:
        self.last_datagram_t = t
        if isinstance(cmd, Look) and not self.ignore_look:
            self.pos = (cmd.x, cmd.y)
        elif isinstance(cmd, Center):
            self.pos = (0.0, 0.0)
        elif isinstance(cmd, Blink):
            self.blink_until = t + cmd.ms / 1000.0

    def read(self) -> Frame:
        t = self.clock()
        if (
            self.failsafe
            and self.last_datagram_t is not None
            and t - self.last_datagram_t >= self.timeout_s
        ):
            self.pos = (0.0, 0.0)
            self.blink_until = -1.0
        img = np.full((HEIGHT, WIDTH, 3), BACKGROUND, dtype=np.uint8)
        cx = int(round(WIDTH / 2 + self.pos[0] * RANGE_X))
        cy = int(round(HEIGHT / 2 - self.pos[1] * RANGE_Y))
        img[cy - EYE // 2 : cy + EYE // 2, cx - EYE // 2 : cx + EYE // 2] = EYE_LEVEL
        if t < self.blink_until:
            img[:15] = LID_LEVEL
            img[-15:] = LID_LEVEL
        frame = Frame(index=self.index, t=t, image=img)
        self.index += 1
        return frame


class FakeMeter:
    """Current in amps: ``base``, or ``spike`` from fake time ``spike_at`` on."""

    def __init__(self, clock: FakeClock, base: float = 0.1, spike: float = 0.0,
                 spike_at: Optional[float] = None) -> None:
        self.clock = clock
        self.base = base
        self.spike = spike
        self.spike_at = spike_at

    def read(self) -> float:
        if self.spike_at is not None and self.clock() >= self.spike_at:
            return self.spike
        return self.base


class FakeRecorder:
    """Implements the recorder interface used by ``bench.motion_test``."""

    def __init__(self, clock: FakeClock, camera: FakeCamera, meter: Optional[FakeMeter]) -> None:
        self.clock = clock
        self._camera = camera
        self._meter = meter
        self._frames: list[Frame] = []
        self._samples: list[tuple[float, float]] = []
        self.on_sample: Optional[Callable[[float, float], None]] = None
        self.camera_settings = {"exposure": -6, "focus": 0}
        self.fail_at_poll: Optional[int] = None  # raise RuntimeError on this poll (0-based)
        self.polls = 0

    @property
    def frames(self) -> list[Frame]:
        return list(self._frames)

    @property
    def samples(self) -> list[tuple[float, float]]:
        return list(self._samples)

    @property
    def has_meter(self) -> bool:
        return self._meter is not None

    def poll_once(self) -> None:
        if self.fail_at_poll is not None and self.polls == self.fail_at_poll:
            raise RuntimeError("camera unplugged")
        self.polls += 1
        self._frames.append(self._camera.read())
        if self._meter is not None:
            t, amps = self.clock(), self._meter.read()
            self._samples.append((t, amps))
            if self.on_sample is not None:
                self.on_sample(t, amps)


class FakeSerialLog:
    def __init__(self, events=()) -> None:
        self._events = list(events)

    def failsafe_events(self) -> list[tuple[float, str]]:
        return list(self._events)


class Rig:
    """A complete simulated rig. Build it, then call ``run_motion_test(**rig.kwargs(...))``."""

    def __init__(self, failsafe: bool = True, ignore_look: bool = False, meter: bool = True,
                 spike: float = 0.0, spike_at: Optional[float] = None) -> None:
        self.limits = load_limits()
        self.timeout_s = self.limits.command_timeout_ms / 1000.0
        self.clock = FakeClock()
        self.camera = FakeCamera(self.clock, self.timeout_s, failsafe, ignore_look)
        self.sock = FakeSocket(self.clock, self.camera.on_datagram)
        self.client = EyeClient("head.invalid", sock=self.sock, key=KEY, limits=self.limits)
        self.meter = FakeMeter(self.clock, spike=spike, spike_at=spike_at) if meter else None
        self.recorder = FakeRecorder(self.clock, self.camera, self.meter)
        self.serial_log: Optional[FakeSerialLog] = None

    def waiter(self, seconds: float) -> None:
        """Advance the fake clock one frame period at a time, polling the recorder each tick."""
        remaining = seconds
        while remaining > 1e-9:
            dt = min(FRAME_PERIOD, remaining)
            self.clock.advance(dt)
            remaining -= dt
            # Stand-in for the client's heartbeat thread, which runs on real time.
            last = self.sock.datagrams[-1][0] if self.sock.datagrams else 0.0
            if not self.sock.closed and self.clock() - last >= self.client.heartbeat_interval:
                self.client.ping()
            self.recorder.poll_once()

    def kwargs(self, out_dir, stall_amps: float = 1.0) -> dict:
        return dict(
            client=self.client,
            recorder=self.recorder,
            limits=self.limits,
            stall_amps=stall_amps,
            out_dir=out_dir,
            serial_log=self.serial_log,
            waiter=self.waiter,
        )
