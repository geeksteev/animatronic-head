"""Cross-check the C++ eye_protocol parser and eye_auth against brain.eye_protocol.

Builds tests/native/parse_stdin.cpp with the eye_core sources, feeds it one hex-encoded
payload (or signed datagram) per line, and checks every printed result against the Python
decoder (or decode_signed plus ReplayGuard).
"""

import itertools
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from brain import eye_protocol as P
from brain.eye_protocol import (
    Blink,
    Center,
    Look,
    Ping,
    ProtocolError,
    ReplayGuard,
    decode,
    decode_signed,
    encode,
    sign,
)

ROOT = Path(__file__).resolve().parent.parent
LIB_SRC = ROOT / "firmware" / "eyes_esp32" / "lib" / "eye_core" / "src"
HEADER = LIB_SRC / "eye_protocol.h"
HARNESS = ROOT / "tests" / "native" / "parse_stdin.cpp"
AUTH_HEADER = LIB_SRC / "eye_auth.h"
KEY = bytes(range(1, 33))

CXXFLAGS = ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-fno-exceptions", "-O1"]
TOL = 0.001


def _define(name: str, header: Path = HEADER) -> int:
    m = re.search(rf"^#define\s+{name}\s+(\d+)\b", header.read_text(), re.MULTILINE)
    assert m, f"{name} not defined in {header.name}"
    return int(m.group(1))


def test_port_matches_python():
    assert _define("EYE_UDP_PORT") == P.DEFAULT_PORT


def test_max_datagram_matches_python():
    assert _define("EYE_MAX_DATAGRAM") == P.MAX_DATAGRAM


def test_auth_sizes_match_python():
    assert _define("EYE_AUTH_TAG_LEN") == P.TAG_LEN
    assert _define("EYE_MAX_PAYLOAD") == P.MAX_PAYLOAD
    assert _define("EYE_AUTH_KEY_LEN", AUTH_HEADER) == P.KEY_LEN


def test_parser_has_no_heap_or_arduino():
    for name in ("eye_protocol.h", "eye_protocol.cpp", "eye_auth.h", "eye_auth.cpp"):
        text = (LIB_SRC / name).read_text()
        code = re.sub(r"//.*", "", text)
        for bad in ("Arduino.h", "malloc", "calloc", "realloc", "free(", "new ", "delete ",
                    "<string>", "<vector>", "<map>", "std::", "throw", "try"):
            assert bad not in code, f"{name} uses {bad!r}"


# --- vectors ----------------------------------------------------------------

def _valid_vectors() -> list[bytes]:
    values = [0, 0.25, -0.5, 1, -1, 0.0004, -0.0004, 0.0005, 0.9995, -0.9995, 0.123456,
              1.5, -3, 1e6, -1e-9, 0.333, -0.001]
    out = []
    for x, y in itertools.product(values, repeat=2):
        out.append(encode(Look(x, y)))
    for seq in (None, 0, 1, 7, 2**31 - 1, 2**31, 2**53 - 1):
        out.append(encode(Look(0.25, -0.5), seq))
        out.append(encode(Blink(), seq))
        out.append(encode(Center(), seq))
        out.append(encode(Ping(), seq))
    for ms in (0, 1, 49, 50, 51, 149.5, 150, 150.5, 151.5, 400, 999, 999.5, 1000, 1001, 10**9, -5):
        out.append(encode(Blink(ms)))
    return out


