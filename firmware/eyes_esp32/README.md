# eyes_esp32

Firmware for the ESP32 that drives the six eye servos. It listens for UDP commands
(`docs/eye-protocol.md`, port `EYE_UDP_PORT`), passes them to `eye_core`, and writes the resulting
angles to the servos. The head is I/O only: all motion logic lives in `lib/eye_core`.

## Flashing

```sh
pip install platformio
cp firmware/eyes_esp32/include/secrets.h.example firmware/eyes_esp32/include/secrets.h
# edit secrets.h: set WIFI_SSID and WIFI_PASSWORD
pio run -d firmware/eyes_esp32 -t upload
pio device monitor -b 115200
```

`include/secrets.h` is gitignored. Never commit it. Without it, the firmware still builds (with a
`#warning`) but skips Wi-Fi, so the servos just hold neutral. This is how CI builds it.

On serial you will see the IP address once Wi-Fi connects, plus `[failsafe] ENTER/EXIT` lines.
If no valid command arrives for `failsafe.command_timeout_ms`, or Wi-Fi drops, the servos return to
neutral.

## Bench checklist

Do this with the linkages **disconnected** first, then connected, moving slowly.

1. **Pins.** Check `include/pins.h` `SERVO_PINS` against your wiring. The values there are placeholders.
2. **Calibrate limits first.** For each servo, find the mechanical stops, back off about 5°, and
   have a human record min/max/neutral in `config/limits.yaml`. Then run `python -m brain.limits_header`
   and reflash. Until then, treat the placeholder limits as untrusted.
3. **Pulse range.** Confirm `SERVO_MIN_US`/`SERVO_MAX_US` (500–2500 µs for 0–180°) suit your TS90MDs.
4. **Lid closed direction.** Send `{"v":1,"seq":1,"cmd":"blink","ms":1000}` and watch each lid. If a lid opens
   instead of closing, flip its entry in `LID_CLOSED_AT_MAX` in `include/pins.h`.
5. **Fail-safe.** Send a `look` command, stop sending, and check that everything returns to neutral
   within about `command_timeout_ms`. Repeat with the Wi-Fi access point switched off.

## Eye controller core

`lib/eye_core/` holds the motion logic (look mapping, blink, fail-safe, speed limiting) as plain
C++17 with no Arduino dependency. Limits are injected via `EyeConfig`. Host tests live in
`tests/native/test_eye_controller.cpp` and run through `pytest` (`tests/test_eye_core_native.py`).

`lib/eye_core/src/eye_protocol.*` parses the UDP datagrams from `docs/eye-protocol.md`
(`parse_command`) and forwards them to the controller (`dispatch`). It is a small strict JSON
reader with no heap use. `tests/test_eye_protocol_cross.py` checks it against
`brain.eye_protocol.decode` on several hundred valid and malformed datagrams.

## Servo limits

`include/limits_generated.h` is generated from `config/limits.yaml`. Do not edit it by hand.

When a human changes `config/limits.yaml`, regenerate the header and commit it in the same change:

```sh
python -m brain.limits_header
```

CI (`tests/test_limits_header.py`) fails until the committed header matches the YAML.
