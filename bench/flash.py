"""Flash the eye firmware from the bench with PlatformIO.

:func:`flash` runs ``pio run -d <project_dir> -t upload`` (plus ``--upload-port <port>`` when a
port is given). The arguments are passed to the runner as a list, never through a shell.

Before anything runs, the project must have ``include/secrets.h`` (copy it from
``include/secrets.h.example``). Only its existence is checked: this module never opens, reads or
logs ``secrets.h``.

**The ESP32 USB port is shared.** The upload uses the same serial port that
:class:`bench.serial_log.SerialLog` reads. Close the ``SerialLog`` before calling :func:`flash`
and open a new one afterwards, or the upload fails because the port is busy.

The runner (``subprocess.run`` by default) and the clock are injectable so tests need no
PlatformIO and no hardware.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

__all__ = ["FlashError", "FlashResult", "TAIL_LINES", "flash", "pio_upload_argv"]

TAIL_LINES = 50

DEFAULT_PROJECT_DIR = Path("firmware/eyes_esp32")


@dataclass(frozen=True)
class FlashResult:
    """Outcome of one upload. ``output_tail`` is the last 50 lines of combined stdout/stderr."""

    ok: bool
    returncode: Optional[int]
    duration_s: float
    output_tail: str


class FlashError(RuntimeError):
    """The upload could not start, failed, or timed out.

    ``result`` holds the :class:`FlashResult` (with the output tail) when the upload ran.
    """

    def __init__(self, message: str, result: Optional[FlashResult] = None):
        super().__init__(message)
        self.result = result


def pio_upload_argv(project_dir: Union[str, Path], port: Optional[str] = None) -> list[str]:
    """The exact argv for an upload. Kept separate so tests and logs can show it."""
    argv = ["pio", "run", "-d", str(project_dir), "-t", "upload"]
    if port is not None:
        argv += ["--upload-port", str(port)]
    return argv


def _to_text(output: Union[str, bytes, None]) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _tail(output: Union[str, bytes, None], n: int = TAIL_LINES) -> str:
    return "\n".join(_to_text(output).splitlines()[-n:])


def flash(
    project_dir: Union[str, Path] = DEFAULT_PROJECT_DIR,
    port: Optional[str] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_s: float = 180,
    clock: Callable[[], float] = time.monotonic,
) -> FlashResult:
    """Build and upload the firmware in ``project_dir``. Raises :class:`FlashError` on failure.

    Close any :class:`bench.serial_log.SerialLog` on the same port first (see module docstring).
    """
    project_dir = Path(project_dir)
    secrets = project_dir / "include" / "secrets.h"
    if not secrets.is_file():
        example = project_dir / "include" / "secrets.h.example"
        raise FlashError(
            f"{secrets} is missing. Copy {example} to {secrets} and fill in your own values "
            "before flashing."
        )

    argv = pio_upload_argv(project_dir, port)
    start = clock()
    try:
        proc = runner(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = FlashResult(
            ok=False, returncode=None, duration_s=clock() - start, output_tail=_tail(exc.output)
        )
        raise FlashError(
            f"upload timed out after {timeout_s} s\n{result.output_tail}", result
        ) from exc
    except FileNotFoundError as exc:
        raise FlashError(
            "pio not found; install PlatformIO (pip install platformio)"
        ) from exc

    result = FlashResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        duration_s=clock() - start,
        output_tail=_tail(proc.stdout),
    )
    if not result.ok:
        raise FlashError(
            f"upload failed with exit code {proc.returncode}\n{result.output_tail}", result
        )
    return result
