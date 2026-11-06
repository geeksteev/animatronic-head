import json
import subprocess
import sys

import pytest

from bench.instruments import extech_383273 as dmm
from bench.instruments.extech_383273 import (
    POLL,
    RESERVED,
    FrameError,
    decode,
    read_once,
)


def frame(code, word):
    return bytes([0x02, code, word & 0xFF, word >> 8, 0x03])


def _word(digits, negative=False, places=0):
    """Build a digits word the same way the meter does (4-bit fields MSB-first)."""
    def put(v, start, width):
        w = 0
        for i in range(width):
            if v >> (width - 1 - i) & 1:
                w |= 1 << (start + i)
        return w
    b14, b15 = {0: (0, 0), 1: (0, 1), 2: (1, 0), 3: (1, 1)}[places]
    return (int(negative) | (digits[0] & 1) << 1 | put(digits[1], 2, 4) | put(digits[2], 6, 4)
            | put(digits[3], 10, 4) | b14 << 14 | b15 << 15)


class FakeSerial:
    """Minimal stand-in for a pyserial port: replays queued bytes, records writes."""

    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.written = bytearray()
        self.closed = False
        self.resets = 0

    def reset_input_buffer(self):
        self.resets += 1

    def write(self, b):
        self.written += b
        return len(b)

    def read(self, n=1):
        out, self.data = bytes(self.data[:n]), self.data[n:]
        return out

    def close(self):
        self.closed = True


# --- decode -------------------------------------------------------------------------------

def test_manual_example():
    r = decode(bytes.fromhex("02 0C 21 B1 03"))
    assert r.display == "-012.3"
    assert r.value == pytest.approx(-12.3)
    assert r.raw == "02 0c 21 b1 03"


def test_word_builder_matches_manual_example():
    assert _word([0, 1, 2, 3], negative=True, places=1) == 0xB121


def test_dcv_2v_positive():
    r = decode(frame(0x01, _word([1, 5, 0, 7], places=3)))
    assert (r.function, r.unit, r.value, r.display) == ("DCV 2V", "V", 1.507, "1.507")


def test_dca_milliamps():
    r = decode(frame(0x22, _word([0, 1, 8, 4], places=1)))
    assert r.unit == "mA" and r.value == pytest.approx(18.4)


@pytest.mark.parametrize("places,display,value", [
    (0, "1999", 1999), (1, "199.9", 199.9), (2, "19.99", 19.99), (3, "1.999", 1.999),
])
def test_decimal_point_positions(places, display, value):
    r = decode(frame(0x01, _word([1, 9, 9, 9], places=places)))
    assert r.display == display and r.value == pytest.approx(value)


def test_frequency_unit_from_polarity_bit():
    mhz = decode(frame(0x05, _word([0, 0, 6, 0], negative=True)))
    assert mhz.unit == "MHz" and mhz.value == 60 and not mhz.display.startswith("-")
    assert decode(frame(0x05, _word([0, 0, 6, 0]))).unit == "kHz"


@pytest.mark.parametrize("code,unit", [
    (0x08, "Ω"), (0x30, "µA"), (0x42, "°C"), (0x84, "V"), (0x06, ""), (0xFF, ""),
])
def test_units(code, unit):
    assert decode(frame(code, 0)).unit == unit


def test_unknown_function_code():
    r = decode(frame(0x7E, _word([0, 0, 0, 1])))
    assert r.function == "UNKNOWN 0x7E" and r.value == 1


def test_overrange_digit_is_special():
    r = decode(frame(0x01, _word([0, 15, 15, 15])))
    assert r.value is None and r.display == "OL/special"


@pytest.mark.parametrize("pos", [1, 2, 3])
def test_any_digit_above_nine_is_special(pos):
    digits = [0, 0, 0, 0]
    digits[pos] = 10
    assert decode(frame(0x01, _word(digits))).value is None


def test_explicit_timestamp_is_kept_even_if_zero():
    assert decode(frame(0x01, 0), t=0.0).t == 0.0
    assert decode(frame(0x01, 0), t=123.5).t == 123.5


