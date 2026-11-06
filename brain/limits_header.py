"""Render ``config/limits.yaml`` as a C/C++ header for the firmware.

Every value comes from :func:`brain.limits.load_limits`; nothing here supplies
a default. Regenerate after changing the limits file::

    python -m brain.limits_header [--limits PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from brain.limits import LimitsError, Limits, load_limits

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HEADER_PATH = REPO_ROOT / "firmware" / "eyes_esp32" / "include" / "limits_generated.h"

_GUARD = "EYE_LIMITS_GENERATED_H"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Upper bound for failsafe.command_timeout_ms. The firmware stores the value in
# a uint32_t, so anything >= 2**32 would be truncated, and a timeout longer than
# a few seconds leaves the servos holding a stale pose for too long to count as
# a fail-safe. 10 s is far above any sensible setting.
MAX_COMMAND_TIMEOUT_MS = 10_000


def _c_float(value: float) -> str:
    text = repr(float(value))
    if "." not in text and "e" not in text:
        text += ".0"
    return text + "f"


def render_header(limits: Limits) -> str:
    """Return the header text for ``limits``.

    Raises LimitsError if the fail-safe timeout is missing, not positive, or
    above ``MAX_COMMAND_TIMEOUT_MS``, or if a servo name or channel cannot be represented in the header.
    """
    timeout = limits.command_timeout_ms
    if timeout is None or timeout <= 0:
        raise LimitsError("failsafe.command_timeout_ms must be set and > 0; the fail-safe cannot be disabled")
    if timeout > MAX_COMMAND_TIMEOUT_MS:
        raise LimitsError(
            f"failsafe.command_timeout_ms is {timeout}; it must be <= {MAX_COMMAND_TIMEOUT_MS} ms"
        )
    if limits.neutral_on_boot is None:
        raise LimitsError("failsafe.neutral_on_boot must be set")

    names = limits.names()
    seen: dict[str, str] = {}
    for name in names:
        if not _IDENT.match(name):
            raise LimitsError(f"servo name {name!r} is not a valid C identifier")
        macro = name.upper()
        if macro in seen:
            raise LimitsError(f"servo names {seen[macro]!r} and {name!r} collide in the header")
        seen[macro] = name
        if not 0 <= limits[name].channel <= 255:
            raise LimitsError(f"servo {name!r}: channel {limits[name].channel} does not fit uint8_t")

    lines = [
        "// GENERATED from config/limits.yaml by brain/limits_header.py. Do not edit.",
        f"#ifndef {_GUARD}",
        f"#define {_GUARD}",
        "",
        "#include <stdint.h>",
        "",
        f"#define EYE_LIMITS_COMMAND_TIMEOUT_MS {int(timeout)}",
        f"#define EYE_LIMITS_NEUTRAL_ON_BOOT {1 if limits.neutral_on_boot else 0}",
        "",
        f"#define EYE_SERVO_COUNT {len(names)}",
    ]
    lines += [f"#define EYE_SERVO_{name.upper()} {i}" for i, name in enumerate(names)]
    lines += [
        "",
        "struct EyeServoLimit {",
        "    const char* name;",
        "    uint8_t channel;",
        "    float min_deg;",
        "    float max_deg;",
        "    float neutral_deg;",
        "    float max_speed_dps;",
        "};",
        "",
        "static const EyeServoLimit EYE_SERVO_LIMITS[EYE_SERVO_COUNT] = {",
    ]
    for name in names:
        s = limits[name]
        fields = [
            f'"{name}"',
            str(s.channel),
            _c_float(s.min),
            _c_float(s.max),
            _c_float(s.neutral),
            _c_float(s.max_speed),
        ]
        lines.append(f"    {{{', '.join(fields)}}},")
    lines += [
        "};",
        "",
        f"#endif  // {_GUARD}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limits", type=Path, default=None, help="limits YAML (default: config/limits.yaml)")
    parser.add_argument("--out", type=Path, default=DEFAULT_HEADER_PATH, help="output header path")
    args = parser.parse_args(argv)

    try:
        text = render_header(load_limits(args.limits))
    except (LimitsError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
