"""Encode and decode eye commands for the head (see ``docs/eye-protocol.md``).

One command per UDP datagram: a compact UTF-8 JSON payload of at most
``MAX_PAYLOAD`` bytes, followed by a ``TAG_LEN``-character authentication tag
(``sign`` / ``verify``). This module only builds and parses datagrams; it does
not open sockets and does not map commands to servo angles (the head does that
using ``config/limits.yaml``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from typing import Union

PROTOCOL_VERSION = 1
DEFAULT_PORT = 4210
MAX_DATAGRAM = 128  # whole UDP datagram: payload + tag
KEY_LEN = 32  # bytes; written as 64 hex characters in config
TAG_LEN = 32  # lowercase hex characters: HMAC-SHA256 truncated to 128 bits
MAX_PAYLOAD = MAX_DATAGRAM - TAG_LEN
BLINK_MIN_MS = 50
BLINK_MAX_MS = 1000
BLINK_DEFAULT_MS = 150
SERVO_COUNT = 6  # servo channels 0..5: pan, tilt, then the 4 lids (see docs/eye-protocol.md)
SERVO_DEG_MIN = 0.0
SERVO_DEG_MAX = 180.0

SEQ_MIN = 0
SEQ_MAX = 2**53 - 1  # exact in a double; the head stores it as uint64


class ProtocolError(ValueError):
    """A command cannot be encoded, or a datagram is not a valid command."""


class AuthError(ProtocolError):
    """A datagram has a missing or wrong authentication tag, or no ``seq``."""


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


@dataclass(frozen=True)
class Servo:
    """Set one servo's target angle. ``ch``: 0 pan, 1 tilt, 2..5 lids (see the protocol doc)."""

    ch: int
    deg: float


Command = Union[Look, Blink, Center, Ping, Servo]


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


def _channel(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"ch must be an integer, got {type(value).__name__}")
    if not 0 <= value < SERVO_COUNT:
        raise ProtocolError(f"ch must be in [0, {SERVO_COUNT - 1}]")
    return value


def _deg(value: object) -> float:
    """Clamp to ``[SERVO_DEG_MIN, SERVO_DEG_MAX]`` and round to 3 decimals."""
    f = max(SERVO_DEG_MIN, min(SERVO_DEG_MAX, _number(value, "deg")))
    f = round(f, 3)
    return 0.0 if f == 0 else f


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

    Clamps x/y to ``[-1, 1]``, ms to ``[BLINK_MIN_MS, BLINK_MAX_MS]`` and deg to
    ``[SERVO_DEG_MIN, SERVO_DEG_MAX]``. Raises ProtocolError for NaN/inf,
    non-numeric fields, a ``ch`` that is not an integer in ``[0, SERVO_COUNT)``,
    a bad ``seq`` or an unknown command type.
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
    elif isinstance(cmd, Servo):
        ch, deg = _channel(cmd.ch), _deg(cmd.deg)
        parts += ['"cmd":"servo"', f'"ch":{ch}', f'"deg":{_fmt(deg)}']
    else:
        raise ProtocolError(f"unknown command type {type(cmd).__name__}")

    data = ("{" + ",".join(parts) + "}").encode("utf-8")
    if len(data) > MAX_PAYLOAD:  # cannot happen with the bounds above; guard anyway
        raise ProtocolError(f"encoded payload is {len(data)} bytes (max {MAX_PAYLOAD})")
    return data


def _reject_constant(name: str) -> float:
    raise ProtocolError(f"non-finite number {name} not allowed")


def decode(data: bytes) -> Command:
    """Parse a payload (no tag) into a command, applying the same clamping as ``encode``.

    Raises ProtocolError for oversize input, invalid UTF-8 or JSON, a wrong or
    missing ``v``, an invalid ``seq``, an unknown ``cmd``, missing or
    non-numeric fields, non-finite values, or a ``ch`` that is not an integer
    in ``[0, SERVO_COUNT)``. Unknown extra keys are ignored.
    """
    return _decode(data)[1]


