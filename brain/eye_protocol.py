"""Encode and decode eye commands for the head (see ``docs/eye-protocol.md``).

One command per UDP datagram, as a compact UTF-8 JSON object of at most
``MAX_DATAGRAM`` bytes. This module only builds and parses datagrams; it does
not open sockets and does not map commands to servo angles (the head does that
using ``config/limits.yaml``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Union

PROTOCOL_VERSION = 1
DEFAULT_PORT = 4210
MAX_DATAGRAM = 128
BLINK_MIN_MS = 50
BLINK_MAX_MS = 1000
BLINK_DEFAULT_MS = 150

SEQ_MIN = 0
SEQ_MAX = 2**31 - 1  # fits a signed 32-bit int on the head


class ProtocolError(ValueError):
    """A command cannot be encoded, or a datagram is not a valid command."""


@dataclass(frozen=True)
class Look:
    x: float
    y: float


@dataclass(frozen=True)
class Blink:
    ms: int = BLINK_DEFAULT_MS


@dataclass(frozen=True)
class Center:
    pass


@dataclass(frozen=True)
class Ping:
    pass


Command = Union[Look, Blink, Center, Ping]


def _number(value: object, name: str) -> float:
    """Return ``value`` as a finite float. Rejects bools, non-numbers, NaN and inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{name} must be a number, got {type(value).__name__}")
    try:
        f = float(value)
    except OverflowError:
        raise ProtocolError(f"{name} is out of range") from None
    if not math.isfinite(f):
        raise ProtocolError(f"{name} must be finite")
    return f


def _unit(value: object, name: str) -> float:
    """Clamp to ``[-1, 1]`` and round to 3 decimals (the wire precision)."""
    f = max(-1.0, min(1.0, _number(value, name)))
    f = round(f, 3)
    return 0.0 if f == 0 else f  # drop negative zero


def _blink_ms(value: object) -> int:
    f = max(float(BLINK_MIN_MS), min(float(BLINK_MAX_MS), _number(value, "ms")))
    return int(round(f))


def _seq(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"seq must be an integer, got {type(value).__name__}")
    if not SEQ_MIN <= value <= SEQ_MAX:
        raise ProtocolError(f"seq must be in [{SEQ_MIN}, {SEQ_MAX}]")
    return value


def _fmt(f: float) -> str:
    """At most 3 decimal places, no exponent, no trailing zeros: 0.25, -1, 0."""
    s = f"{f:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def encode(cmd: Command, seq: int | None = None) -> bytes:
    """Encode ``cmd`` as a datagram. Keys are written in the order v, seq, cmd, ...

    Clamps x/y to ``[-1, 1]`` and ms to ``[BLINK_MIN_MS, BLINK_MAX_MS]``.
    Raises ProtocolError for NaN/inf, non-numeric fields, a bad ``seq`` or an
    unknown command type.
    """
    parts = [f'"v":{PROTOCOL_VERSION}']
    if seq is not None:
        parts.append(f'"seq":{_seq(seq)}')

    if isinstance(cmd, Look):
        x, y = _unit(cmd.x, "x"), _unit(cmd.y, "y")
        parts += ['"cmd":"look"', f'"x":{_fmt(x)}', f'"y":{_fmt(y)}']
    elif isinstance(cmd, Blink):
        parts += ['"cmd":"blink"', f'"ms":{_blink_ms(cmd.ms)}']
    elif isinstance(cmd, Center):
        parts.append('"cmd":"center"')
    elif isinstance(cmd, Ping):
        parts.append('"cmd":"ping"')
    else:
        raise ProtocolError(f"unknown command type {type(cmd).__name__}")

    data = ("{" + ",".join(parts) + "}").encode("utf-8")
    if len(data) > MAX_DATAGRAM:  # cannot happen with the bounds above; guard anyway
        raise ProtocolError(f"encoded datagram is {len(data)} bytes (max {MAX_DATAGRAM})")
    return data


def _reject_constant(name: str) -> float:
    raise ProtocolError(f"non-finite number {name} not allowed")


def decode(data: bytes) -> Command:
    """Parse a datagram into a command, applying the same clamping as ``encode``.

    Raises ProtocolError for oversize input, invalid UTF-8 or JSON, a wrong or
    missing ``v``, an invalid ``seq``, an unknown ``cmd``, missing or
    non-numeric fields, or non-finite values. Unknown extra keys are ignored.
    """
    if len(data) > MAX_DATAGRAM:
        raise ProtocolError(f"datagram is {len(data)} bytes (max {MAX_DATAGRAM})")
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ProtocolError(f"invalid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("datagram must be a JSON object")

    v = obj.get("v")
    if isinstance(v, bool) or not isinstance(v, int) or v != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {v!r}")
    if "seq" in obj:
        _seq(obj["seq"])

    name = obj.get("cmd")
    if name == "look":
        for field in ("x", "y"):
            if field not in obj:
                raise ProtocolError(f"look: missing {field}")
        return Look(_unit(obj["x"], "x"), _unit(obj["y"], "y"))
    if name == "blink":
        return Blink(_blink_ms(obj.get("ms", BLINK_DEFAULT_MS)))
    if name == "center":
        return Center()
    if name == "ping":
        return Ping()
    raise ProtocolError(f"unknown cmd {name!r}")
