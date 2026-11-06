# Eye client (brain side)

`brain/eye_client.py` sends eye commands (`docs/eye-protocol.md`) to the head over UDP.
Every datagram is signed with the shared eye key (see
[Authentication](eye-protocol.md#authentication)).

## Key

```sh
cp config/secrets.yaml.example config/secrets.yaml   # gitignored
# set eye_key to the same 64 hex characters as EYE_AUTH_KEY_HEX in the head's secrets.h
```

`EYES_KEY=<64 hex characters>` in the environment overrides the file. The client and the CLI
refuse to start without a valid key. You can also pass `key=<32 bytes>` to `EyeClient`.

## Python

```python
import os
from brain.eye_client import EyeClient

with EyeClient(os.environ["EYES_HOST"]) as eyes:  # port defaults to 4210, limits to config/limits.yaml
    eyes.start()                         # heartbeat: ping when idle for command_timeout_ms / 3
    eyes.look(0.25, -0.5)                # x, y in [-1, 1]; out-of-range values are clamped, NaN raises
    eyes.blink()                         # 150 ms by default
    eyes.center()
    eyes.servo("lid_upper_left", 95)     # one servo, by its name in the limits
```

### `servo(name, deg)`

Moves one servo to the absolute angle `deg`. `name` is the servo's name in
`config/limits.yaml` (`eye_pan`, `lid_lower_right`, ...). Sends a
[`servo`](eye-protocol.md#servo) command and returns its `seq`.

- The channel is `limits[name].channel`. It must match the protocol order: 0 pan, 1 tilt,
  then lids 2 to 5 (upper_left, lower_left, upper_right, lower_right).
- `deg` is clamped to that servo's `[min, max]` with `limits.clamp(name, deg)` before sending.
  The head clamps it again and rate-limits it to `max_speed_dps`.
- An unknown name raises `brain.limits.UnknownServoError`. NaN or infinity raises
  `ProtocolError` (a `ValueError`). Nothing is sent in either case.
- The client's limits are available as `EyeClient.limits`.
- There is no CLI subcommand for it.

- The client refuses to start if `failsafe.command_timeout_ms` is missing from the limits.
- Every command carries an incrementing `seq`, starting at the current Unix time in
  microseconds, so a restarted client passes the head's replay check. It never wraps.
- Sends are serialized with a lock, so one client can be shared between threads.
- `close()` (or leaving the `with` block) stops the heartbeat and closes the socket. It does
  **not** send `center`: the head's fail-safe returns the servos to neutral once commands stop.

## CLI

```sh
export EYES_HOST=<head hostname or address>   # or pass --host every time
python -m brain.eye_client look 0.25 -0.5
python -m brain.eye_client blink 300
python -m brain.eye_client center
python -m brain.eye_client ping
python -m brain.eye_client --hold 5 look -1 0   # keep the heartbeat going for 5 s, then exit
```

The CLI exits non-zero if neither `--host` nor `EYES_HOST` is set. No hostname is built in.
Without `--hold`, the head goes back to neutral `command_timeout_ms` after the command.
