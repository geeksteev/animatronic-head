// Hardware-independent eye motion logic. Plain C++17: no Arduino, no heap, no exceptions.
// Time is always passed in as a millisecond counter (e.g. millis()); uint32_t wraparound is handled.
// Rules follow docs/eye-protocol.md.
#ifndef EYE_CORE_EYE_CONTROLLER_H
#define EYE_CORE_EYE_CONTROLLER_H

#include <stdint.h>

struct ServoCfg {
    float min_deg, max_deg, neutral_deg, max_speed_dps;
};

struct EyeConfig {
    ServoCfg pan, tilt, lids[4];  // lids: upper_left, lower_left, upper_right, lower_right
    bool lid_closed_at_max[4];    // which end of the range is 'closed' for each lid
    uint32_t command_timeout_ms;
};

class EyeController {
public:
    static constexpr int kServoCount = 6;
    static constexpr int kPan = 0;
    static constexpr int kTilt = 1;
    static constexpr int kFirstLid = 2;
    static constexpr uint16_t kBlinkMinMs = 50;
    static constexpr uint16_t kBlinkMaxMs = 1000;

    explicit EyeController(const EyeConfig& cfg);

    // Normalized gaze in [-1, 1] (clamped). Non-finite x or y: the call is ignored entirely
    // (not a valid command, does not reset the fail-safe timer).
    void look(float x, float y, uint32_t now_ms);
    // Close all lids, reopen after ms (clamped to [50, 1000]).
    void blink(uint16_t ms, uint32_t now_ms);
    // Pan/tilt to neutral, lids open (cancels any blink).
    void center(uint32_t now_ms);
    // Heartbeat, no motion.
    void ping(uint32_t now_ms);
    // Set one servo's target (0=pan, 1=tilt, 2..5=lids), clamped to its [min, max]. A lid
    // cancels any blink; the other servos keep their current targets. An invalid idx or a
    // non-finite deg is ignored entirely (does not reset the fail-safe timer).
    void set_servo(int idx, float deg, uint32_t now_ms);

    // Apply fail-safe and blink timing, then rate-limit each servo toward its target.
    void update(uint32_t now_ms);

    // 0=pan, 1=tilt, 2..5=lids. Always within [min, max] of that servo.
    // Returns NaN for an invalid index.
    float angle(int idx) const;
    // True from construction until the first valid command, and whenever the command timeout
    // has expired (latched until the next valid command).
    bool in_failsafe() const { return failsafe_; }

private:
    const ServoCfg& cfg_for(int idx) const;
    void record_command(uint32_t now_ms);
    void set_all_targets_neutral();
    void set_lid_targets_open();

    EyeConfig cfg_;
    float current_[kServoCount];
    float target_[kServoCount];

    bool have_cmd_ = false;  // no command yet -> fail-safe
    uint32_t last_cmd_ms_ = 0;
    bool failsafe_ = true;

    bool have_update_ = false;
    uint32_t last_update_ms_ = 0;

    bool blinking_ = false;
    uint32_t blink_start_ms_ = 0;
    uint16_t blink_ms_ = 0;
};

#endif  // EYE_CORE_EYE_CONTROLLER_H
