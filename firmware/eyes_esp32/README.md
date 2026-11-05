# eyes_esp32

Firmware for the ESP32 that drives the six eye servos. The PlatformIO project comes in a later issue.

## Servo limits

`include/limits_generated.h` is generated from `config/limits.yaml`. Do not edit it by hand.

When a human changes `config/limits.yaml`, regenerate the header and commit it in the same change:

```sh
python -m brain.limits_header
```

CI (`tests/test_limits_header.py`) fails until the committed header matches the YAML.
