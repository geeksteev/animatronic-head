"""``python -m bench.calibrate SERVO``: find a servo's mechanical stops and write a PROPOSAL.

The servo is stepped from neutral toward max, then toward min, a few degrees at a time. A step
sequence ends at the first of: a current stop (a sample above ``--stall-amps`` or above
baseline + ``--spike-amps``), an image stop (the picture did not change for ``--still-steps``
consecutive steps; the stop angle is the last angle at which it still changed), or the edge
of the effective bound. On a stop the servo is immediately commanded ``--backoff`` degrees back
toward neutral and that angle is the proposal for the direction.

**Bounds.** The head clamps every command to ``config/limits.yaml`` as well, so calibration
cannot search past the current limits. The effective bound is ``[hard-min, hard-max]``
intersected with the servo's ``[min, max]`` from the limits; every command is clamped to
the limits and to that bound. If the head's clamp were not part of the bound, it would look
like a mechanical stop. To search wider, a human must first widen ``config/limits.yaml``,
regenerate the header and reflash.

**Output is a proposal only.** ``--out`` is a YAML file for a human to review; this tool never
writes ``config/limits.yaml`` and refuses an ``--out`` that resolves into ``config/`` or whose
name starts with ``limits.``. Evidence PNGs are written next to it.

Exit codes: 0 done (also when no stop was found), 1 aborted (exception or Ctrl-C; the servo is
sent to neutral), 2 setup error or refused arguments, before anything moved.

``main(argv, factories=None)`` takes the same optional factories as :mod:`bench.cli`
(``serial_log``, ``camera``, ``meter``, ``recorder``, ``sock``, ``key``, ``clock``, ``waiter``).
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import yaml

from bench import cli
from bench.camera import CameraError, OpenCVCamera
from bench.meter import MeterError
from bench.motion import NoiseFloor, diff_score, preprocess
from bench.png import write_png
from bench.recorder import Recorder
from bench.serial_log import IP_RE, SerialLog, SerialLogError
from brain.eye_client import HOST_ENV, EyeClient
from brain.limits import LimitsError, UnknownServoError, load_limits

__all__ = ["CalibrationError", "check_out_path", "effective_bound", "main", "run_calibration"]

HEADER = "# PROPOSAL ONLY - a human must review it before editing config/limits.yaml\n"
REPO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SETTLE_S = 1.0
BASELINE_SAMPLES = 10
MAX_BASELINE_POLLS = 200


class CalibrationError(Exception):
    """Refused arguments or an unusable setup (exit code 2)."""


def effective_bound(limits, servo: str, hard_min: float, hard_max: float) -> tuple[float, float]:
    """``[hard_min, hard_max]`` intersected with the servo's limits; must contain neutral."""
    lim = limits[servo]
    lo, hi = max(hard_min, lim.min), min(hard_max, lim.max)
    if not lo <= hi:
        raise CalibrationError(
            f"empty effective bound: [{hard_min:g}, {hard_max:g}] and the limits "
            f"[{lim.min:g}, {lim.max:g}] do not overlap")
    if not lo <= lim.neutral <= hi:
        raise CalibrationError(
            f"effective bound [{lo:g}, {hi:g}] does not contain neutral {lim.neutral:g}")
    return lo, hi