def _handwritten_vectors() -> list[bytes]:
    """Valid and malformed datagrams the encoder never produces."""
    s = [
        # whitespace, order, formats the encoder does not use
        ' { "cmd" : "look" , "y" : -5e-1 , "x" : 2.5E-1 , "v" : 1 } ',
        '\n{"v":1,\r\n"cmd":\t"ping"}\n',
        '{"v":1,"cmd":"look","x":1e-999,"y":-0}',
        '{"v":1,"cmd":"look","x":-0.0,"y":1E+0}',
        '{"v":1,"cmd":"look","x":1e300,"y":-1e300}',
        '{"v":1,"cmd":"look","x":' + "9" * 80 + ',"y":0}',
        '{"v":1,"cmd":"blink","ms":150.49999}',
        '{"v":1,"cmd":"blink","ms":2.5e2}',
        '{"v":1,"cmd":"blink","ms":50.5}',
        '{"v":1,"cmd":"blink","ms":-0}',
        '{"v":1,"seq":-0,"cmd":"ping"}',
        '{"\\u0076":1,"cmd":"\\u0070ing"}',
        '{"v":1,"cmd":"ping","extra":{"a":[1,true,false,null,"\\"",{}],"b":[]}}',
        '{"v":1,"cmd":"ping","note":"h\\u00e9llo \\ud800"}',
        '{"v":1,"cmd":"center","x":"left","ms":null}',
        '{"v":1,"cmd":"look","x":"bad","x":0.5,"y":0}',
        '{"v":1,"cmd":"look","x":0.5,"y":0,"x":"bad"}',
        '{"v":2,"v":1,"cmd":"ping"}',
        '{"v":1,"cmd":"look","cmd":"blink"}',
        '{"v":1,"cmd":"ping","d":' + "[" * 45 + "]" * 45 + "}",
        '{"v":1,"cmd":"ping","d":' + '{"a":[' * 9 + "]}" * 9 + "}",
        '{"v":1,"cmd":"ping","d":[{"b":[{}]},[],{"c":{"d":[1,{"e":null}]}}],"x":2}',
        '{"v":1,"cmd":"ping","d":[ { "a" : [ ] , "b" : { } } , 1 ] }',
        # malformed nesting in a skipped value
        '{"v":1,"cmd":"ping","d":[}', '{"v":1,"cmd":"ping","d":{]}',
        '{"v":1,"cmd":"ping","d":[{]}', '{"v":1,"cmd":"ping","d":{"a":[}]}',
        '{"v":1,"cmd":"ping","d":[[]]]}', '{"v":1,"cmd":"ping","d":{"a":{}}}}',
        '{"v":1,"cmd":"ping","d":[1 2]}', '{"v":1,"cmd":"ping","d":{"a" 1}}',
        '{"v":1,"cmd":"ping","d":{1:2}}', '{"v":1,"cmd":"ping","d":{"a":1,}}',
        '{"v":1,"cmd":"ping","d":[,1]}', '{"v":1,"cmd":"ping","d":{"a"}}',
        '{"v":1,"cmd":"ping","d":[[],[]', '{"v":1,"cmd":"ping","d":{"a":[{"b":1}]',
        # malformed JSON
        "", " ", "{", "}", "[]", "[1]", "1", '"ping"', "null", "true",
        '{"v":1,"cmd":"ping"', '{"v":1,"cmd":"ping",}', '{"v":1,,"cmd":"ping"}',
        '{"v":1 "cmd":"ping"}', '{v:1,"cmd":"ping"}', "{'v':1,'cmd':'ping'}",
        '{"v":1,"cmd":"ping"}x', '{"v":1,"cmd":"ping"}{}', '{"v":1,"cmd":"ping","e":[1,]}',
        '{"v":1,"cmd":"ping","e":"\\x"}', '{"v":1,"cmd":"ping","e":"\\u12"}',
        '{"v":1,"cmd":"ping","e":"a\tb"}', '{"v":1,"cmd":"ping","e":tru}',
        '{"v":1,"cmd":"ping","e":nul}', '{"v":1,"cmd":"ping","d":' + "[" * 45 + "}",
        # version
        '{"cmd":"ping"}', '{"v":0,"cmd":"ping"}', '{"v":2,"cmd":"ping"}', '{"v":-1,"cmd":"ping"}',
        '{"v":1.0,"cmd":"ping"}', '{"v":1e0,"cmd":"ping"}', '{"v":"1","cmd":"ping"}',
        '{"v":true,"cmd":"ping"}', '{"v":null,"cmd":"ping"}', '{"v":01,"cmd":"ping"}',
        '{"v":1,"cmd":"ping","v":2}',
        # seq
        '{"v":1,"seq":-1,"cmd":"ping"}', '{"v":1,"seq":9007199254740992,"cmd":"ping"}',
        '{"v":1,"seq":18446744073709551616,"cmd":"ping"}', '{"v":1,"seq":2147483648,"cmd":"ping"}',
        '{"v":1,"seq":99999999999999999999,"cmd":"ping"}', '{"v":1,"seq":1.0,"cmd":"ping"}',
        '{"v":1,"seq":1e3,"cmd":"ping"}', '{"v":1,"seq":"7","cmd":"ping"}',
        '{"v":1,"seq":null,"cmd":"ping"}', '{"v":1,"seq":true,"cmd":"ping"}',
        # cmd
        '{"v":1}', '{"v":1,"cmd":"Ping"}', '{"v":1,"cmd":"wink"}', '{"v":1,"cmd":1}',
        '{"v":1,"cmd":null}', '{"v":1,"cmd":"pingpong"}', '{"v":1,"cmd":"pin"}',
        '{"v":1,"cmd":"ping "}', '{"v":1,"cmd":["ping"]}',
        # look fields
        '{"v":1,"cmd":"look","x":0.1}', '{"v":1,"cmd":"look","y":0.1}',
        '{"v":1,"cmd":"look"}', '{"v":1,"cmd":"look","x":"0.1","y":0}',
        '{"v":1,"cmd":"look","x":true,"y":0}', '{"v":1,"cmd":"look","x":null,"y":0}',
        '{"v":1,"cmd":"look","x":[0],"y":0}', '{"v":1,"cmd":"look","x":{},"y":0}',
        # numbers
        '{"v":1,"cmd":"look","x":NaN,"y":0}', '{"v":1,"cmd":"look","x":Infinity,"y":0}',
        '{"v":1,"cmd":"look","x":-Infinity,"y":0}', '{"v":1,"cmd":"look","x":1e999,"y":0}',
        '{"v":1,"cmd":"look","x":0,"y":-1e400}', '{"v":1,"cmd":"look","x":+1,"y":0}',
        '{"v":1,"cmd":"look","x":.5,"y":0}', '{"v":1,"cmd":"look","x":1.,"y":0}',
        '{"v":1,"cmd":"look","x":0x1,"y":0}', '{"v":1,"cmd":"look","x":1e,"y":0}',
        '{"v":1,"cmd":"look","x":01,"y":0}', '{"v":1,"cmd":"look","x":-,"y":0}',
        '{"v":1,"cmd":"look","x":1e+,"y":0}', '{"v":1,"cmd":"look","x":inf,"y":0}',
        # blink ms
        '{"v":1,"cmd":"blink","ms":"150"}', '{"v":1,"cmd":"blink","ms":null}',
        '{"v":1,"cmd":"blink","ms":false}', '{"v":1,"cmd":"blink","ms":1e999}',
        '{"v":1,"cmd":"blink","ms":[150]}',
    ]
    out = [t.encode("utf-8", "surrogatepass") for t in s]
    out += [
        b'{"v":1,"cmd":"ping","e":"\xff"}',              # invalid UTF-8
        b'{"v":1,"cmd":"ping","e":"\xc0\xaf"}',          # overlong
        b'{"v":1,"cmd":"ping","e":"\xed\xa0\x80"}',      # encoded surrogate
        b'{"v":1,"cmd":"ping","e":"\xf4\x90\x80\x80"}',  # above U+10FFFF
        b'{"v":1,"cmd":"ping","e":"\xe2\x82"}',          # truncated sequence
        b'{"v":1,"cmd":"ping","e":"\xe2\x82\xac"}',      # valid euro sign
        b'{"v":1,"cmd":"ping"}\x00',                     # trailing NUL
        b'\xef\xbb\xbf{"v":1,"cmd":"ping"}',             # BOM
    ]
    # Size boundary: exactly MAX_PAYLOAD is parsed, one more byte is rejected.
    head = b'{"v":1,"cmd":"ping","pad":"'
    for size in (P.MAX_PAYLOAD - 1, P.MAX_PAYLOAD, P.MAX_PAYLOAD + 1, P.MAX_DATAGRAM,
                 4 * P.MAX_DATAGRAM):
        out.append(head + b"a" * (size - len(head) - 2) + b'"}')
    return out


