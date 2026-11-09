"""Adapter: :class:`bench.meter.CurrentMeter` on top of the Extech 383273 reader.

Uses ``read_once`` from :mod:`bench.instruments.extech_383273` (not modified). Readings are
converted to amps. :class:`bench.meter.MeterError` is raised if the meter is not in a DC
current mode (``DCA ...``), the reading is overload/invalid, or the meter does not answer.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional

from bench.instruments import extech_383273
from bench.meter import MeterError

__all__ = ["ExtechMeter"]

_TO_AMPS = {"A": 1.0, "mA": 1e-3, "µA": 1e-6, "μA": 1e-6, "uA": 1e-6}


class ExtechMeter:
    """``read()`` returns amps. ``ser`` is an open port; ``reader(ser) -> Reading`` is injectable."""

    def __init__(self, ser: Any, reader: Optional[Callable[[Any], Any]] = None) -> None:
        self._ser = ser
        self._reader = reader or extech_383273.read_once

    @classmethod
    def open(cls, port: str) -> "ExtechMeter":
        return cls(extech_383273.open_port(port))

    def read(self) -> float:
        try:
            r = self._reader(self._ser)
        except (TimeoutError, extech_383273.FrameError) as exc:
            raise MeterError(f"meter read failed: {exc}") from exc
        if not r.function.startswith("DCA"):
            raise MeterError(f"meter is not in a DC current mode (shows {r.function})")
        if r.value is None:
            raise MeterError(f"meter overload or invalid reading ({r.display})")
        scale = _TO_AMPS.get(r.unit)
        if scale is None:
            raise MeterError(f"unexpected unit {r.unit} in DC current mode")
        amps = r.value * scale
        if not math.isfinite(amps):
            raise MeterError("meter returned a non-finite value")
        return amps

    def close(self) -> None:
        close = getattr(self._ser, "close", None)
        if close is not None:
            close()
