"""Bench motion test: drive the head through a fixed sequence and judge it from camera + current.

:func:`run_motion_test` sends ``center``, ``look`` and ``blink`` through a
:class:`brain.eye_client.EyeClient` (the head clamps them to its limits), watches the result with
the bench camera and the current meter, and writes ``<out_dir>/report.json`` plus key frames.

The runner never calls ``time.sleep``. Every wait goes through ``waiter(seconds)``; the default
sleeps while the recorder threads run. Tests pass a waiter that advances a fake clock and calls
``recorder.poll_once()`` once per frame period.

Recorder interface used here (duck typed; see ``tests/bench_fakes.py`` for a fake):

- ``frames``: snapshot ``list[bench.camera.Frame]``, oldest first.
- ``samples``: snapshot ``list[(t, amps)]``; ``has_meter``: whether a meter is attached.
- ``on_sample``: settable callable ``(t, amps)`` invoked for every current sample, possibly from
  a recorder thread.
- ``clock()``: the time base shared by frames and samples (seconds).
- ``poll_once()``: capture one frame and one sample. Optional ``camera_settings`` (dict or
  dataclass) is copied into the report.

Safety: a sample above ``stall_amps``, any exception or a KeyboardInterrupt sends ``center`` and
closes the client; the remaining steps are marked ``skipped`` and the report is still written.
At the final step ``client.close()`` stops all sending, so the head must reach neutral by itself.
The head's host/IP is never written to the report.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from bench.motion import NoiseFloor, analyze, matches_reference
from bench.report import SCHEMA_VERSION, write_report

__all__ = ["run_motion_test"]

SETTLE_WINDOW_S = 0.5  # still time that counts as settled; longer than the 300 ms blink
BLINK_MS = 300
FAILSAFE_MIN_FRACTION = 0.8  # motion may start no earlier than this x command_timeout_ms
NOISE_WINDOW_S = 0.5
WAIT_CHUNK_S = 0.1  # waits are split so an overcurrent abort is noticed promptly
LOOK_PREP = (0.6, 0.6)

# (name, command description) in run order.
STEPS: list[tuple[str, dict]] = [
    ("baseline", {"cmd": "center"}),
    ("look_left", {"cmd": "look", "x": -1.0, "y": 0.0}),
    ("look_right", {"cmd": "look", "x": 1.0, "y": 0.0}),
    ("look_up", {"cmd": "look", "x": 0.0, "y": 1.0}),
    ("look_down", {"cmd": "look", "x": 0.0, "y": -1.0}),
    ("blink", {"cmd": "blink", "ms": BLINK_MS}),
    ("center", {"cmd": "center"}),
    ("failsafe_prep", {"cmd": "look", "x": LOOK_PREP[0], "y": LOOK_PREP[1]}),
    ("failsafe", {"cmd": "close"}),
]


def _sleep_waiter(seconds: float) -> None:
    time.sleep(seconds)


def _skipped(name: str, command: dict) -> dict:
    return {
        "name": name,
        "command": command,
        "t_sent": None,
        "status": "skipped",
        "checks": {},
        "latency_s": None,
        "settle_time_s": None,
        "max_amps": None,
        "key_frames": {},
    }


class _Run:
    def __init__(self, client, recorder, limits, stall_amps, out_dir, serial_log, waiter,
                 settle_timeout_s, failsafe_margin_s):
        timeout_ms = limits.command_timeout_ms
        if timeout_ms is None or timeout_ms <= 0:
            raise ValueError("limits need a positive failsafe.command_timeout_ms")
        self.client = client
        self.rec = recorder
        self.stall_amps = float(stall_amps)
        self.out_dir = Path(out_dir)
        self.serial_log = serial_log
        self.wait = waiter if waiter is not None else _sleep_waiter
        self.settle_timeout_s = float(settle_timeout_s)
        self.margin_s = float(failsafe_margin_s)
        self.timeout_ms = timeout_ms
        self.timeout_s = timeout_ms / 1000.0

        self.lock = threading.Lock()
        self.stopped = False  # emergency stop done: nothing more may be sent
        self.aborted: Optional[str] = None
        self.noise: Optional[NoiseFloor] = None
        self.neutral = None  # neutral reference Frame
        self.images: dict[str, Any] = {}
        self.steps: list[dict] = []
        self.t0 = 0.0
        self.last_t_sent = 0.0

    # -- safety -----------------------------------------------------------

    def emergency_stop(self, reason: str) -> None:
        """Send ``center`` then close the client. Idempotent; never raises."""
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            if self.aborted is None:
                self.aborted = reason
            try:
                self.client.center()
            except Exception:
                pass
            try:
                self.client.close()
            except Exception:
                pass

    def on_sample(self, t: float, amps: float) -> None:
        if amps > self.stall_amps:
            self.emergency_stop("overcurrent")

    def send(self, action: Callable[[], Any]) -> bool:
        """Run ``action`` unless an emergency stop has happened. Returns whether it ran."""
        with self.lock:
            if self.stopped:
                return False
            action()
            return True

    # -- helpers ----------------------------------------------------------

    def wait_for(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 1e-9 and not self.stopped:
            chunk = min(WAIT_CHUNK_S, remaining)
            self.wait(chunk)
            remaining -= chunk

    def window_frames(self, t_sent: float) -> list:
        frames = list(self.rec.frames)
        first = next((i for i, f in enumerate(frames) if f.t >= t_sent), len(frames))
        return frames[max(0, first - 1) :]

    def max_amps(self, t_from: float, t_to: float) -> Optional[float]:
        if not self.rec.has_meter:
            return None
        vals = [a for t, a in self.rec.samples if t_from <= t <= t_to]
        return max(vals) if vals else 0.0

    def threshold(self) -> float:
        return self.noise.threshold if self.noise is not None else 1.0

    def run_step(self, name: str, command: dict, action: Callable[[], Any], duration_s: float,
                 settle_deadline_s: float, extra: Optional[Callable] = None) -> dict:
        frames_now = list(self.rec.frames)
        before = frames_now[-1] if frames_now else None
        t_sent = self.last_t_sent = self.rec.clock()
        ran = self.send(action)
        self.wait_for(duration_s)
        t_end = self.rec.clock()

        max_amps = self.max_amps(t_sent, t_end)
        checks: dict[str, Optional[bool]] = {"moved": None, "settled": None}
        latency = settle_time = None
        key: dict[str, str] = {}
        if self.aborted == "overcurrent" or not ran:
            checks["current_ok"] = False if max_amps is not None else None
        else:
            frames = self.window_frames(t_sent)
            res = analyze(frames, t_sent, self.threshold(), settle_window_s=SETTLE_WINDOW_S,
                          timeout_s=settle_deadline_s)
            checks["moved"] = res.moved
            checks["settled"] = res.settled
            checks["current_ok"] = None if max_amps is None else max_amps <= self.stall_amps
            latency, settle_time = res.latency_s, res.settle_time_s
            if extra is not None:
                checks.update(extra(res, frames, before))
            last = frames[-1] if frames else None
            by_t = {f.t: f for f in frames}
            picks = {
                "before": before,
                "peak": by_t.get(res.peak_t) if res.moved else None,
                "settled": by_t.get(res.settled_t) if res.settled else last,
            }
            for label, frame in picks.items():
                if frame is not None:
                    rel = f"frames/{name}_{label}.png"
                    self.images[rel] = frame.image
                    key[label] = rel
        step = {
            "name": name,
            "command": command,
            "t_sent": t_sent - self.t0,
            "status": "passed" if all(v is not False for v in checks.values()) else "failed",
            "checks": checks,
            "latency_s": latency,
            "settle_time_s": settle_time,
            "max_amps": max_amps,
            "key_frames": key,
        }
        if self.aborted == "overcurrent":
            step["status"] = "failed"
        return step

    # -- sequence ---------------------------------------------------------

    def baseline(self, command: dict) -> dict:
        t_sent = self.rec.clock()
        self.send(self.client.center)
        self.wait_for(self.settle_timeout_s + NOISE_WINDOW_S)
        t_end = self.rec.clock()
        max_amps = self.max_amps(t_sent, t_end)
        checks: dict[str, Optional[bool]] = {"moved": None, "settled": None}
        key: dict[str, str] = {}
        if self.aborted is None:
            still = [f for f in self.rec.frames if f.t >= t_end - NOISE_WINDOW_S]
            if len(still) < 2:
                raise RuntimeError("baseline: fewer than two frames to measure the noise floor")
            self.noise = NoiseFloor.from_frames(still)
            self.neutral = still[-1]
            self.images["frames/baseline_settled.png"] = self.neutral.image
            key["settled"] = "frames/baseline_settled.png"
        checks["current_ok"] = None if max_amps is None else max_amps <= self.stall_amps
        return {
            "name": "baseline",
            "command": command,
            "t_sent": t_sent - self.t0,
            "status": "passed" if all(v is not False for v in checks.values()) else "failed",
            "checks": checks,
            "latency_s": None,
            "settle_time_s": None,
            "max_amps": max_amps,
            "key_frames": key,
        }

    def failsafe(self, command: dict) -> dict:
        def stop_sending() -> None:
            self.client.ping()  # the head's timeout runs from the last datagram: pin it to now
            self.client.close()

        timeout_s = self.timeout_s + self.margin_s
        serial_before = len(self.serial_log.failsafe_events()) if self.serial_log else 0

        def extra(res, frames, before):
            matched = False
            if frames and self.neutral is not None:
                matched, _ = matches_reference(frames[-1], self.neutral, self.threshold())
            start_ok = (
                res.moved
                and res.motion_start_t - self.last_t_sent >= FAILSAFE_MIN_FRACTION * self.timeout_s
            )
            settle_ok = bool(res.settled and res.settled_t - self.last_t_sent <= timeout_s)
            return {"returned_to_neutral": bool(matched and start_ok and settle_ok)}

        step = self.run_step("failsafe", command, stop_sending,
                             timeout_s + SETTLE_WINDOW_S, timeout_s, extra=extra)
        if self.serial_log is not None:
            events = self.serial_log.failsafe_events()[serial_before:]
            step["evidence"] = {"serial_failsafe_enter": any(e == "ENTER" for _, e in events)}
        return step

    def go(self) -> None:
        self.client.start()
        self.t0 = self.rec.clock()
        prev_cb = getattr(self.rec, "on_sample", None)
        self.rec.on_sample = self.on_sample
        try:
            settle = self.settle_timeout_s
            window = settle + SETTLE_WINDOW_S
            c = self.client
            for name, command in STEPS:
                if self.aborted is not None:
                    self.steps.append(_skipped(name, command))
                    continue
                if name == "baseline":
                    step = self.baseline(command)
                elif name == "failsafe":
                    step = self.failsafe(command)
                else:
                    if name.startswith("look_") or name == "failsafe_prep":
                        action = lambda x=command["x"], y=command["y"]: c.look(x, y)
                    elif name == "blink":
                        action = lambda: c.blink(BLINK_MS)
                    else:
                        action = c.center
                    extra = None
                    if name == "blink":
                        extra = lambda res, frames, before: {
                            "returned_to_reference": bool(
                                frames and before is not None
                                and matches_reference(frames[-1], before, self.threshold())[0]
                            )
                        }
                    step = self.run_step(name, command, action, window, settle, extra=extra)
                self.steps.append(step)
        finally:
            self.rec.on_sample = prev_cb

    def report(self, camera_settings: Any, started_at: str) -> dict:
        done = {s["name"] for s in self.steps}
        for name, command in STEPS:
            if name not in done:
                self.steps.append(_skipped(name, command))
        return {
            "schema_version": SCHEMA_VERSION,
            "started_at": started_at,
            "camera_settings": camera_settings,
            "command_timeout_ms": self.timeout_ms,
            "stall_amps": self.stall_amps,
            "noise_threshold": self.noise.threshold if self.noise is not None else None,
            "steps": self.steps,
            "passed": self.aborted is None and all(s["status"] == "passed" for s in self.steps),
            "aborted": self.aborted,
        }


def _settings_dict(settings: Any) -> Any:
    if dataclasses.is_dataclass(settings) and not isinstance(settings, type):
        return dataclasses.asdict(settings)
    return settings


def run_motion_test(
    client,
    recorder,
    limits,
    stall_amps: float,
    out_dir,
    serial_log=None,
    waiter: Optional[Callable[[float], None]] = None,
    settle_timeout_s: float = 2.0,
    failsafe_margin_s: float = 1.0,
) -> dict:
    """Run the motion test sequence, write ``<out_dir>/report.json`` and return the report dict.

    ``client`` is a started-by-us :class:`brain.eye_client.EyeClient`; it is closed on return.
    On overcurrent the report is returned with ``aborted == 'overcurrent'``. On an exception
    or KeyboardInterrupt the client gets ``center`` and is closed, the report is written, and
    the exception is raised again.
    """
    run = _Run(client, recorder, limits, stall_amps, out_dir, serial_log, waiter,
               settle_timeout_s, failsafe_margin_s)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settings = _settings_dict(getattr(recorder, "camera_settings", None))
    try:
        run.go()
    except KeyboardInterrupt:
        run.emergency_stop("interrupted")
        raise
    except BaseException:
        run.emergency_stop("exception")
        raise
    finally:
        report = run.report(settings, started_at)
        write_report(run.out_dir, report, run.images)
    client.close()
    return report


if __name__ == "__main__":  # python -m bench.motion_test (see bench/cli.py)
    import sys

    from bench.cli import main

    sys.exit(main())
