# Eye client (brain side)

`brain/eye_client.py` sends eye commands (`docs/eye-protocol.md`) to the head over UDP.

## Python

```python
import os
from brain.eye_client import EyeClient

with EyeClient(os.environ["EYES_HOST"]) as eyes:  # port defaults to 4210, limits to config/limits.yaml
    eyes.start()                         # heartbeat: ping when idle for command_timeout_ms / 3
    eyes.look(0.25, -0.5)                # x, y in [-1, 1]; out-of-range values are clamped, NaN raises
    eyes.blink()                         # 150 ms by default
    eyes.center()
```

- The client refuses to start if `failsafe.command_timeout_ms` is missing from the limits.
- Every command carries an incrementing `seq` (for logging on the head).
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
