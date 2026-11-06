"""Tests for brain.eye_client against a fake head (a UDP socket on loopback)."""

import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brain import eye_client, eye_protocol
from brain.eye_protocol import Blink, Center, Look, Ping, Servo
from brain.limits import UnknownServoError, load_limits

ROOT = Path(__file__).resolve().parent.parent
KEY = bytes(range(1, 33))

SERVOS = """
servos:
  eye_pan: { channel: 0, min: 60, max: 120, neutral: 90, max_speed_dps: 300 }
"""


def _limits(tmp_path: Path, failsafe: str):
    p = tmp_path / "limits.yaml"
    p.write_text(failsafe + SERVOS)
    return load_limits(p)


@pytest.fixture(autouse=True)
def eye_key(monkeypatch):
    """Every client in these tests gets KEY via $EYES_KEY unless a test overrides it."""
    monkeypatch.setenv(eye_client.KEY_ENV, KEY.hex())


@pytest.fixture
def head():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    yield s
    s.close()


def _recv(sock: socket.socket):
    seq, cmd = eye_protocol.decode_signed(sock.recv(1024), KEY)
    return cmd, seq


def _drain(sock: socket.socket, duration: float):
    """Collect every datagram that arrives within ``duration`` seconds."""
    out = []
    end = time.monotonic() + duration
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return out
        sock.settimeout(left)
        try:
            out.append(eye_protocol.decode_signed(sock.recv(1024), KEY)[1])
        except socket.timeout:
            return out


@pytest.fixture
def client(tmp_path, head):
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n")
    c = eye_client.EyeClient("127.0.0.1", head.getsockname()[1], limits=limits)
    yield c
    c.close()


def test_commands_arrive_with_increasing_seq(client, head):
    client.look(0.25, -0.5)
    client.blink()
    client.center()
    client.ping()
    got = [_recv(head) for _ in range(4)]
    assert [cmd for cmd, _ in got] == [Look(0.25, -0.5), Blink(150), Center(), Ping()]
    seqs = [seq for _, seq in got]
    assert seqs == sorted(seqs) and len(set(seqs)) == 4


def test_look_is_clamped(client, head):
    client.look(3, -3)
    assert _recv(head)[0] == Look(1, -1)


def test_nan_raises_and_sends_nothing(client, head):
    with pytest.raises(ValueError):
        client.look(float("nan"), 0)
    with pytest.raises(ValueError):
        client.blink(math.inf)
    assert _drain(head, 0.05) == []


class FakeSocket:
    """Records sendto() calls instead of sending."""

    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def close(self):
        pass


def _fake_client(limits, sock):
    return eye_client.EyeClient("head.invalid", limits=limits, sock=sock, key=KEY)


def test_servo_clamps_to_loaded_limits_with_fake_socket():
    limits = load_limits()  # the repo's config/limits.yaml
    sock = FakeSocket()
    with _fake_client(limits, sock) as c:
        assert c.limits is limits
        seq = c.servo("eye_pan", 500)
    assert len(sock.sent) == 1
    data, addr = sock.sent[0]
    assert addr == ("head.invalid", eye_protocol.DEFAULT_PORT)
    got_seq, cmd = eye_protocol.decode_signed(data, KEY)
    assert got_seq == seq
    assert cmd == Servo(limits["eye_pan"].channel, limits["eye_pan"].max)


def test_servo_uses_channel_and_min(tmp_path):
    p = tmp_path / "limits.yaml"
    p.write_text(
        "failsafe:\n  command_timeout_ms: 500\nservos:\n"
        "  lid_lower_right: { channel: 5, min: 80, max: 110, neutral: 90, max_speed_dps: 600 }\n"
    )
    sock = FakeSocket()
    with _fake_client(load_limits(p), sock) as c:
        c.servo("lid_lower_right", -20)
        c.servo("lid_lower_right", 95.5)
    cmds = [eye_protocol.decode_signed(d, KEY)[1] for d, _ in sock.sent]
    assert cmds == [Servo(5, 80), Servo(5, 95.5)]


