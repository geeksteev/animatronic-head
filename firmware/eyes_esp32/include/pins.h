// Board wiring and servo pulse calibration for the ESP32 eye controller.
// Angle limits are NOT here: they come from limits_generated.h (config/limits.yaml).
#ifndef EYES_ESP32_PINS_H
#define EYES_ESP32_PINS_H

#include <stdint.h>

#include "limits_generated.h"

// GPIO per servo index (order matches EYE_SERVO_LIMITS: pan, tilt, lid UL, LL, UR, LR).
// PLACEHOLDERS: confirm on the bench. Chosen to avoid strapping, flash and input-only pins.
static const uint8_t SERVO_PINS[EYE_SERVO_COUNT] = {
    13,  // eye_pan          (placeholder, confirm on bench)
    14,  // eye_tilt         (placeholder, confirm on bench)
    25,  // lid_upper_left   (placeholder, confirm on bench)
    26,  // lid_lower_left   (placeholder, confirm on bench)
    27,  // lid_upper_right  (placeholder, confirm on bench)
    32,  // lid_lower_right  (placeholder, confirm on bench)
};

// TS90MD pulse range: SERVO_MIN_US maps to 0 deg, SERVO_MAX_US to SERVO_RANGE_DEG.
// This is the electrical mapping only; the safe angle range is in config/limits.yaml.
#define SERVO_MIN_US 500
#define SERVO_MAX_US 2500
#define SERVO_RANGE_DEG 180.0f

// For each lid (upper_left, lower_left, upper_right, lower_right): true if the lid is closed at
// the max end of its range, false if closed at min. Calibrate on bench.
static const bool LID_CLOSED_AT_MAX[4] = {true, false, true, false};

#endif  // EYES_ESP32_PINS_H
