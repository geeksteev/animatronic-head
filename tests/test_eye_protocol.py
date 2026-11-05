import itertools
import json
import math

import pytest

from brain import eye_protocol as P
from brain.eye_protocol import Blink, Center, Look, Ping, ProtocolError, decode, encode


def test_constants():
    assert P.PROTOCOL_VERSION == 1
    assert P.DEFAULT_PORT == 4210
    assert P.MAX_DATAGRAM == 128
    assert (P.BLINK_MIN_MS, P.BLINK_MAX_MS, P.BLINK_DEFAULT_MS) == (50, 1000, 150)
    assert Blink().ms == 150
    assert issubclass(ProtocolError, ValueError)


# --- round trip -------------------------------------------------------------

@pytest.mark.parametrize(
    "cmd",
    [Look(0.25, -0.5), Look(0, 0), Look(-1, 1), Blink(), Blink(400), Center(), Ping()],
)
@pytest.mark.parametrize("seq", [None, 0, 7, 2**31 - 1])
def test_round_trip(cmd, seq):
    assert decode(encode(cmd, seq)) == cmd


def test_exact_wire_format_matches_spec_examples():
    assert encode(Look(0.25, -0.5), 7) == b'{"v":1,"seq":7,"cmd":"look","x":0.25,"y":-0.5}'
    assert encode(Blink(150), 8) == b'{"v":1,"seq":8,"cmd":"blink","ms":150}'
    assert encode(Center(), 9) == b'{"v":1,"seq":9,"cmd":"center"}'
    assert encode(Ping(), 10) == b'{"v":1,"seq":10,"cmd":"ping"}'
    assert encode(Ping()) == b'{"v":1,"cmd":"ping"}'


def test_key_order():
    keys = list(json.loads(encode(Look(0.1, 0.2), 3)))
    assert keys == ["v", "seq", "cmd", "x", "y"]


def test_decode_spec_examples():
    assert decode(b'{"v":1,"seq":7,"cmd":"look","x":0.25,"y":-0.5}') == Look(0.25, -0.5)
    assert decode(b'{"v":1,"seq":8,"cmd":"blink","ms":150}') == Blink(150)
    assert decode(b'{"v":1,"seq":9,"cmd":"center"}') == Center()
    assert decode(b'{"v":1,"seq":10,"cmd":"ping"}') == Ping()


def test_decode_seq_optional_and_extra_keys_ignored():
    assert decode(b'{"v":1,"cmd":"ping"}') == Ping()
    assert decode(b'{"cmd":"center","v":1,"extra":true}') == Center()


def test_blink_ms_defaults_when_missing():
    assert decode(b'{"v":1,"cmd":"blink"}') == Blink(150)


# --- number formatting ------------------------------------------------------

@pytest.mark.parametrize(
    "x, text",
    [
        (0, "0"), (-0.0, "0"), (1, "1"), (-1, "-1"), (0.5, "0.5"),
        (0.123456, "0.123"), (-0.0004, "0"), (1e-7, "0"), (0.9996, "1"),
    ],
)
def test_number_formatting(x, text):
    assert f'"x":{text},'.encode() in encode(Look(x, 0))


def test_no_exponent_and_at_most_three_decimals():
    for v in (1e-9, -1e-9, 1e-4, 0.00051, 123456.0, -1e300):
        body = encode(Look(v, v)).decode()
        assert "e" not in body.replace('"cmd":"look"', "").lower()
        for field in ("x", "y"):
            num = json.loads(body)[field]
            assert round(num, 3) == num


# --- clamping ---------------------------------------------------------------

@pytest.mark.parametrize("x, y, cx, cy", [(5, -7, 1, -1), (-1.5, 1.0001, -1, 1), (2, 0, 1, 0)])
def test_encode_clamps_xy(x, y, cx, cy):
    assert decode(encode(Look(x, y))) == Look(cx, cy)


def test_decode_clamps_xy():
    assert decode(b'{"v":1,"cmd":"look","x":3,"y":-99.5}') == Look(1, -1)


@pytest.mark.parametrize("ms, clamped", [(0, 50), (-10, 50), (49, 50), (50, 50), (1000, 1000), (5000, 1000)])
def test_encode_clamps_ms(ms, clamped):
    assert encode(Blink(ms)) == f'{{"v":1,"cmd":"blink","ms":{clamped}}}'.encode()


def test_decode_clamps_and_rounds_ms():
    assert decode(b'{"v":1,"cmd":"blink","ms":10}') == Blink(50)
    assert decode(b'{"v":1,"cmd":"blink","ms":99999}') == Blink(1000)
    assert decode(b'{"v":1,"cmd":"blink","ms":200.6}') == Blink(201)


# --- non-finite -------------------------------------------------------------

