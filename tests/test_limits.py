import math
from pathlib import Path

import pytest

from brain import limits as L

SAMPLE = """
failsafe:
  command_timeout_ms: 500
servos:
  pan:  { channel: 0, min: 60, max: 120, neutral: 90, max_speed_dps: 300 }
  lid:  { channel: 2, min: 80, max: 110, neutral: 90, max_speed_dps: 600 }
"""


@pytest.fixture
def limits(tmp_path: Path) -> L.Limits:
    p = tmp_path / "limits.yaml"
    p.write_text(SAMPLE)
    return L.load_limits(p)


def test_exposes_per_servo_values(limits):
    pan = limits["pan"]
    assert (pan.channel, pan.min, pan.max, pan.neutral, pan.max_speed) == (0, 60, 120, 90, 300)
    assert limits.command_timeout_ms == 500
    assert set(limits.names()) == {"pan", "lid"}


def test_clamp_above_max(limits):
    assert limits.clamp("pan", 180) == 120
    assert L.clamp("lid", 1e9, limits) == 110


def test_clamp_below_min(limits):
    assert limits.clamp("pan", 0) == 60
    assert L.clamp("lid", -50, limits) == 80


def test_clamp_within_range_unchanged(limits):
    assert limits.clamp("pan", 100) == 100
    assert limits.clamp("pan", 60) == 60
    assert limits.clamp("pan", 120) == 120


def test_clamp_rejects_nan(limits):
    with pytest.raises(ValueError):
        limits.clamp("pan", math.nan)


def test_clamp_infinity_is_clamped(limits):
    assert limits.clamp("pan", math.inf) == 120
    assert limits.clamp("pan", -math.inf) == 60


def test_unknown_servo(limits):
    with pytest.raises(L.UnknownServoError):
        limits.clamp("jaw", 90)
    with pytest.raises(KeyError):
        limits["jaw"]
    assert "jaw" not in limits


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        L.load_limits(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "body",
    [
        "",
        "servos: {}",
        "servos:\n  pan: { channel: 0, min: 60, max: 120, neutral: 90 }",
        "servos:\n  pan: { channel: 0, min: 60, max: 120, neutral: 130, max_speed_dps: 1 }",
        "servos:\n  pan: { channel: 0, min: 120, max: 60, neutral: 90, max_speed_dps: 1 }",
        "servos:\n  pan: { channel: 0, min: 60, max: 120, neutral: 90, max_speed_dps: 0 }",
        "servos:\n  pan: { channel: 0, min: x, max: 120, neutral: 90, max_speed_dps: 1 }",
        "servos: [",
    ],
)
def test_malformed_file(tmp_path, body):
    p = tmp_path / "limits.yaml"
    p.write_text(body)
    with pytest.raises(L.LimitsError):
        L.load_limits(p)


def test_repo_limits_file_loads():
    lim = L.load_limits()
    assert len(lim.names()) > 0
    for name in lim.names():
        s = lim[name]
        assert s.min <= s.neutral <= s.max
        assert L.clamp(name, s.max + 1000) == s.max
        assert L.clamp(name, s.min - 1000) == s.min
