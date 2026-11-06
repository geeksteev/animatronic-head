# animatronic-head

An AI-driven animatronic head.

**Status:** early. The eye mechanism works end to end: a Python client on the brain sends UDP
commands, and ESP32 firmware drives six servos with a fail-safe. The LLM, speech and audio parts
are not built yet.

## How it works

- **The head is I/O only.** Microcontrollers in the head drive servos, LEDs, mic and speaker and
  take commands over the network. No AI or decision logic runs on the head.
- **The brain runs off-board**, on a server on the LAN. It talks to an LLM, turns replies into
  speech and motion, and sends commands to the head.
- You can swap the brain (API model, local model, prompts) without reflashing the head.

```
brain (Python, LAN server) ──UDP, JSON──▶ head (ESP32) ──PWM──▶ 6 eye servos
```

## Hardware

- Eyes: 6x TS90MD micro digital servos (eye pan, eye tilt, 4 eyelids).
- Controller: ESP32 for now. An nmrobots (Nilheim Mechatronics) control board is on order.
- Audio (planned): ESP32-S3 with ICS-43434 I2S mic and MAX98357A I2S amp.

## Repository layout

| Path | What it is |
|------|------------|
| `firmware/eyes_esp32/` | PlatformIO project for the eye ESP32. See its [README](firmware/eyes_esp32/README.md). |
| `firmware/eyes_esp32/lib/eye_core/` | Hardware-independent C++ motion logic and protocol parser, tested on the host. |
| `brain/` | Python package for the off-board brain: limits loader, protocol codec, eye client. |
| `config/limits.yaml` | Servo safety limits. **Human-owned**: agents and PRs must not change it. |
| `docs/` | [Eye protocol](docs/eye-protocol.md) and [eye client](docs/eye-client.md) docs. |
| `tests/` | pytest suite, including host builds of the C++ core. |

## Safety

- **Every servo command is clamped** to `config/limits.yaml`. The brain sends normalized values
  (clamped to `[-1, 1]`), and the head maps them to angles, clamps them to each servo's
  `min`/`max` and limits the speed to `max_speed_dps`. The firmware gets the limits at build time
  from the generated header `firmware/eyes_esp32/include/limits_generated.h`.
- **Fail-safe:** if no valid command arrives for `failsafe.command_timeout_ms`, or Wi-Fi drops,
  all servos go back to neutral. The brain client sends a heartbeat `ping` while it runs.
- The limits in the repo are **placeholders**. Calibrate every servo on the bench before you
  trust them. Follow the [bench checklist](firmware/eyes_esp32/README.md#bench-checklist).

After a human edits `config/limits.yaml`, regenerate the firmware header and commit both together:

```sh
python -m brain.limits_header
```

## Quick start

### Brain (Python 3.10+)

```sh
pip install -e .
export EYES_HOST=<head hostname or address>
cp config/secrets.yaml.example config/secrets.yaml   # set eye_key (or export EYES_KEY)
python -m brain.eye_client look 0.25 -0.5
python -m brain.eye_client blink 300
python -m brain.eye_client --hold 5 look -1 0   # keep the heartbeat going for 5 s
```

See [docs/eye-client.md](docs/eye-client.md) for the Python API.

### Firmware

```sh
pip install platformio
cp firmware/eyes_esp32/include/secrets.h.example firmware/eyes_esp32/include/secrets.h
# edit secrets.h: set WIFI_SSID, WIFI_PASSWORD and EYE_AUTH_KEY_HEX (same key as the brain)
pio run -d firmware/eyes_esp32 -t upload
```

## Secrets

No credentials or hostnames go in the repo. Wi-Fi settings live in gitignored files
(`secrets.h`, `secrets.yaml`, `*.local.*`) with a committed `.example` template. The head's
address is passed to the brain with `--host` or `EYES_HOST`.

Eye commands are authenticated with a shared key (HMAC-SHA256 plus a replay check, see
[Authentication](docs/eye-protocol.md#authentication)), so other devices on the LAN cannot move
the eyes or keep the fail-safe from engaging. The key goes in `EYE_AUTH_KEY_HEX` in the head's
`secrets.h`, and in `eye_key` in `config/secrets.yaml` (or `EYES_KEY`) on the brain.

## Development

CI (`.github/workflows/ci.yml`) runs the same steps you can run locally:

```sh
# build every PlatformIO project
pio run -d firmware/eyes_esp32

# Python and host-side C++ tests (the C++ tests are skipped if g++ is missing)
pip install pytest pyyaml
pip install -e .
pytest -q
```

Code here is mostly written by a builder agent, reviewed by a reviewer agent, and merged
automatically when CI and the review pass. See [CLAUDE.md](CLAUDE.md) for the rules they follow.