@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_encode_rejects_non_finite(bad):
    with pytest.raises(ProtocolError):
        encode(Look(bad, 0))
    with pytest.raises(ProtocolError):
        encode(Look(0, bad))
    with pytest.raises(ProtocolError):
        encode(Blink(bad))


@pytest.mark.parametrize(
    "data",
    [
        b'{"v":1,"cmd":"look","x":NaN,"y":0}',
        b'{"v":1,"cmd":"look","x":0,"y":Infinity}',
        b'{"v":1,"cmd":"look","x":-Infinity,"y":0}',
        b'{"v":1,"cmd":"look","x":1e999,"y":0}',
        b'{"v":1,"cmd":"blink","ms":NaN}',
        b'{"v":1,"cmd":"blink","ms":-1e999}',
    ],
)
def test_decode_rejects_non_finite(data):
    with pytest.raises(ProtocolError):
        decode(data)


# --- malformed input --------------------------------------------------------

def test_decode_rejects_oversize():
    pad = b" " * (P.MAX_DATAGRAM - len(b'{"v":1,"cmd":"ping"}'))
    assert decode(b'{"v":1,"cmd":"ping"}' + pad) == Ping()  # exactly 128 is fine
    with pytest.raises(ProtocolError):
        decode(b'{"v":1,"cmd":"ping"}' + pad + b" ")


@pytest.mark.parametrize(
    "data",
    [b"", b"{", b"not json", b'{"v":1,"cmd":"ping"', b"\xff\xfe", b"[1,2]", b'"ping"', b"null"],
)
def test_decode_rejects_bad_json(data):
    with pytest.raises(ProtocolError):
        decode(data)


@pytest.mark.parametrize(
    "data",
    [
        b'{"cmd":"ping"}',
        b'{"v":2,"cmd":"ping"}',
        b'{"v":0,"cmd":"ping"}',
        b'{"v":"1","cmd":"ping"}',
        b'{"v":1.0,"cmd":"ping"}',
        b'{"v":true,"cmd":"ping"}',
    ],
)
def test_decode_rejects_wrong_version(data):
    with pytest.raises(ProtocolError):
        decode(data)


@pytest.mark.parametrize(
    "data",
    [b'{"v":1,"cmd":"dance"}', b'{"v":1}', b'{"v":1,"cmd":null}', b'{"v":1,"cmd":"PING"}'],
)
def test_decode_rejects_unknown_cmd(data):
    with pytest.raises(ProtocolError):
        decode(data)


@pytest.mark.parametrize(
    "data",
    [
        b'{"v":1,"cmd":"look","x":0.1}',
        b'{"v":1,"cmd":"look","y":0.1}',
        b'{"v":1,"cmd":"look","x":"0.1","y":0}',
        b'{"v":1,"cmd":"look","x":true,"y":0}',
        b'{"v":1,"cmd":"look","x":null,"y":0}',
        b'{"v":1,"cmd":"blink","ms":"150"}',
        b'{"v":1,"cmd":"blink","ms":[150]}',
    ],
)
def test_decode_rejects_missing_or_non_numeric(data):
    with pytest.raises(ProtocolError):
        decode(data)


@pytest.mark.parametrize(
    "data",
    [
        b'{"v":1,"seq":-1,"cmd":"ping"}',
        b'{"v":1,"seq":2147483648,"cmd":"ping"}',
        b'{"v":1,"seq":1.5,"cmd":"ping"}',
        b'{"v":1,"seq":"7","cmd":"ping"}',
    ],
)
def test_decode_rejects_bad_seq(data):
    with pytest.raises(ProtocolError):
        decode(data)


@pytest.mark.parametrize("seq", [-1, 2**31, 1.0, "7", True])
def test_encode_rejects_bad_seq(seq):
    with pytest.raises(ProtocolError):
        encode(Ping(), seq)


@pytest.mark.parametrize("cmd", [object(), None, "ping", Look("0.1", 0), Blink(None)])
def test_encode_rejects_bad_command(cmd):
    with pytest.raises(ProtocolError):
        encode(cmd)


# --- size bound -------------------------------------------------------------

WORST_VALUES = [-1, 1, 0, -0.999, -0.001, 0.123, -0.5, -1e300, 1e300]
WORST_SEQS = [None, 0, 2**31 - 1]


def test_every_encoded_datagram_fits():
    cmds = [Look(x, y) for x, y in itertools.product(WORST_VALUES, repeat=2)]
    cmds += [Blink(ms) for ms in (0, 50, 999, 1000, 10**9)] + [Center(), Ping()]
    for cmd, seq in itertools.product(cmds, WORST_SEQS):
        assert len(encode(cmd, seq)) <= P.MAX_DATAGRAM


def test_worst_case_look_size():
    data = encode(Look(-0.999, -0.999), 2**31 - 1)
    assert len(data) <= P.MAX_DATAGRAM
    assert len(encode(Look(-1, -1), 2**31 - 1)) <= P.MAX_DATAGRAM
