"""Firmware indexes EYE_SERVO_LIMITS by limits.names() position; the protocol
uses SERVO_PROTOCOL_CHANNELS. These must agree for the real config."""
from brain.eye_client import SERVO_PROTOCOL_CHANNELS
from brain.limits import load_limits


def test_limits_order_matches_protocol_channels():
    names = load_limits().names()
    assert names == list(SERVO_PROTOCOL_CHANNELS)


def test_protocol_channel_values_are_0_to_5_in_order():
    assert list(SERVO_PROTOCOL_CHANNELS.values()) == [0, 1, 2, 3, 4, 5]
