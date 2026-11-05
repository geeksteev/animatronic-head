"""Build and run the eye_core host tests (tests/native/test_*.cpp) with g++."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB_SRC = ROOT / "firmware" / "eyes_esp32" / "lib" / "eye_core" / "src"
NATIVE_DIR = ROOT / "tests" / "native"
TEST_SRCS = ["test_eye_controller.cpp", "test_eye_protocol.cpp"]

CXXFLAGS = ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-fno-exceptions", "-O1"]


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not available")
@pytest.mark.parametrize("test_src", TEST_SRCS)
def test_eye_core_native(tmp_path: Path, test_src: str) -> None:
    sources = sorted(str(p) for p in LIB_SRC.glob("*.cpp"))
    assert sources, f"no sources in {LIB_SRC}"
    binary = tmp_path / Path(test_src).stem
    build = subprocess.run(
        ["g++", *CXXFLAGS, f"-I{LIB_SRC}", *sources, str(NATIVE_DIR / test_src), "-o", str(binary), "-lm"],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"compile failed:\n{build.stdout}\n{build.stderr}"

    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, f"native tests failed:\n{run.stdout}\n{run.stderr}"


def test_eye_core_has_no_arduino_dependency() -> None:
    for path in LIB_SRC.iterdir():
        text = path.read_text()
        assert "Arduino.h" not in text, f"{path.name} includes Arduino.h"
