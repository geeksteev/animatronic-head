"""Read the eye ESP32's serial log on the bench.

:class:`SerialLog` reads lines from the ESP32 USB serial port on a daemon thread and keeps them
as ``(t, line)`` pairs, where ``t`` comes from the injected clock (``time.monotonic`` by default).

- Bytes are decoded as UTF-8 with ``errors='replace'``, so boot-ROM noise never raises.
- Lines longer than 1 KiB (``MAX_LINE_BYTES``) are truncated; the rest of the line is dropped.
- At most ``max_lines`` lines are kept; the oldest are dropped first.

It understands these lines from ``firmware/eyes_esp32/src/main.cpp``::

    [failsafe] ENTER: no commands, servos to neutral
    [failsafe] EXIT: commands received
    [wifi] connected, IP 192.0.2.10, UDP port 4210

:meth:`SerialLog.failsafe_events` and :meth:`SerialLog.ip_address` are tracked as lines arrive,
so they survive lines being dropped from the buffer. (The firmware starts in fail-safe and only
prints ``[failsafe] active until first command`` at boot; the first ENTER/EXIT event is usually
an EXIT when the brain starts sending.)

**The ESP32 USB port is shared.** :func:`bench.flash.flash` uploads over the same port. Call
:meth:`SerialLog.close` before flashing and create a new ``SerialLog`` afterwards.

``import bench.serial_log`` does not need pyserial; ``serial`` is imported only when a
``SerialLog`` starts without an injected ``serial_factory``. Install it with
``pip install -e '.[bench]'``.
"""

from __future__ import annotations

import collections
import re
import threading
import time
from typing import Any, Callable, Optional, Pattern, Union

__all__ = ["MAX_LINE_BYTES", "SerialLog", "SerialLogError"]

MAX_LINE_BYTES = 1024
READ_TIMEOUT_S = 0.1

# Exact strings from firmware/eyes_esp32/src/main.cpp (report_failsafe, wifi_service).
FAILSAFE_RE = re.compile(r"\[failsafe\] (ENTER|EXIT):")
IP_RE = re.compile(r"\[wifi\] connected, IP (\d{1,3}(?:\.\d{1,3}){3}),")


class SerialLogError(RuntimeError):
    """The serial port could not be opened."""


def _default_serial_factory(port: str, baud: int) -> Any:
    try:
        import serial  # pyserial, lazy so the module imports without it
    except ImportError as exc:
        raise SerialLogError(
            "pyserial is not installed; run pip install -e '.[bench]'"
        ) from exc
    return serial.Serial(port, baud, timeout=READ_TIMEOUT_S)


class SerialLog:
    """Background reader for the ESP32 serial log. Usable as a context manager.

    ``serial_factory(port, baud)`` must return an object with ``readline() -> bytes`` (which
    returns ``b''`` or a partial line on timeout) and ``close()``.
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        serial_factory: Optional[Callable[[str, int], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        max_lines: int = 10000,
    ):
        if max_lines < 1:
            raise ValueError("max_lines must be at least 1")
        self.port = port
        self.baud = baud
        self._factory = serial_factory or _default_serial_factory
        self._clock = clock
        self._lines: collections.deque = collections.deque(maxlen=max_lines)  # (seq, t, line)
        self._seq = 0
        self._events: collections.deque = collections.deque(maxlen=max_lines)
        self._ip: Optional[str] = None
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ser: Any = None
        self.error: Optional[BaseException] = None

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> "SerialLog":
        if self._thread is not None:
            raise RuntimeError("SerialLog already started")
        self._ser = self._factory(self.port, self.baud)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"SerialLog({self.port})", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """Stop the reader thread and close the port. Safe to call more than once."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None
        with self._cond:
            self._cond.notify_all()

    def __enter__(self) -> "SerialLog":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- reader thread ---------------------------------------------------------------------

    def _run(self) -> None:
        pending = bytearray()
        try:
            while not self._stop.is_set():
                chunk = self._ser.readline()
                if not chunk:
                    continue
                # A timed-out readline() can return part of a line; keep it until the newline.
                while chunk:
                    nl = chunk.find(b"\n")
                    part, chunk = (chunk, b"") if nl < 0 else (chunk[:nl], chunk[nl + 1:])
                    room = MAX_LINE_BYTES - len(pending)
                    if room > 0:
                        pending += part[:room]
                    if nl >= 0:
                        self._add(bytes(pending))
                        pending.clear()
        except Exception as exc:  # port vanished (e.g. unplugged); stop and record it
            self.error = exc
        finally:
            with self._cond:
                self._cond.notify_all()

    def _add(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        t = self._clock()
        with self._cond:
            self._seq += 1
            self._lines.append((self._seq, t, line))
            m = FAILSAFE_RE.search(line)
            if m:
                self._events.append((t, m.group(1)))
            m = IP_RE.search(line)
            if m:
                self._ip = m.group(1)
            self._cond.notify_all()

    # -- queries ---------------------------------------------------------------------------

    def lines(self) -> list[tuple[float, str]]:
        """A snapshot of the buffered ``(t, line)`` pairs, oldest first."""
        with self._cond:
            return [(t, line) for _, t, line in self._lines]

    def wait_for(
        self,
        regex: Union[str, Pattern[str]],
        timeout_s: float,
        since_t: Optional[float] = None,
    ) -> Optional[tuple[float, str]]:
        """Return the first buffered line matching ``regex`` (``re.search``), or None on timeout.

        Only lines with ``t >= since_t`` are considered when ``since_t`` is given. Lines already
        in the buffer count. The timeout is real (wall-clock) time. Returns None early if the
        reader has stopped and nothing matched.
        """
        pattern = re.compile(regex) if isinstance(regex, str) else regex
        deadline = time.monotonic() + max(0.0, timeout_s)
        scanned = 0
        with self._cond:
            while True:
                for seq, t, line in self._lines:
                    if seq <= scanned:
                        continue
                    if (since_t is None or t >= since_t) and pattern.search(line):
                        return (t, line)
                scanned = self._seq
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.running:
                    return None
                self._cond.wait(min(remaining, 0.05))

    def failsafe_events(self) -> list[tuple[float, str]]:
        """``(t, 'ENTER' | 'EXIT')`` for every ``[failsafe] ENTER/EXIT`` line seen, oldest first."""
        with self._cond:
            return list(self._events)

    def ip_address(self) -> Optional[str]:
        """The last IP from a ``[wifi] connected, IP ...`` line, or None if none was seen."""
        with self._cond:
            return self._ip