def _vectors() -> list[bytes]:
    vectors = _valid_vectors() + _handwritten_vectors()
    # Every prefix of a few valid datagrams is malformed (or, rarely, still valid).
    for d in (encode(Look(0.25, -0.5), 7), encode(Blink(400), 3)):
        vectors += [d[:i] for i in range(len(d))]
    return vectors


def _expected(data: bytes) -> tuple:
    try:
        cmd = decode(data)
    except ProtocolError:
        return ("invalid",)
    if isinstance(cmd, Look):
        return ("look", cmd.x, cmd.y)
    if isinstance(cmd, Blink):
        return ("blink", cmd.ms)
    if isinstance(cmd, Center):
        return ("center",)
    assert isinstance(cmd, Ping)
    return ("ping",)


def _matches(line: str, expected: tuple) -> bool:
    parts = line.split()
    if not parts or parts[0] != expected[0]:
        return False
    if expected[0] == "look":
        if len(parts) != 3:
            return False
        x, y = float(parts[1]), float(parts[2])
        return (math.isclose(x, expected[1], abs_tol=TOL)
                and math.isclose(y, expected[2], abs_tol=TOL))
    if expected[0] == "blink":
        return len(parts) == 2 and int(parts[1]) == expected[1]
    return len(parts) == 1