def check_out_path(out: Path | str, limits_path: Path | str | None = None) -> Path:
    """Resolve ``out`` and refuse anything that could overwrite the human-owned limits."""
    raw = Path(out)
    resolved = raw.resolve()
    config = REPO_CONFIG_DIR.resolve()
    if resolved == config or config in resolved.parents:
        raise CalibrationError(f"refusing --out {out}: it is inside {config}")
    for name in (raw.name, resolved.name):
        if name.startswith("limits."):
            raise CalibrationError(f"refusing --out {out}: file names starting with 'limits.' are reserved")
    if limits_path is not None and resolved == Path(limits_path).resolve():
        raise CalibrationError(f"refusing --out {out}: it is the limits file")
    return resolved


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class _Calibration:
    def __init__(self, client, recorder, limits, servo, bound, stall_amps, spike_amps, step,
                 dwell, backoff, still_steps, waiter):
        self.client = client
        self.rec = recorder
        self.limits = limits
        self.servo = servo
        self.lim = limits[servo]
        self.bmin, self.bmax = bound
        self.stall_amps = stall_amps
        self.spike_amps = spike_amps
        self.step = step
        self.dwell = dwell
        self.backoff = backoff
        self.still_steps = still_steps
        self.wait = waiter if waiter is not None else _sleep
        self.angle: float = self.lim.neutral
        self.baseline = 0.0
        self.threshold = 0.0

    # -- the only way anything is commanded ---------------------------------------------------

    def send(self, angle: float) -> float:
        a = self.limits.clamp(self.servo, angle)
        a = min(max(a, self.bmin), self.bmax)
        assert self.bmin <= a <= self.bmax, f"{a} left the effective bound"
        self.client.servo(self.servo, a)
        self.angle = a
        return a

    # -- observations -------------------------------------------------------------------------

    def samples_since(self, t0: float) -> list[float]:
        return [a for t, a in self.rec.samples if t > t0]

    def last_image(self):
        frames = self.rec.frames
        if not frames:
            raise RuntimeError("no camera frames")
        return frames[-1].image.copy()

    def move_and_wait(self, angle: float, extra: float = 0.0) -> float:
        """Command ``angle``, wait for it to arrive and dwell. Returns the command time."""
        t0 = self.rec.clock()
        travel = abs(angle - self.angle) / self.lim.max_speed
        self.send(angle)
        self.wait(max(self.dwell, travel + extra))
        return t0

    # -- steps --------------------------------------------------------------------------------

    def setup(self) -> None:
        self.send(self.lim.neutral)
        self.wait(SETTLE_S)
        mark = self.rec.clock()
        for _ in range(MAX_BASELINE_POLLS):
            self.wait(0.1)
            frames = [f for f in self.rec.frames if f.t > mark]
            samples = self.samples_since(mark)
            if len(samples) >= BASELINE_SAMPLES and len(frames) >= 2:
                break
        else:
            raise RuntimeError("not enough camera frames and current samples for a baseline")
        self.threshold = NoiseFloor.from_frames(frames).threshold
        self.baseline = float(statistics.median(samples))

    def sweep(self, name: str, sign: int) -> dict:
        neutral = self.lim.neutral
        edge = self.bmax if sign > 0 else self.bmin
        delta = min(self.step, self.lim.max_speed * self.dwell)
        prev = preprocess(self.last_image())
        last_changed, still, k = neutral, 0, 0
        trace: list[dict] = []
        reason: Optional[str] = None
        stop_angle: Optional[float] = None
        while True:
            if self.angle == edge:
                reason = "bound"
                break
            k += 1
            target = neutral + sign * k * delta
            if sign * (target - edge) > 0:
                target = edge
            t0 = self.move_and_wait(target)
            amps = self.samples_since(t0)
            if not amps:
                raise RuntimeError("no current samples during a step: the meter has stopped")
            peak = max(amps)
            trace.append({"angle": self.angle, "max_amps": peak})
            if peak > self.stall_amps or (
                    self.spike_amps is not None and peak > self.baseline + self.spike_amps):
                reason, stop_angle = "current", self.angle
                break
            cur = preprocess(self.last_image())
            if diff_score(prev, cur) <= self.threshold:
                still += 1
            else:
                still, last_changed = 0, self.angle
            prev = cur
            if still >= self.still_steps:
                reason, stop_angle = "image", last_changed
                break
        stop_img = self.last_image()
        result: dict[str, Any] = {"stop_found": reason != "bound", "reason": reason}
        if reason == "bound":
            result.update(stop_angle=None, proposal=edge, backoff_img=None)
        else:
            # Back off immediately, toward neutral, never past neutral.
            back = stop_angle - sign * self.backoff
            back = max(neutral, back) if sign > 0 else min(neutral, back)
            self.move_and_wait(back)
            result.update(stop_angle=stop_angle, proposal=self.angle, backoff_img=self.last_image())
        result.update(current_trace=trace, stop_img=stop_img)
        self.move_and_wait(neutral, extra=SETTLE_S / 2)
        return result


