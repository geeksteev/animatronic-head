"""Static hygiene checks for the ESP32 eye firmware (no hardware, no toolchain needed)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware" / "eyes_esp32"
MAIN = FW / "src" / "main.cpp"
SECRETS = FW / "include" / "secrets.h"


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_secrets_h_not_tracked() -> None:
    rel = SECRETS.relative_to(ROOT).as_posix()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel], cwd=ROOT, capture_output=True, text=True
    )
    assert tracked.returncode != 0, f"{rel} is tracked by git"
    ignored = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
    assert ignored.returncode == 0, f"{rel} is not gitignored"


def test_secrets_example_exists() -> None:
    example = FW / "include" / "secrets.h.example"
    assert example.is_file()
    text = example.read_text()
    assert "WIFI_SSID" in text and "WIFI_PASSWORD" in text


def test_main_uses_secrets_only_if_present() -> None:
    src = MAIN.read_text()
    assert '__has_include("secrets.h")' in src
    assert "#warning" in src


def test_main_includes_generated_limits_and_protocol() -> None:
    src = _strip_comments(MAIN.read_text())
    assert re.search(r'#include\s+"limits_generated\.h"', src)
    assert re.search(r'#include\s+"eye_protocol\.h"', src)


_LIMIT_WORDS = re.compile(r"min|max|neutral|timeout|deg|angle|speed", re.I)


def test_main_defines_no_limit_literals() -> None:
    src = _strip_comments(MAIN.read_text())
    offenders = []
    for m in re.finditer(r"^\s*#\s*define\s+(\w+)\s+(.*)$", src, flags=re.M):
        name, value = m.group(1), m.group(2).strip()
        if _LIMIT_WORDS.search(name) and re.search(r"\d", value):
            offenders.append(m.group(0).strip())
    assert not offenders, f"main.cpp defines its own limit literals: {offenders}"
    # Also no constexpr/const numeric limits or timeouts hiding outside #define.
    for m in re.finditer(r"\b(?:constexpr|const)\s+[\w:<> ]+?\s+(\w+)\s*=\s*([^;]+);", src):
        name, value = m.group(1), m.group(2)
        if re.search(r"min|max_deg|neutral|timeout", name, re.I) and re.fullmatch(r"\s*[-\d.fFuUlL]+\s*", value):
            offenders.append(m.group(0))
    assert not offenders, f"main.cpp defines its own limit literals: {offenders}"


def test_main_clamps_against_generated_limits_at_write() -> None:
    src = _strip_comments(MAIN.read_text())
    assert "EYE_SERVO_LIMITS" in src
    assert "EYE_LIMITS_COMMAND_TIMEOUT_MS" in src
    assert "writeMicroseconds" in src


def test_pins_header_has_pulse_range_and_lid_direction() -> None:
    pins = (FW / "include" / "pins.h").read_text()
    assert re.search(r"#define\s+SERVO_MIN_US\s+500\b", pins)
    assert re.search(r"#define\s+SERVO_MAX_US\s+2500\b", pins)
    assert "LID_CLOSED_AT_MAX[4]" in pins
    assert "calibrate on bench" in pins.lower()


def test_secrets_example_has_auth_key_placeholder() -> None:
    text = (FW / "include" / "secrets.h.example").read_text()
    m = re.search(r'#define\s+EYE_AUTH_KEY_HEX\s+"([^"]*)"', text)
    assert m, "secrets.h.example must define EYE_AUTH_KEY_HEX"
    # Not a usable key: copying the example unchanged must fail the build's length check.
    assert len(m.group(1)) != 64


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_brain_secrets_not_tracked() -> None:
    rel = "config/secrets.yaml"
    assert (ROOT / "config" / "secrets.yaml.example").is_file()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel], cwd=ROOT, capture_output=True, text=True
    )
    assert tracked.returncode != 0, f"{rel} is tracked by git"
    ignored = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
    assert ignored.returncode == 0, f"{rel} is not gitignored"


def test_main_authenticates_before_dispatch() -> None:
    src = _strip_comments(MAIN.read_text())
    assert re.search(r'#include\s+"eye_auth\.h"', src)
    assert "EYE_AUTH_KEY_HEX" in src and "parse_auth_key" in src
    assert "open_command(" in src
    # Datagrams must not reach the controller through the unauthenticated parser.
    assert "parse_command(" not in src
