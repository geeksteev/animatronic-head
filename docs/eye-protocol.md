# Eye command protocol (v1)

The wire format the brain uses to send eye commands to the head. The brain-side codec is
`brain/eye_protocol.py`; the ESP32 firmware implements the same rules.

## Transport

- **UDP**, one command per datagram. Default port **4210**.
- Each datagram is at most **128 bytes**: a **payload** (one UTF-8 JSON object, at most
  **96 bytes**) immediately followed by a 32-character **tag** (see
  [Authentication](#authentication)). Larger datagrams are rejected without being parsed.
- Senders write compact JSON (no whitespace) with keys in a fixed order: `v`, `seq`, `cmd`,
  then the command's fields. Receivers must not depend on key order or whitespace, and ignore
  keys they do not know.

The JSON examples below show the payload only.

## Common fields

| Key   | Type    | Required | Meaning |
|-------|---------|----------|---------|
| `v`   | integer | yes      | Protocol version. Must be exactly `1`. |
| `seq` | integer | yes\*    | Sequence number, `0` to `2^53 - 1`. Used for replay protection. |
| `cmd` | string  | yes      | Command name, see below. Case-sensitive. |

\* The payload grammar allows `seq` to be absent, but the head drops any datagram without it
(see [Authentication](#authentication)).

The head does **not** reorder datagrams. It drops any datagram whose `seq` is not above the last
accepted one, so commands apply in send order and late ones are discarded.

## Authentication

Every datagram carries a shared-key authenticator, so only a sender holding the key can move the
eyes or feed the fail-safe.

```
datagram = payload || tag
tag      = lowercase hex of the first 16 bytes of HMAC-SHA256(key, payload)   (32 characters)
```

- **Key:** 32 bytes, written as 64 hex characters. The head gets it from `EYE_AUTH_KEY_HEX` in
  the gitignored `firmware/eyes_esp32/include/secrets.h`. The brain gets it from the `EYES_KEY`
  environment variable or `eye_key` in the gitignored `config/secrets.yaml`. Both have a
  committed `.example`. Generate a key with
  `python -c "import secrets; print(secrets.token_hex(32))"`. An all-zero key is refused.
- The tag covers the payload bytes exactly as sent. There is no separator: the tag is the last
  32 bytes of the datagram. Upper-case hex is rejected.
- **Replay window:** the head remembers the highest `seq` it has accepted and accepts a datagram
  only if its `seq` is strictly higher. Duplicates, replays and late datagrams are dropped.
- **Seq source:** the brain starts `seq` at the current Unix time in microseconds and adds 1 per
  datagram. A restarted brain therefore starts above everything it sent before, without
  persisting any state. `seq` never wraps.
- **Check order on the head:** size, tag (constant-time compare), payload, `seq` present, replay
  window. The replay window only advances when every check passes.

Limitations:

- The head forgets the window when it reboots. Until the brain's next datagram, a recorded
  datagram from an earlier session can be replayed once per `seq`.
- If the brain's clock steps backwards past the last `seq` the head accepted, the head drops its
  datagrams until the clock catches up or the head reboots.
- The payload is authenticated, not encrypted.

## Commands

### `look`

```json
{"v":1,"seq":7,"cmd":"look","x":0.25,"y":-0.5}
```

- `x`, `y` (number, required): normalized gaze in `[-1, 1]`. Out-of-range values are clamped.
- `+x` is the head's right, `+y` is up, `0,0` is neutral.
- The head maps them to pan/tilt angles using the limits (see [Angle mapping](#angle-mapping)).

### `blink`

```json
{"v":1,"seq":8,"cmd":"blink","ms":150}
```

- `ms` (number, optional, default **150**): close all lids for `ms` milliseconds, then reopen.
  Clamped to `[50, 1000]` and rounded to an integer.

### `center`

```json
{"v":1,"seq":9,"cmd":"center"}
```

Pan and tilt to neutral, lids open.

### `ping`

```json
{"v":1,"seq":10,"cmd":"ping"}
```

Heartbeat. No motion.

### `servo`

```json
{"v":1,"seq":11,"cmd":"servo","ch":2,"deg":95.5}
```

Moves a single servo. Meant for bench calibration, which steps one servo at a time, including
each lid on its own.

- `ch` (integer, required): servo index `0` to `5`, in the head's order: `0` pan, `1` tilt,
  `2` lid upper_left, `3` lid lower_left, `4` lid upper_right, `5` lid lower_right.
  - The datagram is **invalid** if `ch` is not an integer (`2.0`, `2e0`, `"2"`, `true`) or is
    out of range.
- `deg` (number, required): absolute angle in degrees. Non-finite values are invalid.
  - The codec clamps `deg` to `[0, 180]` and writes it in the usual 3-decimal format.
  - The head then clamps it to that servo's `[min, max]` from the limits and rate-limits it
    to `max_speed_dps`.

Semantics:

- Sets that servo's target only. Every other servo keeps its current target.
- On a lid (`ch` 2 to 5), it cancels an active `blink`. The other lids hold their current
  targets: if the blink closed them, they stay closed until another command moves them.
- A later `look` overrides pan and tilt. `center` resets everything.
- Like any valid command, it resets the fail-safe timer. When the fail-safe triggers, all
  servos still go to neutral.

## Fail-safe

- Every **valid** command, including `ping`, resets the head's fail-safe timer
  (`failsafe.command_timeout_ms` in `config/limits.yaml`).
- If the timer expires, all servos go to neutral.
- Unauthenticated, replayed, malformed or unknown datagrams are ignored and do **not** reset
  the timer. A datagram is invalid if any of these hold:
  - it is larger than 128 bytes, or its tag is missing or wrong;
  - `seq` is missing, or not above the last accepted `seq`;
  - the payload is larger than 96 bytes, not valid UTF-8, or not a JSON object;
  - `v` is missing or not the integer `1`;
  - `seq` is present but not an integer in `[0, 2^53 - 1]`;
  - `cmd` is missing or not one of `look`, `blink`, `center`, `ping`, `servo`;
  - `servo`'s `ch` is not an integer in `[0, 5]`;
  - a required field is missing, or any field is non-numeric (strings, booleans and `null`
    are not numbers);
  - any number is non-finite (`NaN`, `Infinity`, or a literal that overflows, e.g. `1e999`).

Out-of-range numbers other than `ch` are **not** invalid: they are clamped (`x`/`y` to
`[-1, 1]`, `ms` to `[50, 1000]`, `deg` to `[0, 180]`) and the command is accepted.

## Angle mapping

Done on the head, using each servo's `min`, `max`, `neutral` and `max_speed_dps` from
`config/limits.yaml`:

```
angle = neutral + x * (max - neutral)    if x >= 0
angle = neutral + x * (neutral - min)    if x < 0
```

Pan uses `x` with `eye_pan`; tilt uses `y` with `eye_tilt`. The result is then clamped to
`[min, max]` and rate-limited to `max_speed_dps`.

With the placeholder limits (`eye_pan`: min 60, neutral 90, max 120), `x = 0.25` gives
`90 + 0.25 * 30 = 97.5` degrees and `x = -0.5` gives `90 - 0.5 * 30 = 75` degrees.

## Number format

- Numbers are written with at most **3 decimal places** and **no exponent notation**
  (`0.25`, `-1`, `0`, never `2.5e-1` or `1e-4`).
- Trailing zeros are dropped and negative zero is written as `0`.
- `ms`, `seq` and `ch` are written as plain integers.

## Size bound

The longest possible payloads are 65 bytes, under the 96-byte payload limit:

- a `look` with the maximum `seq` and 3-decimal negative values, e.g.
  `{"v":1,"seq":9007199254740991,"cmd":"look","x":-0.999,"y":-0.999}`;
- a `servo` with the maximum `seq` and a 3-digit, 3-decimal angle, e.g.
  `{"v":1,"seq":9007199254740991,"cmd":"servo","ch":5,"deg":179.999}`.

With the tag they are 97 bytes, under the 128-byte datagram limit.
