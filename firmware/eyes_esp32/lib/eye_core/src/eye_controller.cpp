#include "eye_controller.h"

#include <math.h>

namespace {

float clampf(float v, float lo, float hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

float clamp_to(const ServoCfg& c, float v) { return clampf(v, c.min_deg, c.max_deg); }

// docs/eye-protocol.md "Angle mapping".
float map_axis(const ServoCfg& c, float v) {
    v = clampf(v, -1.0f, 1.0f);
    float a = (v >= 0.0f) ? c.neutral_deg + v * (c.max_deg - c.neutral_deg)
                          : c.neutral_deg + v * (c.neutral_deg - c.min_deg);
    return clamp_to(c, a);
}

}  // namespace

EyeController::EyeController(const EyeConfig& cfg) : cfg_(cfg) {
    for (int i = 0; i < kServoCount; ++i) {
        const ServoCfg& c = cfg_for(i);
        current_[i] = clamp_to(c, c.neutral_deg);
        target_[i] = current_[i];
    }
}

const ServoCfg& EyeController::cfg_for(int idx) const {
    if (idx == kPan) return cfg_.pan;
    if (idx == kTilt) return cfg_.tilt;
    return cfg_.lids[idx - kFirstLid];
}

void EyeController::record_command(uint32_t now_ms) {
    have_cmd_ = true;
    last_cmd_ms_ = now_ms;
    failsafe_ = false;
}

void EyeController::set_lid_targets_open() {
    for (int i = kFirstLid; i < kServoCount; ++i) {
        const ServoCfg& c = cfg_for(i);
        target_[i] = clamp_to(c, c.neutral_deg);
    }
}

void EyeController::set_all_targets_neutral() {
    for (int i = 0; i < kServoCount; ++i) {
        const ServoCfg& c = cfg_for(i);
        target_[i] = clamp_to(c, c.neutral_deg);
    }
}

void EyeController::look(float x, float y, uint32_t now_ms) {
    if (!isfinite(x) || !isfinite(y)) return;
    target_[kPan] = map_axis(cfg_.pan, x);
    target_[kTilt] = map_axis(cfg_.tilt, y);
    record_command(now_ms);
}

void EyeController::blink(uint16_t ms, uint32_t now_ms) {
    if (ms < kBlinkMinMs) ms = kBlinkMinMs;
    if (ms > kBlinkMaxMs) ms = kBlinkMaxMs;
    blinking_ = true;
    blink_start_ms_ = now_ms;
    blink_ms_ = ms;
    for (int l = 0; l < 4; ++l) {
        const ServoCfg& c = cfg_.lids[l];
        target_[kFirstLid + l] = cfg_.lid_closed_at_max[l] ? c.max_deg : c.min_deg;
    }
    record_command(now_ms);
}

void EyeController::center(uint32_t now_ms) {
    blinking_ = false;
    set_all_targets_neutral();
    record_command(now_ms);
}

void EyeController::ping(uint32_t now_ms) { record_command(now_ms); }

void EyeController::set_servo(int idx, float deg, uint32_t now_ms) {
    if (idx < 0 || idx >= kServoCount || !isfinite(deg)) return;
    // Cancel the blink so update() does not reopen the lids over this target. The other lids
    // keep whatever target they had.
    if (idx >= kFirstLid) blinking_ = false;
    target_[idx] = clamp_to(cfg_for(idx), deg);
    record_command(now_ms);
}

void EyeController::update(uint32_t now_ms) {
    // Fail-safe. Unsigned subtraction handles millis() wraparound. Once entered, fail-safe
    // latches until the next valid command, so a silence longer than 2^32 ms cannot wrap
    // back into looking "fresh". record_command() clears the latch.
    if (!have_cmd_ || failsafe_ ||
        (uint32_t)(now_ms - last_cmd_ms_) >= cfg_.command_timeout_ms) {
        failsafe_ = true;
        blinking_ = false;
        set_all_targets_neutral();
    } else {
        if (blinking_ && (uint32_t)(now_ms - blink_start_ms_) >= blink_ms_) {
            blinking_ = false;
            set_lid_targets_open();
        }
    }

    uint32_t dt_ms = have_update_ ? (uint32_t)(now_ms - last_update_ms_) : 0;
    have_update_ = true;
    last_update_ms_ = now_ms;

    float dt_s = (float)dt_ms / 1000.0f;
    for (int i = 0; i < kServoCount; ++i) {
        const ServoCfg& c = cfg_for(i);
        float max_step = c.max_speed_dps * dt_s;
        if (!(max_step > 0.0f)) max_step = 0.0f;  // also catches NaN
        float tgt = clamp_to(c, target_[i]);
        float cur = current_[i];
        float diff = tgt - cur;
        if (diff > max_step) {
            cur += max_step;
        } else if (diff < -max_step) {
            cur -= max_step;
        } else {
            cur = tgt;
        }
        current_[i] = clamp_to(c, cur);
    }
}

float EyeController::angle(int idx) const {
    if (idx < 0 || idx >= kServoCount) return NAN;
    return current_[idx];
}