def run_calibration(client, recorder, limits, servo: str, hard_min: float, hard_max: float,
                    stall_amps: float, out: Path | str, spike_amps: Optional[float] = None,
                    step: float = 1.0, dwell: float = 0.3, backoff: float = 5.0,
                    still_steps: int = 2, waiter: Optional[Callable[[float], None]] = None,
                    now: Callable[[], float] = time.time) -> dict:
    """Run both sweeps, write the proposal and evidence next to ``out``, return the proposal.

    The client is always closed. On any exception (including KeyboardInterrupt) the servo is
    first commanded to neutral. Raises :class:`CalibrationError` before anything is sent.
    """
    out = check_out_path(out)
    bound = effective_bound(limits, servo, hard_min, hard_max)
    run = _Calibration(client, recorder, limits, servo, bound, stall_amps, spike_amps, step,
                       dwell, backoff, still_steps, waiter)
    print(f"effective bound for {servo}: [{bound[0]:g}, {bound[1]:g}] degrees")
    try:
        client.start()  # heartbeat on throughout
        run.setup()
        results = {"max": run.sweep("max", +1), "min": run.sweep("min", -1)}
    except BaseException:
        try:
            run.send(run.lim.neutral)
        except Exception:
            pass
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass

    evidence: dict[str, dict] = {}
    for name, r in results.items():
        evidence[name] = {}
        for kind in ("stop", "backoff"):
            img = r.pop(f"{kind}_img")
            fname = f"{servo}_{name}_{kind}.png"
            if img is not None:
                write_png(out.parent / fname, img)
            evidence[name][kind] = fname if img is not None else None
    proposal: dict[str, Any] = {
        "servo": servo,
        "channel": run.lim.channel,
        "proposed_min": float(results["min"]["proposal"]),
        "proposed_max": float(results["max"]["proposal"]),
        "neutral": run.lim.neutral,
    }
    for name, r in results.items():
        proposal[name] = {
            "stop_found": r["stop_found"],
            "reason": r["reason"],
            "stop_angle": r["stop_angle"],
            "current_trace": r["current_trace"],
            "evidence": evidence[name],
        }
    proposal.update(
        effective_bound={"min": bound[0], "max": bound[1]},
        step=float(step),
        backoff=float(backoff),
        generated_at=datetime.fromtimestamp(now(), timezone.utc).isoformat(),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HEADER + yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")
    return proposal


def _finite(text: str) -> float:
    try:
        f = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not math.isfinite(f):
        raise argparse.ArgumentTypeError("must be finite")
    return f


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _parser() -> argparse.ArgumentParser:
    pos = cli._positive_finite
    p = argparse.ArgumentParser(
        prog="python -m bench.calibrate",
        description="Find a servo's mechanical stops and write a proposal file (never the limits).",
    )
    p.add_argument("servo", help="servo name from the limits file, e.g. eye_pan")
    p.add_argument("--hard-min", required=True, type=_finite, help="lowest angle to try, degrees")
    p.add_argument("--hard-max", required=True, type=_finite, help="highest angle to try, degrees")
    p.add_argument("--stall-amps", required=True, type=pos, help="stop above this current, amps")
    p.add_argument("--spike-amps", type=pos, help="also stop above baseline + this, amps")
    p.add_argument("--step", type=pos, default=1.0, help="degrees per step (default: %(default)s)")
    p.add_argument("--dwell", type=pos, default=0.3, help="seconds to wait per step (default: %(default)s)")
    p.add_argument("--backoff", type=pos, default=5.0, help="degrees to back off (default: %(default)s)")
    p.add_argument("--still-steps", type=_positive_int, default=2,
                   help="unchanged images that count as a stop (default: %(default)s)")
    p.add_argument("--out", required=True, help="proposal YAML path (never config/ or limits.*)")
    p.add_argument("--port", help="ESP32 serial port, to read the IP when --host is not given")
    p.add_argument("--host", help=f"head address (default: ${HOST_ENV}, else the IP in the serial log)")
    p.add_argument("--meter-port", required=True, help="Extech 383273 RS-232 port (mandatory)")
    p.add_argument("--limits", default=cli.DEFAULT_LIMITS,
                   help="limits file (read-only; default: %(default)s)")
    cli.add_camera_args(p)
    return p


def _err(msg: str) -> None:
    print(f"calibrate: {msg}", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None,
         factories: Optional[Mapping[str, Any]] = None) -> int:
    args = _parser().parse_args(argv)
    f = dict(factories or {})
    settings = cli.camera_settings(args)

    serial_log = camera = meter = recorder = client = None
    try:
        # -- setup and refusals: exit 2, nothing has moved --
        try:
            limits = load_limits(args.limits)
            if args.servo not in limits:
                raise CalibrationError(f"unknown servo {args.servo!r}; known: {', '.join(limits.names())}")
            bound = effective_bound(limits, args.servo, args.hard_min, args.hard_max)
            out = check_out_path(args.out, args.limits)
            host = args.host or os.environ.get(HOST_ENV)
            if not host:
                if "serial_log" not in f and not args.port:
                    raise CalibrationError(f"pass --host, set {HOST_ENV} or give --port to read the IP")
                serial_log = f["serial_log"](args.port) if "serial_log" in f else SerialLog(args.port).start()
                if serial_log.wait_for(IP_RE.pattern, cli.IP_TIMEOUT_S) is None:
                    raise RuntimeError(f"no IP in the serial log after {cli.IP_TIMEOUT_S:g} s")
                host = serial_log.ip_address()
                if not host:
                    raise RuntimeError("could not read the head's IP from the serial log")
            camera = f["camera"](settings) if "camera" in f else OpenCVCamera(settings)
            camera.open()
            meter = f["meter"](args.meter_port) if "meter" in f else cli._open_meter(args.meter_port)
            meter.read()  # fail early: wrong mode, overload or no reply
            if "recorder" in f:
                recorder = f["recorder"](camera, meter, settings)
            else:
                recorder = Recorder(camera, meter, camera_settings=dataclasses.asdict(settings))
            kwargs: dict[str, Any] = {"limits": limits}
            if "sock" in f:
                kwargs["sock"] = f["sock"]()
            if "key" in f:
                kwargs["key"] = f["key"]()
            client = EyeClient(host, **kwargs)
        except (CalibrationError, SerialLogError, CameraError, MeterError, LimitsError,
                UnknownServoError, OSError, ValueError, RuntimeError, ImportError) as exc:
            _err(f"setup failed: {exc}")
            return 2

        base_wait = f.get("waiter", time.sleep)
        check = getattr(recorder, "check", None)

        def waiter(seconds: float) -> None:
            base_wait(seconds)
            if check is not None:
                check()  # a dead meter or camera thread must stop the run

        if hasattr(recorder, "start"):
            recorder.start()
        try:
            proposal = run_calibration(
                client, recorder, limits, args.servo, args.hard_min, args.hard_max,
                args.stall_amps, out, spike_amps=args.spike_amps, step=args.step,
                dwell=args.dwell, backoff=args.backoff, still_steps=args.still_steps,
                waiter=waiter, now=f.get("clock", time.time))
        except KeyboardInterrupt:
            _err("interrupted; servo sent to neutral")
            return 1
        except Exception as exc:
            _err(f"aborted: {type(exc).__name__}: {exc}; servo sent to neutral")
            return 1
        for name in ("min", "max"):
            r = proposal[name]
            print(f"{name}: stop_found={r['stop_found']} reason={r['reason']} "
                  f"stop_angle={r['stop_angle']}")
        print(f"PROPOSAL ONLY (review before editing config/limits.yaml): "
              f"min {proposal['proposed_min']:g}, max {proposal['proposed_max']:g}; written to {out}")
        return 0
    finally:
        for res in (recorder, client, meter, camera, serial_log):
            close = getattr(res, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
