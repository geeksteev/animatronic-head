# animatronic-head

An AI-driven animatronic head. Code here is written by a builder agent, reviewed by a reviewer
agent, and merged automatically when CI and the review pass.

## Architecture
- **The head is I/O only.** Microcontrollers in the head drive servos, LEDs, mic and speaker,
  and accept commands over the network. No AI or decision logic runs on the head.
- **The brain runs off-board** (a server on the LAN). It talks to an LLM, turns replies into
  speech and motion, and sends commands to the head.
- Swapping the brain (API model, local model, prompts) must never require reflashing the head.

## Hardware (current)
- Eye mechanism: 6x TS90MD micro digital servos (eye pan, eye tilt, 4 eyelids).
- Controller: ESP32 for now; an nmrobots (Nilheim Mechatronics) control board is on order.
- Audio: ESP32-S3 with ICS-43434 I2S mic and MAX98357A I2S amp (proven wiring, separate project).

## Layout (create as needed)
- `firmware/<board>/`: PlatformIO projects (`platformio.ini` per project). CI builds every one it finds.
- `brain/`: Python package for the off-board brain.
- `tests/`: pytest, runs in CI. Anything testable without hardware gets tested.
- `config/limits.yaml`: servo safety limits. **Human-owned, read-only for agents.**

## Rules
- Every servo command is clamped to `config/limits.yaml`. Firmware gets limits at build time
  (generated header) or from the brain at runtime; either way, clamp on the head too.
- Fail safe: if commands stop for `failsafe.command_timeout_ms`, all servos go to neutral.
- No secrets in the repo. Wi-Fi credentials and hostnames go in gitignored files with a committed `.example`.
