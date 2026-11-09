"""Write the motion-test report: ``<out_dir>/report.json`` and key frames under ``frames/``.

The report never contains the head's host or IP address.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from bench.png import write_png

__all__ = ["SCHEMA_VERSION", "write_report"]

SCHEMA_VERSION = 1


def write_report(
    out_dir: Path | str, report: Mapping[str, Any], images: Mapping[str, np.ndarray]
) -> Path:
    """Write ``images`` (keyed by path relative to ``out_dir``) and then ``report.json``.

    Returns the path of ``report.json``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    for rel, image in images.items():
        target = (out / rel).resolve()
        if root not in target.parents:
            raise ValueError(f"key frame path escapes out_dir: {rel!r}")
        write_png(target, image)
    path = out / "report.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    return path
