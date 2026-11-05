"""Tests for brain.eye_client against a fake head (a UDP socket on loopback)."""

import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brain import eye_client, eye_protocol
from brain.eye_protocol import Blink, Center, Look, Ping
from brain.limits import load_limits

ROOT = Path(__file__).resolve().parent.parent

SERVOS = """
servos:
  eye_pan: { channel: 0, min: 60, max: 120, neutral: 90, max_speed_dps: 300 }
"""


def _limits(tmp_path: Path, failsafe: str):
    p = tmp_path / "limits.yaml"
    p.write_text(failsafe + SERVOS)
    return load_limits(p)


@pytest.fixture
def head():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    yield s
    s.close()


def _recv(sock: socket.socket):
    data = sock.recv(1024)
    return eye_protocol.decode(data), json.loads(data)["seq"]


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
            out.append(eye_protocol.decode(sock.recv(1024)))
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


def test_seq_wraps(client, head):
    client._seq = eye_protocol.SEQ_MAX
    client.ping()
    client.ping()
    assert [_recv(head)[1] for _ in range(2)] == [eye_protocol.SEQ_MAX, 0]


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
