# eyes_esp32

Firmware for the ESP32 that drives the six eye servos. The PlatformIO project comes in a later issue.

## Eye controller core

`lib/eye_core/` holds the motion logic (look mapping, blink, fail-safe, speed limiting) as plain
C++17 with no Arduino dependency. Limits are injected via `EyeConfig`. Host tests live in
`tests/native/test_eye_controller.cpp` and run through `pytest` (`tests/test_eye_core_native.py`).

## Servo limits

`include/limits_generated.h` is generated from `config/limits.yaml`. Do not edit it by hand.

When a human changes `config/limits.yaml`, regenerate the header and commit it in the same change:

```sh
python -m brain.limits_header
```

CI (`tests/test_limits_header.py`) fails until the committed header matches the YAML.
