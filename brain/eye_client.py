"""Brain-side UDP client for the eye firmware (see ``docs/eye-client.md``).

``EyeClient`` sends one command per datagram, encoded with ``brain.eye_protocol``.
While started, a heartbeat thread sends ``ping`` whenever nothing has been sent
for ``failsafe.command_timeout_ms / 3``, so the head's fail-safe stays fed while
the brain is running. When the client stops, it sends nothing more: the head
returns to neutral by itself after the timeout.

CLI::

    python -m brain.eye_client [--host HOST] [--port N] [--hold SECONDS] \\
        look X Y | blink [MS] | center | ping

The host comes from ``--host`` or the ``EYES_HOST`` environment variable.
"""

from __future__ import annotations

import argparse
import math
import os
import socket
import sys
import threading
import time
from typing import Sequence

from brain import eye_protocol
from brain.eye_protocol import Blink, Center, Command, Look, Ping
from brain.limits import Limits, load_limits

HOST_ENV = "EYES_HOST"


class EyeClient:
    """Send eye commands to the head over UDP. Thread-safe."""

    def __init__(
        self,
        host: str,
        port: int = eye_protocol.DEFAULT_PORT,
        limits: Limits | None = None,
        sock: socket.socket | None = None,
    ) -> None:
        if not host:
            raise ValueError("host is required")
        if limits is None:
            limits = load_limits()
        timeout_ms = limits.command_timeout_ms
        if timeout_ms is None:
            raise ValueError("limits have no failsafe.command_timeout_ms; refusing to run")
        if timeout_ms <= 0:
            raise ValueError(f"failsafe.command_timeout_ms must be positive, got {timeout_ms}")

        self.command_timeout_ms = timeout_ms
        self.heartbeat_interval = timeout_ms / 3 / 1000.0  # seconds

        if sock is None:
            family, _, _, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
            sock = socket.socket(family, socket.SOCK_DGRAM)
            self._addr = addr
        else:
            self._addr = (host, port)
        self._sock = sock

        self._lock = threading.Lock()
        self._seq = 0
        self._last_send = time.monotonic()
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- commands ---------------------------------------------------------

    def look(self, x: float, y: float) -> int:
        """Look at ``(x, y)`` in ``[-1, 1]`` (clamped by the codec). Returns the seq."""
        return self.send(Look(x, y))

    def blink(self, ms: int = eye_protocol.BLINK_DEFAULT_MS) -> int:
        return self.send(Blink(ms))

    def center(self) -> int:
        return self.send(Center())

    def ping(self) -> int:
        return self.send(Ping())

    def send(self, cmd: Command) -> int:
        """Encode and send ``cmd`` with the next seq. Returns the seq used.

        Raises ``eye_protocol.ProtocolError`` (a ValueError) for NaN/inf or
        bad fields, before anything is sent, and RuntimeError after close().
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("EyeClient is closed")
            seq = self._seq
            data = eye_protocol.encode(cmd, seq=seq)
            self._sock.sendto(data, self._addr)
            self._seq = 0 if seq >= eye_protocol.SEQ_MAX else seq + 1
            self._last_send = time.monotonic()
            return seq

    # -- heartbeat --------------------------------------------------------

    def start(self) -> None:
        """Start the heartbeat thread. Calling it again while running does nothing."""
        with self._lock:
            if self._closed:
                raise RuntimeError("EyeClient is closed")
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._heartbeat, name="eye-heartbeat", daemon=True
            )
            self._thread.start()

    def _heartbeat(self) -> None:
        while True:
            with self._lock:
                due = self._last_send + self.heartbeat_interval
            wait = due - time.monotonic()
            if wait > 0:
                if self._stop.wait(wait):
                    return
                continue  # re-check: a command may have been sent meanwhile
            if self._stop.is_set():
                return
            try:
                self.ping()
            except RuntimeError:
                return  # closed
            except OSError as e:
                # Network hiccup: keep trying. The head goes neutral if it persists.
                print(f"eye_client: heartbeat send failed: {e}", file=sys.stderr)
                with self._lock:
                    self._last_send = time.monotonic()

    def close(self) -> None:
        """Stop the heartbeat and close the socket. Does not send ``center``."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._sock.close()

    def __enter__(self) -> EyeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# -- CLI -------------------------------------------------------------------


def _finite_nonneg(text: str) -> float:
    try:
        f = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(f) or f < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return f


def _port(text: str) -> int:
    try:
        p = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if not 1 <= p <= 65535:
        raise argparse.ArgumentTypeError("must be in 1..65535")
    return p


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m brain.eye_client", description="Send one eye command to the head."
    )
    p.add_argument("--host", help=f"head hostname or address (default: ${HOST_ENV})")
    p.add_argument(
        "--port", type=_port, default=eye_protocol.DEFAULT_PORT, help="UDP port (default: %(default)s)"
    )
    p.add_argument(
        "--hold",
        type=_finite_nonneg,
        default=0.0,
        metavar="SECONDS",
        help="keep the heartbeat running this long after the command",
    )
    sub = p.add_subparsers(dest="command", required=True)
    look = sub.add_parser("look", help="look at X Y in [-1, 1]")
    look.add_argument("x", type=float)
    look.add_argument("y", type=float)
    blink = sub.add_parser("blink", help="blink for MS milliseconds")
    blink.add_argument("ms", type=float, nargs="?", default=eye_protocol.BLINK_DEFAULT_MS)
    sub.add_parser("center", help="return the eyes to center")
    sub.add_parser("ping", help="feed the fail-safe without moving")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    host = args.host or os.environ.get(HOST_ENV)
    if not host:
        parser.error(f"no host: pass --host or set {HOST_ENV}")

    try:
        client = EyeClient(host, args.port)
    except (ValueError, OSError) as e:
        print(f"eye_client: {e}", file=sys.stderr)
        return 1

    with client:
        try:
            if args.command == "look":
                client.look(args.x, args.y)
            elif args.command == "blink":
                client.blink(args.ms)
            elif args.command == "center":
                client.center()
            else:
                client.ping()
            if args.hold > 0:
                client.start()
                time.sleep(args.hold)
        except KeyboardInterrupt:
            pass
        except (ValueError, OSError) as e:
            print(f"eye_client: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
