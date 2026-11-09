"""``python -m bench.motion_test``: flash, connect, run the motion test and write the report.

Exit codes: 0 the test passed, 1 it failed or was aborted, 2 a setup error (bad arguments, flash
failure, no IP, camera or meter could not be opened, ...).

``main(argv, factories=None)`` takes an optional dict of factories so tests can inject fakes.
Every key is optional; missing ones use the real implementation:

- ``flash(project_dir, port)``: default :func:`bench.flash.flash`.
- ``serial_log(port)``: returns a started log with ``wait_for``, ``ip_address``,
  ``failsafe_events`` and ``close``.
- ``camera(settings)``, ``meter(port)``, ``recorder(camera, meter, settings)``.
- ``sock()``: the UDP socket for the :class:`brain.eye_client.EyeClient`; ``key()``: its HMAC key.
- ``clock()``: epoch seconds, used for the default ``--out`` directory name.
- ``waiter(seconds)``: how the runner waits.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from bench import flash as flash_mod
from bench.camera import CameraError, CameraSettings, OpenCVCamera
from bench.meter import MeterError
from bench.motion_test import run_motion_test
from bench.recorder import Recorder
from bench.serial_log import IP_RE, SerialLog, SerialLogError
from brain.eye_client import HOST_ENV, EyeClient
from brain.limits import LimitsError, load_limits

__all__ = ["main"]

IP_TIMEOUT_S = 30.0
DEFAULT_LIMITS = "config/limits.yaml"
DEFAULT_OUT_ROOT = "bench/out"


def _positive_finite(text: str) -> float:
    try:
        f = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not math.isfinite(f) or f <= 0:
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    return f


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bench.motion_test",
        description="Run the bench motion test against the eye head.",
    )
    p.add_argument("--port", required=True, help="ESP32 USB serial port, e.g. /dev/ttyUSB0")
    p.add_argument("--host", help=f"head address (default: ${HOST_ENV}, else the IP in the serial log)")
    p.add_argument("--stall-amps", required=True, type=_positive_finite,
                   help="abort above this current, in amps (no default; finite and > 0)")
    meter = p.add_mutually_exclusive_group(required=True)
    meter.add_argument("--meter-port", help="Extech 383273 RS-232 port")
    meter.add_argument("--no-meter", action="store_true",
                       help="run without a current meter (no stall protection)")
    p.add_argument("--skip-flash", action="store_true", help="do not flash the firmware first")
    p.add_argument("--limits", default=DEFAULT_LIMITS, help="limits file (read-only; default: %(default)s)")
    p.add_argument("--out", help=f"output directory (default: {DEFAULT_OUT_ROOT}/<UTC timestamp>/)")
    add_camera_args(p)
    return p


def add_camera_args(p: argparse.ArgumentParser) -> None:
    """Add the camera flag group (shared with ``bench.calibrate``)."""
    d = CameraSettings()
    c = p.add_argument_group("camera")
    c.add_argument("--camera", default=d.device, help="video device (default: %(default)s)")
    c.add_argument("--width", type=int, default=d.width)
    c.add_argument("--height", type=int, default=d.height)
    c.add_argument("--fps", type=int, default=d.fps)
    c.add_argument("--fourcc", default=d.fourcc)
    c.add_argument("--exposure", type=int, default=d.exposure)
    c.add_argument("--gain", type=int, default=d.gain)
    c.add_argument("--focus", type=int, default=d.focus)
    c.add_argument("--wb-temperature", type=int, default=d.wb_temperature)
    c.add_argument("--warmup-frames", type=int, default=d.warmup_frames)


def camera_settings(args: argparse.Namespace) -> CameraSettings:
    return CameraSettings(
        device=args.camera, width=args.width, height=args.height, fps=args.fps,
        fourcc=args.fourcc, exposure=args.exposure, gain=args.gain, focus=args.focus,
        wb_temperature=args.wb_temperature, warmup_frames=args.warmup_frames,
    )


def _err(msg: str) -> None:
    print(f"motion_test: {msg}", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None,
         factories: Optional[Mapping[str, Any]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    f = dict(factories or {})

    settings = camera_settings(args)
    clock = f.get("clock", time.time)
    if args.out:
        out_dir = Path(args.out)
    else:
        stamp = datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(DEFAULT_OUT_ROOT) / stamp

    serial_log = camera = meter = recorder = client = None
    try:
        # -- setup: any failure here is exit code 2 and nothing has moved yet --
        try:
            limits = load_limits(args.limits)
            if not args.skip_flash:
                flash = f.get("flash", flash_mod.flash)
                flash(flash_mod.DEFAULT_PROJECT_DIR, args.port)
            serial_log = f["serial_log"](args.port) if "serial_log" in f else SerialLog(args.port).start()
            host = args.host or os.environ.get(HOST_ENV)
            if not host:
                if serial_log.wait_for(IP_RE.pattern, IP_TIMEOUT_S) is None:
                    raise RuntimeError(
                        f"no IP in the serial log after {IP_TIMEOUT_S:g} s; "
                        f"pass --host or set {HOST_ENV}")
                host = serial_log.ip_address()
                if not host:
                    raise RuntimeError("could not read the head's IP from the serial log")
            camera = f["camera"](settings) if "camera" in f else OpenCVCamera(settings)
            camera.open()
            if not args.no_meter:
                meter = f["meter"](args.meter_port) if "meter" in f else _open_meter(args.meter_port)
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
        except (flash_mod.FlashError, SerialLogError, CameraError, MeterError, LimitsError,
                OSError, ValueError, RuntimeError, ImportError) as exc:
            _err(f"setup failed: {exc}")
            return 2

        # -- run --
        base_wait = f.get("waiter", time.sleep)
        check = getattr(recorder, "check", None)

        def waiter(seconds: float) -> None:
            base_wait(seconds)
            if check is not None:
                check()  # a dead meter or camera thread must stop the test

        if hasattr(recorder, "start"):
            recorder.start()
        try:
            report = run_motion_test(client, recorder, limits, args.stall_amps, out_dir,
                                     serial_log=serial_log, waiter=waiter)
        except KeyboardInterrupt:
            _err("interrupted; servos sent to center")
            return 1
        except Exception as exc:
            _err(f"test aborted: {type(exc).__name__}: {exc} (report written to {out_dir})")
            return 1
        for s in report["steps"]:
            print(f"{s['status']:>8}  {s['name']}")
        print(f"{'PASSED' if report['passed'] else 'FAILED'}"
              f"{' (aborted: ' + report['aborted'] + ')' if report['aborted'] else ''}"
              f"; report: {out_dir / 'report.json'}")
        return 0 if report["passed"] else 1
    finally:
        for res in (recorder, client, meter, camera, serial_log):
            close = getattr(res, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass


def _open_meter(port: str):
    from bench.instruments.extech_meter import ExtechMeter

    return ExtechMeter.open(port)


if __name__ == "__main__":
    sys.exit(main())
