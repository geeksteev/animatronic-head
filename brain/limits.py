"""Load servo safety limits from ``config/limits.yaml`` and clamp commands to them.

The limits file is human-owned. This module only reads it; it never supplies
fallback values, so a missing or malformed file is an error rather than a guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

DEFAULT_LIMITS_PATH = Path(__file__).resolve().parent.parent / "config" / "limits.yaml"


class LimitsError(ValueError):
    """The limits file is present but malformed."""


class UnknownServoError(KeyError):
    """A servo name that is not defined in the limits file."""


@dataclass(frozen=True)
class ServoLimits:
    name: str
    channel: int
    min: float
    max: float
    neutral: float
    max_speed: float  # degrees per second (``max_speed_dps`` in the YAML)

    def clamp(self, angle: float) -> float:
        """Return ``angle`` limited to ``[min, max]``. Rejects NaN."""
        angle = float(angle)
        if math.isnan(angle):
            raise ValueError(f"angle for servo {self.name!r} is NaN")
        return max(self.min, min(self.max, angle))


@dataclass(frozen=True)
class Limits:
    servos: Mapping[str, ServoLimits]
    command_timeout_ms: int | None = None
    neutral_on_boot: bool | None = None

    def __getitem__(self, servo: str) -> ServoLimits:
        try:
            return self.servos[servo]
        except KeyError:
            raise UnknownServoError(servo) from None

    def __contains__(self, servo: object) -> bool:
        return servo in self.servos

    def names(self) -> list[str]:
        return list(self.servos)

    def clamp(self, servo: str, angle: float) -> float:
        """Clamp ``angle`` for ``servo``. Raises UnknownServoError for unknown names."""
        return self[servo].clamp(angle)


_REQUIRED = ("channel", "min", "max", "neutral", "max_speed_dps")


def _parse_servo(name: str, raw: object) -> ServoLimits:
    if not isinstance(raw, Mapping):
        raise LimitsError(f"servo {name!r}: expected a mapping")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise LimitsError(f"servo {name!r}: missing {', '.join(missing)}")
    try:
        lim = ServoLimits(
            name=name,
            channel=int(raw["channel"]),
            min=float(raw["min"]),
            max=float(raw["max"]),
            neutral=float(raw["neutral"]),
            max_speed=float(raw["max_speed_dps"]),
        )
    except (TypeError, ValueError) as e:
        raise LimitsError(f"servo {name!r}: {e}") from None
    values = (lim.min, lim.max, lim.neutral, lim.max_speed)
    if not all(math.isfinite(v) for v in values):
        raise LimitsError(f"servo {name!r}: non-finite value")
    if not lim.min <= lim.neutral <= lim.max:
        raise LimitsError(f"servo {name!r}: need min <= neutral <= max")
    if lim.max_speed <= 0:
        raise LimitsError(f"servo {name!r}: max_speed_dps must be positive")
    return lim


def load_limits(path: str | Path | None = None) -> Limits:
    """Load limits from ``path`` (default: ``config/limits.yaml`` in the repo).

    Raises FileNotFoundError if the file does not exist and LimitsError if it
    is malformed.
    """
    path = Path(path) if path is not None else DEFAULT_LIMITS_PATH
    with path.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise LimitsError(f"{path}: invalid YAML: {e}") from None

    if not isinstance(data, Mapping):
        raise LimitsError(f"{path}: expected a mapping at top level")
    servos_raw = data.get("servos")
    if not isinstance(servos_raw, Mapping) or not servos_raw:
        raise LimitsError(f"{path}: 'servos' must be a non-empty mapping")

    servos = {str(name): _parse_servo(str(name), raw) for name, raw in servos_raw.items()}

    timeout = None
    neutral_on_boot = None
    failsafe = data.get("failsafe")
    if isinstance(failsafe, Mapping):
        if "command_timeout_ms" in failsafe:
            timeout = int(failsafe["command_timeout_ms"])
        if "neutral_on_boot" in failsafe:
            raw_nob = failsafe["neutral_on_boot"]
            # Only a real YAML boolean: bool("false") would silently be True.
            if not isinstance(raw_nob, bool):
                raise LimitsError(f"{path}: failsafe.neutral_on_boot must be true or false")
            neutral_on_boot = raw_nob

    return Limits(servos=servos, command_timeout_ms=timeout, neutral_on_boot=neutral_on_boot)


_default: Limits | None = None


def clamp(servo: str, angle: float, limits: Limits | None = None) -> float:
    """Clamp ``angle`` for ``servo`` using ``limits`` (default: the repo limits file)."""
    global _default
    if limits is None:
        if _default is None:
            _default = load_limits()
        limits = _default
    return limits.clamp(servo, angle)