@pytest.fixture(scope="module")
def parser_bin(tmp_path_factory) -> Path:
    if shutil.which("g++") is None:
        pytest.skip("g++ not available")
    sources = sorted(str(p) for p in LIB_SRC.glob("*.cpp"))
    binary = tmp_path_factory.mktemp("eye_protocol") / "parse_stdin"
    build = subprocess.run(
        ["g++", *CXXFLAGS, f"-I{LIB_SRC}", *sources, str(HARNESS), "-o", str(binary), "-lm"],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"compile failed:\n{build.stdout}\n{build.stderr}"
    return binary


def test_vectors_cover_both_outcomes():
    results = [_expected(d)[0] for d in _vectors()]
    for kind in ("look", "blink", "center", "ping", "invalid"):
        assert results.count(kind) >= 5, kind


def test_cpp_matches_python_decoder(parser_bin: Path):
    vectors = _vectors()
    stdin = "".join(d.hex() + "\n" for d in vectors)
    run = subprocess.run([str(parser_bin)], input=stdin, capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert len(lines) == len(vectors)

    mismatches = []
    for data, line in zip(vectors, lines):
        expected = _expected(data)
        if not _matches(line, expected):
            mismatches.append(f"{data!r}: C++ {line!r}, Python {expected!r}")
    assert not mismatches, "\n".join(mismatches)


# --- authenticated datagrams ------------------------------------------------

def _auth_vectors() -> list[bytes]:
    """A stream of datagrams as the head would see it: genuine, forged, replayed and late."""
    other = bytes(range(2, 34))
    out: list[bytes] = []
    seq = 0
    for payload in _vectors():
        if len(payload) > P.MAX_PAYLOAD:
            out.append(payload)
            continue
        seq += 3
        if payload.startswith(b"{"):
            # Give it a fresh seq (an existing "seq" later in the payload wins, as in json).
            with_seq = b'{"seq":%d,' % seq + payload[1:]
            if len(with_seq) <= P.MAX_PAYLOAD:
                out.append(sign(with_seq, KEY))
                out.append(sign(b'{"seq":%d,' % (seq - 1) + payload[1:], KEY))  # late
        out.append(payload)                      # unsigned
        out.append(sign(payload, other))         # wrong key
        signed = sign(payload, KEY)
        out.append(signed)                       # payload as-is (seq may be absent)
        if signed[-1:] != b"0":
            out.append(signed[:-1] + b"0")       # tag tampered
        out.append(signed[:-1])                  # truncated
        out.append(signed)                       # replay
    # Seq-ordered stream with replays and late datagrams.
    for s in [10**15, 10**15 + 1, 10**15, 10**15 - 1, 10**15 + 5, 10**15 + 2, P.SEQ_MAX, 0,
              P.SEQ_MAX]:
        out.append(sign(encode(Ping(), s), KEY))
    out.append(b"0" * P.TAG_LEN)
    out.append(b"")
    out.append(b"x" * (P.MAX_DATAGRAM + 1))
    return out


def _expected_auth(stream: list[bytes]) -> list[tuple]:
    guard = ReplayGuard()
    out = []
    for data in stream:
        try:
            seq, cmd = decode_signed(data, KEY)
        except ProtocolError:
            out.append(("invalid",))
            continue
        if not guard.accept(seq):
            out.append(("invalid",))
            continue
        out.append((seq, *_expected(encode(cmd))))
    return out


def test_auth_vectors_cover_both_outcomes():
    results = _expected_auth(_auth_vectors())
    accepted = [r for r in results if r[0] != "invalid"]
    assert len(accepted) >= 50
    assert len(results) - len(accepted) >= 200
    for kind in ("look", "blink", "center", "ping"):
        assert any(r[1] == kind for r in accepted), kind


def test_cpp_open_command_matches_python(parser_bin: Path):
    stream = _auth_vectors()
    stdin = "".join(d.hex() + "\n" for d in stream)
    run = subprocess.run([str(parser_bin), KEY.hex()], input=stdin, capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert len(lines) == len(stream)

    mismatches = []
    for data, line, expected in zip(stream, lines, _expected_auth(stream)):
        if expected[0] == "invalid":
            ok = line == "invalid"
        else:
            seq, _, rest = line.partition(" ")
            ok = seq == str(expected[0]) and _matches(rest, expected[1:])
        if not ok:
            mismatches.append(f"{data!r}: C++ {line!r}, Python {expected!r}")
    assert not mismatches, "\n".join(mismatches[:20])