@pytest.mark.parametrize("bad", [
    b"",
    bytes.fromhex("02 01 00 00"),           # too short
    bytes.fromhex("02 01 00 00 03 03"),     # too long
    bytes.fromhex("01 01 00 00 03"),        # wrong start
    bytes.fromhex("02 01 00 00 04"),        # wrong end
])
def test_bad_frames(bad):
    with pytest.raises(FrameError):
        decode(bad)


# --- command bytes ------------------------------------------------------------------------

def test_poll_is_not_reserved():
    assert POLL == b" "
    assert not set(POLL) & RESERVED


def test_reserved_set_matches_manual():
    assert RESERVED == set(b"9421EGNRWUSTX")


def test_read_once_only_sends_poll():
    ser = FakeSerial(frame(0x01, _word([1, 0, 0, 0])))
    read_once(ser)
    assert bytes(ser.written) == POLL
    assert not set(ser.written) & RESERVED


# --- read_once ----------------------------------------------------------------------------

def test_read_once_clean_frame():
    ser = FakeSerial(bytes.fromhex("02 0C 21 B1 03"))
    assert read_once(ser).value == pytest.approx(-12.3)


def test_read_once_resyncs_after_partial_frame():
    # Tail of a previous frame, then a full frame.
    ser = FakeSerial(bytes.fromhex("21 b1 03") + bytes.fromhex("02 0C 21 B1 03"))
    r = read_once(ser)
    assert r.display == "-012.3"


def test_read_once_timeout_on_short_reply():
    with pytest.raises(TimeoutError):
        read_once(FakeSerial(b"\x02\x01"))


def test_read_once_lost_sync_when_stream_ends():
    with pytest.raises(FrameError):
        read_once(FakeSerial(b"\x55" * 6))


def test_read_once_lost_sync_is_bounded():
    ser = FakeSerial(b"\x55" * 1000)
    with pytest.raises(FrameError):
        read_once(ser)
    assert len(ser.data) > 900  # gave up quickly instead of draining the stream


def test_read_once_bad_end_byte_after_resync():
    with pytest.raises(FrameError):
        read_once(FakeSerial(bytes.fromhex("aa 02 01 00 00 aa")))


# --- CLI ----------------------------------------------------------------------------------

def test_module_works_without_pyserial():
    # Block `serial` entirely; importing, decoding and --help must still work.
    code = (
        "import sys; sys.modules['serial'] = None\n"
        "from bench.instruments import extech_383273 as d\n"
        "assert d.decode(bytes.fromhex('02 0C 21 B1 03')).display == '-012.3'\n"
    )
    root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    subprocess.run([sys.executable, "-c", code], check=True, cwd=root)
    help_out = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['serial'] = None; import runpy; "
         "sys.argv = ['x', '--help']; runpy.run_module('bench.instruments.extech_383273', "
         "run_name='__main__')"],
        cwd=root, capture_output=True, text=True,
    )
    assert help_out.returncode == 0 and "--jsonl" in help_out.stdout


def test_main_jsonl(monkeypatch, capsys):
    data = bytes.fromhex("02 0C 21 B1 03") + frame(0x22, _word([0, 1, 8, 4], places=1))
    ser = FakeSerial(data)
    monkeypatch.setattr(dmm, "open_port", lambda port: ser)
    monkeypatch.setattr(dmm.time, "sleep", lambda s: None)

    assert dmm.main(["/dev/null", "--jsonl", "--count", "2"]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    objs = [json.loads(line) for line in lines]
    for o in objs:
        assert set(o) == {"t", "function", "unit", "value", "display", "raw"}
    assert objs[0]["value"] == pytest.approx(-12.3) and objs[0]["function"] == "OHM 200kΩ"
    assert objs[1]["unit"] == "mA" and objs[1]["value"] == pytest.approx(18.4)
    assert ser.closed


def test_main_jsonl_reports_errors_on_stderr(monkeypatch, capsys):
    ser = FakeSerial(b"")
    monkeypatch.setattr(dmm, "open_port", lambda port: ser)
    monkeypatch.setattr(dmm.time, "sleep", lambda s: None)

    assert dmm.main(["/dev/null", "--jsonl", "--count", "1"]) == 0

    out = capsys.readouterr()
    assert out.out == ""
    assert "error" in json.loads(out.err.strip())
