"""Current meter interface for the bench.

A :class:`CurrentMeter` returns the supply current in **amps** from ``read()`` and raises
:class:`MeterError` when it cannot give a trustworthy DC current reading.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["CurrentMeter", "MeterError"]


class MeterError(RuntimeError):
    """The meter is in the wrong mode, overloaded, unreadable or returned an invalid value."""


@runtime_checkable
class CurrentMeter(Protocol):
    def read(self) -> float:
        """Current in amps."""
        ...

    def close(self) -> None: ...