def _decode(data: bytes) -> tuple[int | None, Command]:
    if len(data) > MAX_PAYLOAD:
        raise ProtocolError(f"payload is {len(data)} bytes (max {MAX_PAYLOAD})")
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
    seq = _seq(obj["seq"]) if "seq" in obj else None

    name = obj.get("cmd")
    if name == "look":
        for field in ("x", "y"):
            if field not in obj:
                raise ProtocolError(f"look: missing {field}")
        return seq, Look(_unit(obj["x"], "x"), _unit(obj["y"], "y"))
    if name == "blink":
        return seq, Blink(_blink_ms(obj.get("ms", BLINK_DEFAULT_MS)))
    if name == "center":
        return seq, Center()
    if name == "ping":
        return seq, Ping()
    if name == "servo":
        for field in ("ch", "deg"):
            if field not in obj:
                raise ProtocolError(f"servo: missing {field}")
        return seq, Servo(_channel(obj["ch"]), _deg(obj["deg"]))
    raise ProtocolError(f"unknown cmd {name!r}")


# -- authentication -----------------------------------------------------------

_KEY_HEX = re.compile(r"[0-9a-fA-F]{%d}" % (2 * KEY_LEN))


def parse_key(text: str) -> bytes:
    """Parse a shared key written as exactly 64 hex characters. Rejects an all-zero key."""
    if not isinstance(text, str) or not _KEY_HEX.fullmatch(text.strip()):
        raise ValueError(f"eye key must be exactly {2 * KEY_LEN} hex characters")
    key = bytes.fromhex(text.strip())
    if not any(key):
        raise ValueError("eye key must not be all zeros")
    return key


def check_key(key: bytes) -> None:
    """Raise ValueError unless ``key`` is ``KEY_LEN`` bytes."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
        raise ValueError(f"eye key must be {KEY_LEN} bytes")


def tag(payload: bytes, key: bytes) -> bytes:
    """The ``TAG_LEN`` lowercase hex characters authenticating ``payload``."""
    check_key(key)
    return hmac.new(bytes(key), payload, hashlib.sha256).hexdigest()[:TAG_LEN].encode("ascii")


def sign(payload: bytes, key: bytes) -> bytes:
    """Append the authentication tag to ``payload``."""
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload is {len(payload)} bytes (max {MAX_PAYLOAD})")
    return payload + tag(payload, key)


def verify(datagram: bytes, key: bytes) -> bytes:
    """Check the tag on ``datagram`` and return the payload. Raises AuthError."""
    check_key(key)
    if len(datagram) > MAX_DATAGRAM:
        raise AuthError(f"datagram is {len(datagram)} bytes (max {MAX_DATAGRAM})")
    if len(datagram) < TAG_LEN:
        raise AuthError("datagram too short for a tag")
    payload, got = datagram[:-TAG_LEN], datagram[-TAG_LEN:]
    if not hmac.compare_digest(tag(payload, key), got):
        raise AuthError("bad authentication tag")
    return payload


def encode_signed(cmd: Command, seq: int, key: bytes) -> bytes:
    """Encode ``cmd`` with ``seq`` (required) and append the tag. What the head accepts."""
    if seq is None:
        raise ProtocolError("seq is required on authenticated datagrams")
    return sign(encode(cmd, seq), key)


def decode_signed(datagram: bytes, key: bytes) -> tuple[int, Command]:
    """Verify and decode a datagram. Returns ``(seq, cmd)``.

    Raises AuthError for a bad tag or a missing ``seq``, ProtocolError for an
    invalid payload. Does not check for replays: see ``ReplayGuard``.
    """
    seq, cmd = _decode(verify(datagram, key))
    if seq is None:
        raise AuthError("seq is required on authenticated datagrams")
    return seq, cmd


class ReplayGuard:
    """The head's replay rule: accept a seq only if it is above every seq accepted so far.

    Reference implementation of ``ReplayGuard`` in the firmware's eye_core.
    """

    def __init__(self) -> None:
        self.last: int | None = None

    def accept(self, seq: int) -> bool:
        if self.last is not None and seq <= self.last:
            return False
        self.last = seq
        return True
