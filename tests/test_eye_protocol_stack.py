"""Stack use of the C++ eye_protocol parser must not depend on the datagram.

Compiles eye_protocol.cpp with -fstack-usage and -fcallgraph-info=su and checks that every
function has a static frame size and that the call graph has no cycles (no recursion), so
parse_command's worst-case stack is a fixed number that an attacker cannot grow.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB_SRC = ROOT / "firmware" / "eyes_esp32" / "lib" / "eye_core" / "src"
SOURCE = LIB_SRC / "eye_protocol.cpp"

CXXFLAGS = ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-fno-exceptions", "-O1"]
# Host (x86-64) budget with headroom; today's worst case is about 700 bytes.
STACK_BUDGET = 1024


@pytest.fixture(scope="module")
def stack_info(tmp_path_factory) -> tuple[dict[str, tuple[int, str]], dict[str, set[str]]]:
    if shutil.which("g++") is None:
        pytest.skip("g++ not available")
    out = tmp_path_factory.mktemp("stack")
    obj = out / "eye_protocol.o"
    build = subprocess.run(
        ["g++", *CXXFLAGS, "-fstack-usage", "-fcallgraph-info=su", f"-I{LIB_SRC}",
         "-c", str(SOURCE), "-o", str(obj)],
        capture_output=True,
        text=True,
        cwd=out,
    )
    assert build.returncode == 0, f"compile failed:\n{build.stdout}\n{build.stderr}"

    ci = out / "eye_protocol.ci"
    assert ci.exists(), "compiler did not write a call graph (-fcallgraph-info needs GCC 10+)"
    text = ci.read_text()

    frames: dict[str, tuple[int, str]] = {}
    for m in re.finditer(r'node:\s*\{\s*title:\s*"([^"]+)"\s*label:\s*"([^"]*)"', text):
        title, label = m.group(1), m.group(2)
        su = re.search(r"\\n(\d+) bytes \((\w+)\)", label)
        if su:  # defined in this file (external functions have no frame info)
            frames[title] = (int(su.group(1)), su.group(2))

    calls: dict[str, set[str]] = {}
    for m in re.finditer(r'edge:\s*\{\s*sourcename:\s*"([^"]+)"\s*targetname:\s*"([^"]+)"', text):
        calls.setdefault(m.group(1), set()).add(m.group(2))
    return frames, calls


def _find(frames: dict, name: str) -> str:
    matches = [t for t in frames if name in t]
    assert matches, f"{name} not in call graph: {sorted(frames)}"
    return matches[0]


def test_all_frames_static(stack_info):
    frames, _ = stack_info
    assert frames, "no stack usage parsed"
    dynamic = {f: q for f, (_, q) in frames.items() if q != "static"}
    assert not dynamic, f"functions with input-dependent stack: {dynamic}"


def test_no_recursion(stack_info):
    _, calls = stack_info
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in calls.get(node, ()):
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt):] + [nxt] if nxt in path else [node, nxt]
                pytest.fail(f"recursion in eye_protocol.cpp: {' -> '.join(cycle)}")
            if nxt not in state:
                visit(nxt, path + [nxt])
        state[node] = 2

    for node in list(calls):
        if node not in state:
            visit(node, [node])


def test_parse_command_worst_case_stack(stack_info):
    frames, calls = stack_info
    root = _find(frames, "parse_command")

    def worst(node: str, path: tuple[str, ...] = ()) -> int:
        assert node not in path, "recursion (see test_no_recursion)"
        own = frames.get(node, (0, "static"))[0]
        return own + max((worst(c, path + (node,)) for c in calls.get(node, ())), default=0)

    total = worst(root)
    assert 0 < total <= STACK_BUDGET, f"parse_command worst-case stack {total} bytes"
