#!/usr/bin/env python3
"""Reader for the Extech 383273 DMM / data logger over RS-232.

Protocol (from the manual, sections 2.2, 2.5, 3.1, 3.2, 3.8):
  * 9600 baud, 8 data bits, no parity, 1 stop bit.
  * RTS must be held LOW (negative RS-232 voltage) or the meter won't talk.
  * Poll: send any non-reserved byte (the manual recommends SPACE, 0x20).
    Reserved: 9 4 2 1 E G N R W U S T X (they change baud or drive the data logger).
  * Reply: 5 bytes  02 | function/range | digits lo | digits hi | 03
  * Digits: 16-bit word = byte4 << 8 | byte3
      bit 0      polarity (1 = negative; for frequency 1 = MHz, 0 = kHz)
      bit 1      most significant digit (0 or 1)
      bits 2-5   second digit   } each 4-bit field is stored MSB-first
      bits 6-9   third digit    } ("the bit order is reversely interpreted")
      bits 10-13 last digit     }
      bits 14,15 decimal point: 00 none, 01 000.0, 10 00.00, 11 0.000
  * Manual example: 02 0C 21 B1 03 -> digits 0,1,2,3, negative, one decimal -> -12.3

TODO: the function table was transcribed from a photo of the manual. Verify entries against
pages 11-12 before trusting an unusual range.
TODO: special codes for byte 3 (manual section 3.5, page 14) are not decoded yet: any digit
outside 0-9 is reported as overrange/special with value = None.

Usage:
  python3 -m bench.instruments.extech_383273 /dev/ttyUSB0            # print readings
  python3 -m bench.instruments.extech_383273 /dev/ttyUSB0 --rate 4   # 4 polls per second
  python3 -m bench.instruments.extech_383273 /dev/ttyUSB0 --jsonl    # JSON lines (for agents)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass

STX, ETX = 0x02, 0x03
FRAME_LEN = 5

# Bytes the meter interprets as commands (baud change / data-logger control). Never send these.
RESERVED = frozenset(b"9421EGNRWUSTX")
POLL = b" "
assert not (set(POLL) & RESERVED), "POLL must not be a reserved command byte"

# Upper bound on bytes discarded while hunting for STX, so a noisy line can't spin forever.
MAX_RESYNC_BYTES = 2 * FRAME_LEN

# Function/range codes (byte 2).
# TODO: transcribed from a photo of Table 1 (manual pp. 11-12); needs verification against the manual.
FUNCTIONS = {
    0x00: "DCV 200mV", 0x01: "DCV 2V", 0x02: "DCV 20V", 0x03: "DCV 200V", 0x04: "DCV 1000V",
    0x05: "FREQ kHz/MHz", 0x06: "DIODE/CONTINUITY",
    0x08: "OHM 200Ω", 0x09: "OHM 2kΩ", 0x0A: "OHM 20kΩ", 0x0C: "OHM 200kΩ",
    0x10: "OHM 2MΩ", 0x11: "OHM 20MΩ",
    0x12: "CAPACITANCE 2nF", 0x14: "CAPACITANCE 20nF", 0x18: "CAPACITANCE 200nF",
    0x20: "CAPACITANCE 2µF",
    0x21: "DCA 20A", 0x22: "DCA 200mA", 0x24: "DCA 20mA", 0x28: "DCA 2mA", 0x30: "DCA 200µA",
    0x40: "TEMP 200°F", 0x41: "TEMP 2000°F", 0x42: "TEMP 200°C", 0x44: "TEMP 1370°C",
    0x80: "ACV 200mV", 0x81: "ACV 2V", 0x82: "ACV 20V", 0x83: "ACV 200V", 0x84: "ACV 750V",
    0xA1: "ACA 20A", 0xA2: "ACA 200mA", 0xA4: "ACA 20mA", 0xA8: "ACA 2mA", 0xB0: "ACA 200µA",
    0xFF: "HOLD",
}

DECIMALS = {(0, 0): 0, (0, 1): 1, (1, 0): 2, (1, 1): 3}  # (bit14, bit15) -> digits after the point


@dataclass
class Reading:
    t: float
    function: str
    unit: str
    value: float | None        # None = overrange / special code
    display: str
    raw: str


class FrameError(ValueError):
    pass


def _field(word: int, start: int, width: int) -> int:
    """Read a bit field whose first (lowest-numbered) bit is the most significant."""
    v = 0
    for i in range(width):
        v = (v << 1) | ((word >> (start + i)) & 1)
    return v


def _unit(function: str) -> str:
    if function.startswith("FREQ"):
        return "Hz"
    m = re.search(r"\d+\s*([^\d\s/]+)$", function)
    return m.group(1) if m else ""


def decode(frame: bytes, t: float | None = None) -> Reading:
    """Decode one 5-byte reply frame. Pure: no I/O."""
    if len(frame) != FRAME_LEN or frame[0] != STX or frame[4] != ETX:
        raise FrameError(f"bad frame: {bytes(frame).hex(' ')}")
    if t is None:
        t = time.time()
    code, lo, hi = frame[1], frame[2], frame[3]
    function = FUNCTIONS.get(code, f"UNKNOWN 0x{code:02X}")
    word = hi << 8 | lo
    raw = bytes(frame).hex(" ")

    negative = bool(word & 1)
    digits = [(word >> 1) & 1, _field(word, 2, 4), _field(word, 6, 4), _field(word, 10, 4)]
    places = DECIMALS[((word >> 14) & 1, (word >> 15) & 1)]

    unit = _unit(function)
    if function.startswith("FREQ"):
        unit = "MHz" if negative else "kHz"
        negative = False

    if any(d > 9 for d in digits):
        return Reading(t, function, unit, None, "OL/special", raw)

    n = digits[0] * 1000 + digits[1] * 100 + digits[2] * 10 + digits[3]
    value = (-n if negative else n) / (10 ** places)
    s = "".join(map(str, digits))
    if places:
        s = s[: 4 - places] + "." + s[4 - places:]
    display = ("-" if negative else "") + s
    return Reading(t, function, unit, value, display, raw)


def open_port(port: str, baud: int = 9600):
    import serial  # pyserial; imported lazily so tests/CI never need it

    ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.5,
                        rtscts=False, dsrdtr=False)
    ser.rts = False   # RTS low (negative voltage) is required by the meter
    ser.dtr = True
    time.sleep(0.1)
    ser.reset_input_buffer()
    return ser


def read_once(ser) -> Reading:
    """Poll the meter once and return the decoded reading.

    `ser` only needs reset_input_buffer(), write(bytes) and read(n) like a pyserial port.
    """
    ser.reset_input_buffer()
    ser.write(POLL)
    buf = ser.read(FRAME_LEN)
    if len(buf) < FRAME_LEN:
        raise TimeoutError("no reply (is COMM enabled on the meter, and RTS low?)")
    # Resync if we started mid-frame.
    skipped = 0
    while buf[0] != STX:
        if skipped >= MAX_RESYNC_BYTES:
            raise FrameError(f"lost sync: {bytes(buf).hex(' ')}")
        nxt = ser.read(1)
        if not nxt:
            raise FrameError(f"lost sync: {bytes(buf).hex(' ')}")
        buf = buf[1:] + nxt
        skipped += 1
    return decode(buf)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--rate", type=float, default=2.0, help="polls per second")
    ap.add_argument("--count", type=int, default=0, help="stop after N readings (0 = forever)")
    ap.add_argument("--jsonl", action="store_true", help="emit JSON lines")
    a = ap.parse_args(argv)
    if a.rate <= 0:
        ap.error("--rate must be positive")

    ser = open_port(a.port)
    n = 0
    try:
        while a.count == 0 or n < a.count:
            start = time.monotonic()
            try:
                r = read_once(ser)
                print(json.dumps(asdict(r), ensure_ascii=False) if a.jsonl
                      else f"{r.display:>8} {r.unit:<4} {r.function}", flush=True)
            except (TimeoutError, FrameError) as e:
                print(json.dumps({"error": str(e)}) if a.jsonl else f"! {e}", file=sys.stderr, flush=True)
            n += 1
            time.sleep(max(0.0, 1.0 / a.rate - (time.monotonic() - start)))
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
