# Eye command protocol (v1)

The wire format the brain uses to send eye commands to the head. The brain-side codec is
`brain/eye_protocol.py`; the ESP32 firmware implements the same rules.

## Transport

- **UDP**, one command per datagram. Default port **4210**.
- Each datagram is a single **UTF-8 JSON object** of at most **128 bytes**. Larger datagrams
  are rejected without being parsed.
- Senders write compact JSON (no whitespace) with keys in a fixed order: `v`, `seq`, `cmd`,
  then the command's fields. Receivers must not depend on key order or whitespace, and ignore
  keys they do not know.

## Common fields

| Key   | Type    | Required | Meaning |
|-------|---------|----------|---------|
| `v`   | integer | yes      | Protocol version. Must be exactly `1`. |
| `seq` | integer | no       | Sequence number, `0` to `2^31 - 1`. For logging only. |
| `cmd` | string  | yes      | Command name, see below. Case-sensitive. |

The head does **not** reorder or drop datagrams by `seq`. Commands apply in arrival order.

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

## Fail-safe

- Every **valid** command, including `ping`, resets the head's fail-safe timer
  (`failsafe.command_timeout_ms` in `config/limits.yaml`).
- If the timer expires, all servos go to neutral.
- Malformed or unknown datagrams are ignored and do **not** reset the timer. A datagram is
  invalid if any of these hold:
  - it is larger than 128 bytes, not valid UTF-8, or not a JSON object;
  - `v` is missing or not the integer `1`;
  - `seq` is present but not an integer in `[0, 2^31 - 1]`;
  - `cmd` is missing or not one of `look`, `blink`, `center`, `ping`;
  - a required field is missing, or any field is non-numeric (strings, booleans and `null`
    are not numbers);
  - any number is non-finite (`NaN`, `Infinity`, or a literal that overflows, e.g. `1e999`).

Out-of-range numbers are **not** invalid: they are clamped (`x`/`y` to `[-1, 1]`, `ms` to
`[50, 1000]`) and the command is accepted.

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
- `ms` and `seq` are written as plain integers.

## Size bound

The longest possible datagram is a `look` with the maximum `seq` and 3-decimal negative
values, e.g. `{"v":1,"seq":2147483647,"cmd":"look","x":-0.999,"y":-0.999}` (59 bytes),
well under the 128-byte limit.