def test_servo_over_udp(client, head):
    client.servo("eye_pan", 100)
    assert _recv(head)[0] == Servo(0, 100)


@pytest.mark.parametrize("deg", [math.nan, math.inf, -math.inf])
def test_servo_non_finite_raises_and_sends_nothing(tmp_path, deg):
    sock = FakeSocket()
    with _fake_client(_limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n"), sock) as c:
        with pytest.raises(ValueError):
            c.servo("eye_pan", deg)
    assert sock.sent == []


def test_servo_unknown_name_raises_and_sends_nothing(tmp_path):
    sock = FakeSocket()
    with _fake_client(_limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n"), sock) as c:
        with pytest.raises(UnknownServoError):
            c.servo("eye_roll", 90)
    assert sock.sent == []


def test_heartbeat_pings_when_idle_and_stops_on_close(tmp_path, head):
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 90\n")
    c = eye_client.EyeClient("127.0.0.1", head.getsockname()[1], limits=limits)
    c.start()
    got = _drain(head, 0.2)
    assert sum(1 for cmd in got if cmd == Ping()) >= 2
    c.close()
    _drain(head, 0.02)  # anything already in flight when close() was called
    assert _drain(head, 0.15) == []


def test_heartbeat_does_not_ping_while_commands_flow(tmp_path, head):
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 300\n")
    with eye_client.EyeClient("127.0.0.1", head.getsockname()[1], limits=limits) as c:
        c.start()
        for _ in range(8):
            c.look(0.1, 0.1)
            time.sleep(0.03)
        got = _drain(head, 0.02)
    assert Ping() not in got


def test_context_manager_closes_and_sends_no_center(tmp_path, head):
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n")
    with eye_client.EyeClient("127.0.0.1", head.getsockname()[1], limits=limits) as c:
        c.start()
        c.look(0.5, 0.5)
    assert _drain(head, 0.1) == [Look(0.5, 0.5)]
    with pytest.raises(RuntimeError):
        c.ping()
    c.close()  # idempotent


def test_missing_timeout_raises(tmp_path):
    limits = _limits(tmp_path, "")
    with pytest.raises(ValueError):
        eye_client.EyeClient("127.0.0.1", limits=limits)


def test_nonpositive_timeout_raises(tmp_path):
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 0\n")
    with pytest.raises(ValueError):
        eye_client.EyeClient("127.0.0.1", limits=limits)


def test_seq_never_wraps(client, head):
    client._seq = eye_protocol.SEQ_MAX
    client.ping()
    with pytest.raises(RuntimeError):
        client.ping()
    assert _recv(head)[1] == eye_protocol.SEQ_MAX
    assert _drain(head, 0.05) == []


def test_restarted_client_passes_head_replay_guard(tmp_path, head):
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n")
    guard = eye_protocol.ReplayGuard()
    for _ in range(3):
        with eye_client.EyeClient("127.0.0.1", head.getsockname()[1], limits=limits) as c:
            for _ in range(50):
                c.ping()
        seqs = [_recv(head)[1] for _ in range(50)]
        assert all(guard.accept(s) for s in seqs)


def test_explicit_key_is_used(tmp_path, head, monkeypatch):
    monkeypatch.delenv(eye_client.KEY_ENV)
    other = bytes(range(2, 34))
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n")
    with eye_client.EyeClient("127.0.0.1", head.getsockname()[1], limits=limits, key=other) as c:
        c.ping()
    data = head.recv(1024)
    assert eye_protocol.decode_signed(data, other)[1] == Ping()
    with pytest.raises(eye_protocol.AuthError):
        eye_protocol.decode_signed(data, KEY)


@pytest.mark.parametrize("key", [b"", b"x" * 31, b"x" * 33])
def test_bad_key_length_raises(tmp_path, key):
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n")
    with pytest.raises(ValueError):
        eye_client.EyeClient("127.0.0.1", limits=limits, key=key)


# --- key loading ------------------------------------------------------------

def test_load_key_from_env():
    assert eye_client.load_eye_key(Path("/nonexistent/secrets.yaml")) == KEY


def test_load_key_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv(eye_client.KEY_ENV)
    p = tmp_path / "secrets.yaml"
    p.write_text(f'eye_key: "{KEY.hex()}"\n')
    assert eye_client.load_eye_key(p) == KEY


def test_env_overrides_file(tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text(f'eye_key: "{bytes(range(2, 34)).hex()}"\n')
    assert eye_client.load_eye_key(p) == KEY


@pytest.mark.parametrize(
    "content",
    [None, "", "other: 1\n", "eye_key: 123\n", "- a\n", 'eye_key: "abc"\n'],
)
def test_load_key_missing_or_bad(tmp_path, monkeypatch, content):
    monkeypatch.delenv(eye_client.KEY_ENV)
    p = tmp_path / "secrets.yaml"
    if content is not None:
        p.write_text(content)
    with pytest.raises(ValueError):
        eye_client.load_eye_key(p)


def test_bad_env_key_raises(monkeypatch):
    monkeypatch.setenv(eye_client.KEY_ENV, "zz")
    with pytest.raises(ValueError, match=eye_client.KEY_ENV):
        eye_client.load_eye_key()


def test_example_secrets_is_a_placeholder_not_a_key(monkeypatch):
    monkeypatch.delenv(eye_client.KEY_ENV)
    example = ROOT / "config" / "secrets.yaml.example"
    with pytest.raises(ValueError):
        eye_client.load_eye_key(example)


def test_client_without_key_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv(eye_client.KEY_ENV)
    monkeypatch.setattr(eye_client, "SECRETS_PATH", tmp_path / "missing.yaml")
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 500\n")
    with pytest.raises(ValueError):
        eye_client.EyeClient("127.0.0.1", limits=limits)


def test_cli_without_key_exits_nonzero(monkeypatch, tmp_path, head, capsys):
    monkeypatch.delenv(eye_client.KEY_ENV)
    monkeypatch.setattr(eye_client, "SECRETS_PATH", tmp_path / "missing.yaml")
    port = str(head.getsockname()[1])
    assert eye_client.main(["--host", "127.0.0.1", "--port", port, "ping"]) == 1
    assert eye_client.KEY_ENV in capsys.readouterr().err
    assert _drain(head, 0.05) == []


def test_cli_without_host_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv(eye_client.HOST_ENV, raising=False)
    with pytest.raises(SystemExit) as e:
        eye_client.main(["ping"])
    assert e.value.code != 0
    assert eye_client.HOST_ENV in capsys.readouterr().err


def test_cli_subprocess_without_host_exits_nonzero():
    env = {k: v for k, v in os.environ.items() if k != eye_client.HOST_ENV}
    r = subprocess.run(
        [sys.executable, "-m", "brain.eye_client", "ping"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert r.returncode != 0


def test_cli_sends_command_via_env_host(monkeypatch, head):
    monkeypatch.setenv(eye_client.HOST_ENV, "127.0.0.1")
    port = str(head.getsockname()[1])
    assert eye_client.main(["--port", port, "look", "0.25", "-0.5"]) == 0
    assert _recv(head)[0] == Look(0.25, -0.5)
    assert eye_client.main(["--port", port, "blink", "200"]) == 0
    assert _recv(head)[0] == Blink(200)


def test_cli_host_flag_and_hold(monkeypatch, tmp_path, head):
    monkeypatch.delenv(eye_client.HOST_ENV, raising=False)
    limits = _limits(tmp_path, "failsafe:\n  command_timeout_ms: 90\n")
    monkeypatch.setattr(eye_client, "load_limits", lambda: limits)
    port = str(head.getsockname()[1])
    # command_timeout_ms 90 -> ping every 30 ms; hold 0.2 s gives several.
    assert eye_client.main(["--host", "127.0.0.1", "--port", port, "--hold", "0.2", "center"]) == 0
    got = _drain(head, 0.1)
    assert got[0] == Center()
    assert Ping() in got[1:]


def test_cli_nan_exits_nonzero(monkeypatch, head):
    port = str(head.getsockname()[1])
    assert eye_client.main(["--host", "127.0.0.1", "--port", port, "look", "nan", "0"]) == 1
