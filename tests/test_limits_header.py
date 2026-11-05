import shutil
import subprocess
from pathlib import Path

import pytest

from brain import limits_header as H
from brain.limits import LimitsError, load_limits

COMMITTED_HEADER = Path(__file__).resolve().parent.parent / "firmware" / "eyes_esp32" / "include" / "limits_generated.h"

SAMPLE = """
failsafe:
  command_timeout_ms: 250
  neutral_on_boot: false
servos:
  pan:  { channel: 0, min: 60, max: 120, neutral: 90, max_speed_dps: 300 }
  lid:  { channel: 7, min: 80.5, max: 110, neutral: 90, max_speed_dps: 600 }
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "limits.yaml"
    p.write_text(body)
    return p


def test_committed_header_matches_yaml():
    expected = H.render_header(load_limits())
    assert COMMITTED_HEADER.read_bytes() == expected.encode("utf-8"), (
        "firmware/eyes_esp32/include/limits_generated.h is stale: run `python -m brain.limits_header`"
    )


def test_render_contents(tmp_path):
    text = H.render_header(load_limits(_write(tmp_path, SAMPLE)))
    assert text.startswith("// GENERATED from config/limits.yaml by brain/limits_header.py. Do not edit.\n")
    assert "#include <stdint.h>" in text
    assert "#define EYE_LIMITS_COMMAND_TIMEOUT_MS 250\n" in text
    assert "#define EYE_LIMITS_NEUTRAL_ON_BOOT 0\n" in text
    assert "#define EYE_SERVO_COUNT 2\n" in text
    assert text.index("#define EYE_SERVO_PAN 0\n") < text.index("#define EYE_SERVO_LID 1\n")
    assert '{"pan", 0, 60.0f, 120.0f, 90.0f, 300.0f},' in text
    assert '{"lid", 7, 80.5f, 110.0f, 90.0f, 600.0f},' in text


def test_missing_timeout_raises(tmp_path):
    body = SAMPLE.replace("  command_timeout_ms: 250\n", "")
    with pytest.raises(LimitsError):
        H.render_header(load_limits(_write(tmp_path, body)))


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_timeout_raises(tmp_path, value):
    body = SAMPLE.replace("command_timeout_ms: 250", f"command_timeout_ms: {value}")
    with pytest.raises(LimitsError):
        H.render_header(load_limits(_write(tmp_path, body)))


def test_missing_neutral_on_boot_raises(tmp_path):
    body = SAMPLE.replace("  neutral_on_boot: false\n", "")
    with pytest.raises(LimitsError):
        H.render_header(load_limits(_write(tmp_path, body)))


@pytest.mark.parametrize(
    "bad",
    [
        "servos:\n  eye-pan: { channel: 0, min: 60, max: 120, neutral: 90, max_speed_dps: 1 }",
        "servos:\n  pan: { channel: 256, min: 60, max: 120, neutral: 90, max_speed_dps: 1 }",
        "servos:\n  pan: { channel: 0, min: 60, max: 120, neutral: 90, max_speed_dps: 1 }\n"
        "  PAN: { channel: 1, min: 60, max: 120, neutral: 90, max_speed_dps: 1 }",
    ],
)
def test_unrepresentable_servo_raises(tmp_path, bad):
    body = "failsafe:\n  command_timeout_ms: 250\n  neutral_on_boot: true\n" + bad
    with pytest.raises(LimitsError):
        H.render_header(load_limits(_write(tmp_path, body)))


def test_cli_writes_header(tmp_path):
    out = tmp_path / "sub" / "limits.h"
    assert H.main(["--limits", str(_write(tmp_path, SAMPLE)), "--out", str(out)]) == 0
    assert out.read_text() == H.render_header(load_limits(tmp_path / "limits.yaml"))


def test_cli_fails_without_timeout(tmp_path):
    out = tmp_path / "limits.h"
    body = SAMPLE.replace("  command_timeout_ms: 250\n", "")
    assert H.main(["--limits", str(_write(tmp_path, body)), "--out", str(out)]) == 1
    assert not out.exists()


def test_header_compiles(tmp_path):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ not available")
    src = tmp_path / "check.cpp"
    src.write_text(
        '#include "limits_generated.h"\n'
        "static_assert(EYE_SERVO_COUNT == 6, \"expected 6 eye servos\");\n"
        "static_assert(sizeof(EYE_SERVO_LIMITS) / sizeof(EYE_SERVO_LIMITS[0]) == EYE_SERVO_COUNT, \"size\");\n"
        "static_assert(EYE_LIMITS_COMMAND_TIMEOUT_MS > 0, \"fail-safe timeout\");\n"
    )
    result = subprocess.run(
        [gxx, "-std=c++17", "-fsyntax-only", "-Wall", "-Werror", "-I", str(COMMITTED_HEADER.parent), str(src)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
