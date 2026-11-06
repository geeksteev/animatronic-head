"""Tests for eye datagram authentication (brain.eye_protocol sign/verify, ReplayGuard)."""

import hashlib
import hmac

import pytest

from brain import eye_protocol as P
from brain.eye_protocol import (
    AuthError,
    Blink,
    Look,
    Ping,
    ProtocolError,
    ReplayGuard,
    decode_signed,
    encode,
    encode_signed,
    parse_key,
    sign,
    verify,
)

KEY = bytes(range(1, 33))
OTHER_KEY = bytes(range(2, 34))


def test_tag_is_truncated_hmac_sha256_hex():
    payload = b'{"v":1,"seq":7,"cmd":"ping"}'
    want = hmac.new(KEY, payload, hashlib.sha256).hexdigest()[:32].encode()
    assert P.tag(payload, KEY) == want
    assert sign(payload, KEY) == payload + want


def test_round_trip():
    data = encode_signed(Look(0.25, -0.5), 7, KEY)
    assert data.startswith(b'{"v":1,"seq":7,"cmd":"look","x":0.25,"y":-0.5}')
    assert len(data) == len(encode(Look(0.25, -0.5), 7)) + P.TAG_LEN
    assert decode_signed(data, KEY) == (7, Look(0.25, -0.5))


def test_worst_case_signed_datagram_fits():
    assert len(encode_signed(Look(-0.999, -0.999), P.SEQ_MAX, KEY)) <= P.MAX_DATAGRAM


def test_rejects_wrong_key():
    with pytest.raises(AuthError):
        decode_signed(encode_signed(Ping(), 1, OTHER_KEY), KEY)


def test_rejects_unsigned_payload():
    with pytest.raises(AuthError):
        decode_signed(encode(Ping(), 1), KEY)


def test_rejects_any_single_byte_change():
    data = encode_signed(Blink(400), 3, KEY)
    for i in range(len(data)):
        bad = bytearray(data)
        bad[i] ^= 0x01
        with pytest.raises(AuthError):
            verify(bytes(bad), KEY)


def test_rejects_uppercase_tag():
    data = encode_signed(Ping(), 1, KEY)
    payload, t = data[:-P.TAG_LEN], data[-P.TAG_LEN:]
    assert t != t.upper()  # 128 random bits: almost surely has a letter; fixed key, so it does
    with pytest.raises(AuthError):
        verify(payload + t.upper(), KEY)


@pytest.mark.parametrize("cut", [1, P.TAG_LEN, P.TAG_LEN + 1])
def test_rejects_truncated(cut):
    data = encode_signed(Ping(), 1, KEY)
    with pytest.raises(AuthError):
        verify(data[:-cut], KEY)


def test_rejects_short_and_oversize():
    with pytest.raises(AuthError):
        verify(b"", KEY)
    with pytest.raises(AuthError):
        verify(b"0" * (P.TAG_LEN - 1), KEY)
    payload = b'{"v":1,"seq":1,"cmd":"ping"}'
    big = payload + b" " * (P.MAX_PAYLOAD + 1 - len(payload))
    with pytest.raises(AuthError):
        verify(big + P.tag(big, KEY), KEY)  # tag is right, but the datagram is too long


def test_seq_required():
    with pytest.raises(ProtocolError):
        encode_signed(Ping(), None, KEY)
    with pytest.raises(AuthError):
        decode_signed(sign(encode(Ping()), KEY), KEY)


def test_valid_tag_on_invalid_payload_is_protocol_error():
    with pytest.raises(ProtocolError):
        decode_signed(sign(b'{"v":2,"seq":1,"cmd":"ping"}', KEY), KEY)


@pytest.mark.parametrize("key", [b"", b"x" * 31, b"x" * 33, "k" * 32])
def test_bad_key_length(key):
    with pytest.raises(ValueError):
        P.tag(b"", key)
    with pytest.raises(ValueError):
        verify(b"0" * 40, key)


def test_parse_key():
    assert parse_key(KEY.hex()) == KEY
    assert parse_key(KEY.hex().upper()) == KEY
    assert parse_key(" " + KEY.hex() + "\n") == KEY
    for bad in ["", KEY.hex()[:-1], KEY.hex() + "0", "g" * 64, "replace-with-64-hex-characters",
                "00" * 32, None, 42]:
        with pytest.raises(ValueError):
            parse_key(bad)


def test_replay_guard():
    g = ReplayGuard()
    assert g.accept(0)
    assert not g.accept(0)  # duplicate
    assert g.accept(5)
    assert not g.accept(4)  # late / replayed
    assert not g.accept(5)
    assert g.accept(6)
    assert g.accept(P.SEQ_MAX)
    assert not g.accept(0)


def test_replay_guard_starts_anywhere():
    g = ReplayGuard()
    assert g.accept(1_700_000_000_000_000)
    assert not g.accept(1)
