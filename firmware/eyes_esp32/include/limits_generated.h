// GENERATED from config/limits.yaml by brain/limits_header.py. Do not edit.
#ifndef EYE_LIMITS_GENERATED_H
#define EYE_LIMITS_GENERATED_H

#include <stdint.h>

#define EYE_LIMITS_COMMAND_TIMEOUT_MS 500
#define EYE_LIMITS_NEUTRAL_ON_BOOT 1

#define EYE_SERVO_COUNT 6
#define EYE_SERVO_EYE_PAN 0
#define EYE_SERVO_EYE_TILT 1
#define EYE_SERVO_LID_UPPER_LEFT 2
#define EYE_SERVO_LID_LOWER_LEFT 3
#define EYE_SERVO_LID_UPPER_RIGHT 4
#define EYE_SERVO_LID_LOWER_RIGHT 5

struct EyeServoLimit {
    const char* name;
    uint8_t channel;
    float min_deg;
    float max_deg;
    float neutral_deg;
    float max_speed_dps;
};

static const EyeServoLimit EYE_SERVO_LIMITS[EYE_SERVO_COUNT] = {
    {"eye_pan", 0, 60.0f, 120.0f, 90.0f, 300.0f},
    {"eye_tilt", 1, 70.0f, 110.0f, 90.0f, 300.0f},
    {"lid_upper_left", 2, 80.0f, 110.0f, 90.0f, 600.0f},
    {"lid_lower_left", 3, 80.0f, 110.0f, 90.0f, 600.0f},
    {"lid_upper_right", 4, 80.0f, 110.0f, 90.0f, 600.0f},
    {"lid_lower_right", 5, 80.0f, 110.0f, 90.0f, 600.0f},
};

#endif  // EYE_LIMITS_GENERATED_H
