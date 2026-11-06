// Host tests for firmware/eyes_esp32/lib/eye_core. Built and run by tests/test_eye_core_native.py.
#include <math.h>
#include <stdint.h>
#include <stdio.h>

#include "eye_controller.h"

static int g_failures = 0;
static int g_checks = 0;

#define CHECK(cond)                                                          \
    do {                                                                     \
        ++g_checks;                                                          \
        if (!(cond)) {                                                       \
            ++g_failures;                                                    \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, \
                    #cond);                                                  \
        }                                                                    \
    } while (0)

#define CHECK_NEAR(a, b, eps)                                                       \
    do {                                                                            \
        ++g_checks;                                                                 \
        double _a = (a), _b = (b);                                                  \
        if (!(fabs(_a - _b) <= (eps))) {                                            \
            ++g_failures;                                                           \
            fprintf(stderr, "%s:%d: CHECK_NEAR failed: %s=%g vs %s=%g\n", __FILE__, \
                    __LINE__, #a, _a, #b, _b);                                      \
        }                                                                           \
    } while (0)

static const float EPS = 1e-3f;

// Asymmetric ranges so mapping bugs show up; lids mix closed-at-max and closed-at-min.
static EyeConfig make_cfg() {
    EyeConfig c{};
    c.pan = {60.0f, 120.0f, 90.0f, 300.0f};
    c.tilt = {70.0f, 115.0f, 95.0f, 200.0f};
    c.lids[0] = {80.0f, 110.0f, 90.0f, 600.0f};
    c.lids[1] = {75.0f, 100.0f, 85.0f, 500.0f};
    c.lids[2] = {80.0f, 110.0f, 100.0f, 600.0f};
    c.lids[3] = {70.0f, 105.0f, 95.0f, 400.0f};
    c.lid_closed_at_max[0] = true;
    c.lid_closed_at_max[1] = false;
    c.lid_closed_at_max[2] = false;
    c.lid_closed_at_max[3] = true;
    c.command_timeout_ms = 500;
    return c;
}

static const ServoCfg& servo(const EyeConfig& c, int i) {
    if (i == 0) return c.pan;
    if (i == 1) return c.tilt;
    return c.lids[i - 2];
}

// Run update() every step_ms for total_ms, pinging to keep the fail-safe away.
static uint32_t run(EyeController& e, uint32_t t, uint32_t total_ms, uint32_t step_ms = 10,
                    bool keepalive = true) {
    for (uint32_t el = 0; el < total_ms; el += step_ms) {
        t += step_ms;
        if (keepalive) e.ping(t);
        e.update(t);
    }
    return t;
}

static void test_starts_neutral() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    for (int i = 0; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).neutral_deg, EPS);
    CHECK(isnan(e.angle(-1)));
    CHECK(isnan(e.angle(6)));
}

static void test_look_extremes_and_clamp() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 1000;
    e.look(1.0f, 1.0f, t);
    e.update(t);
    t = run(e, t, 1000);
    CHECK_NEAR(e.angle(0), c.pan.max_deg, EPS);
    CHECK_NEAR(e.angle(1), c.tilt.max_deg, EPS);

    e.look(-1.0f, -1.0f, t);
    t = run(e, t, 1000);
    CHECK_NEAR(e.angle(0), c.pan.min_deg, EPS);
    CHECK_NEAR(e.angle(1), c.tilt.min_deg, EPS);

    e.look(5.0f, -5.0f, t);
    t = run(e, t, 1000);
    CHECK_NEAR(e.angle(0), c.pan.max_deg, EPS);
    CHECK_NEAR(e.angle(1), c.tilt.min_deg, EPS);

    // Doc formula examples (asymmetric tilt: neutral 95, max 115, min 70).
    e.look(0.25f, 0.5f, t);
    t = run(e, t, 1000);
    CHECK_NEAR(e.angle(0), 97.5, EPS);
    CHECK_NEAR(e.angle(1), 105.0, EPS);
    e.look(-0.5f, -0.4f, t);
    t = run(e, t, 1000);
    CHECK_NEAR(e.angle(0), 75.0, EPS);
    CHECK_NEAR(e.angle(1), 85.0, EPS);

    // Look does not move the lids.
    for (int i = 2; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).neutral_deg, EPS);
}

static void test_look_nan_ignored() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 0;
    e.look(0.5f, 0.5f, t);
    t = run(e, t, 1000);
    float pan = e.angle(0), tilt = e.angle(1);
    e.look(NAN, 0.0f, t);
    e.look(0.0f, NAN, t);
    t = run(e, t, 500);
    CHECK_NEAR(e.angle(0), pan, EPS);
    CHECK_NEAR(e.angle(1), tilt, EPS);

    // A NaN look does not count as a command: it must not hold off the fail-safe.
    EyeController f(c);
    uint32_t u = 0;
    f.ping(u);
    f.update(u);
    for (int i = 0; i < 100; ++i) {
        u += 10;
        f.look(NAN, NAN, u);
        f.update(u);
    }
    CHECK(f.in_failsafe());
}

static void test_speed_limit() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 5000;
    e.ping(t);
    e.update(t);
    float before[6];
    for (int i = 0; i < 6; ++i) before[i] = e.angle(i);
    e.look(1.0f, -1.0f, t);
    e.blink(200, t);
    t += 10;
    e.update(t);
    for (int i = 0; i < 6; ++i) {
        float max_step = servo(c, i).max_speed_dps * 0.01f;
        CHECK(fabsf(e.angle(i) - before[i]) <= max_step + EPS);
    }
    // And it actually moved at full speed on pan (target is 30 deg away, step is 3 deg).
    CHECK_NEAR(e.angle(0), 93.0, EPS);
}

static void test_blink() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 0;
    e.ping(t);
    e.update(t);
    e.blink(300, t);
    // Closing: each lid moves toward its closed end.
    t += 10;
    e.update(t);
    for (int l = 0; l < 4; ++l) {
        const ServoCfg& s = c.lids[l];
        if (c.lid_closed_at_max[l]) {
            CHECK(e.angle(2 + l) > s.neutral_deg);
        } else {
            CHECK(e.angle(2 + l) < s.neutral_deg);
        }
    }
    // Fully closed before ms elapses.
    t = run(e, t, 200);
    for (int l = 0; l < 4; ++l) {
        const ServoCfg& s = c.lids[l];
        CHECK_NEAR(e.angle(2 + l), c.lid_closed_at_max[l] ? s.max_deg : s.min_deg, EPS);
    }
    // Still closed just before ms (t = 290).
    t = run(e, t, 80);
    for (int l = 0; l < 4; ++l) {
        const ServoCfg& s = c.lids[l];
        CHECK_NEAR(e.angle(2 + l), c.lid_closed_at_max[l] ? s.max_deg : s.min_deg, EPS);
    }
    // After ms, lids return to neutral (open).
    t = run(e, t, 300);
    for (int l = 0; l < 4; ++l) CHECK_NEAR(e.angle(2 + l), c.lids[l].neutral_deg, EPS);
    CHECK(!e.in_failsafe());
}

static void test_blink_ms_clamped() {
    EyeConfig c = make_cfg();
    // Short: ms=1 is treated as 50, so lids are still closing/closed at t=40.
    {
        EyeController e(c);
        uint32_t t = 0;
        e.ping(t);
        e.update(t);
        e.blink(1, t);
        t = run(e, t, 40);
        CHECK(e.angle(2) > c.lids[0].neutral_deg + 1.0f);  // lid 0 closes at max
        t = run(e, t, 200);
        CHECK_NEAR(e.angle(2), c.lids[0].neutral_deg, EPS);
    }
    // Long: ms=60000 is treated as 1000.
    {
        EyeController e(c);
        uint32_t t = 0;
        e.ping(t);
        e.update(t);
        e.blink(60000, t);
        t = run(e, t, 990);
        CHECK_NEAR(e.angle(2), c.lids[0].max_deg, EPS);
        t = run(e, t, 200);
        CHECK_NEAR(e.angle(2), c.lids[0].neutral_deg, EPS);
    }
}

static void test_center() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 0;
    e.look(1.0f, 1.0f, t);
    e.blink(1000, t);
    t = run(e, t, 300);
    e.center(t);
    t = run(e, t, 500);
    for (int i = 0; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).neutral_deg, EPS);
}

static void test_failsafe() {
    EyeConfig c = make_cfg();
    // Starts in fail-safe until a command arrives.
    {
        EyeController e(c);
        CHECK(e.in_failsafe());
        e.update(0);
        CHECK(e.in_failsafe());
        e.ping(1);
        CHECK(!e.in_failsafe());
        e.update(2);
        CHECK(!e.in_failsafe());
    }
    // Timeout -> fail-safe, angles converge to neutral, blink cancelled.
    {
        EyeController e(c);
        uint32_t t = 100;
        e.look(1.0f, -1.0f, t);
        e.update(t);
        t = run(e, t, 400);  // keepalive pings
        CHECK(!e.in_failsafe());
        CHECK_NEAR(e.angle(0), c.pan.max_deg, EPS);
        uint32_t last = t;
        e.blink(1000, t);
        t = run(e, t, 490, 10, false);  // 490 ms silence: not yet
        CHECK(!e.in_failsafe());
        t += 10;  // exactly command_timeout_ms
        e.update(t);
        CHECK(t - last == c.command_timeout_ms);
        CHECK(e.in_failsafe());
        t = run(e, t, 1000, 10, false);
        CHECK(e.in_failsafe());
        for (int i = 0; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).neutral_deg, EPS);
        // A new command leaves fail-safe and is applied.
        e.look(1.0f, 0.0f, t);
        CHECK(!e.in_failsafe());
        t = run(e, t, 500);
        CHECK(!e.in_failsafe());
        CHECK_NEAR(e.angle(0), c.pan.max_deg, EPS);
    }
    // Pings just before the timeout keep it away indefinitely.
    {
        EyeController e(c);
        uint32_t t = 0;
        e.look(-1.0f, 1.0f, t);
        for (int k = 0; k < 20; ++k) {
            for (int s = 0; s < 49; ++s) {
                t += 10;
                e.update(t);
            }
            CHECK(!e.in_failsafe());
            e.ping(t);  // 490 ms after previous command
        }
        CHECK(!e.in_failsafe());
        CHECK_NEAR(e.angle(0), c.pan.min_deg, EPS);
        CHECK_NEAR(e.angle(1), c.tilt.max_deg, EPS);
    }
}

static void test_wraparound() {
    EyeConfig c = make_cfg();
    // Command just before wrap, updates after wrap: no spurious fail-safe, motion continues.
    {
        EyeController e(c);
        uint32_t t = UINT32_MAX - 200;
        e.look(1.0f, 1.0f, t);
        e.update(t);
        for (int k = 0; k < 40; ++k) {  // crosses 0 at about k=20
            t += 10;
            e.ping(t);
            e.update(t);
            CHECK(!e.in_failsafe());
        }
        CHECK(t < 1000);  // really wrapped
        CHECK_NEAR(e.angle(0), c.pan.max_deg, EPS);
        CHECK_NEAR(e.angle(1), c.tilt.max_deg, EPS);
    }
    // Silence across the wrap: fail-safe still triggers at the right time.
    {
        EyeController e(c);
        uint32_t t = UINT32_MAX - 100;
        e.look(1.0f, 1.0f, t);
        e.update(t);
        uint32_t cmd = t;
        t += 400;  // wrapped, 400 ms elapsed
        e.update(t);
        CHECK(!e.in_failsafe());
        t = cmd + c.command_timeout_ms;
        e.update(t);
        CHECK(e.in_failsafe());
    }
    // Blink spanning the wrap reopens after ms, not early and not never.
    {
        EyeController e(c);
        uint32_t t = UINT32_MAX - 50;
        e.ping(t);
        e.update(t);
        e.blink(200, t);
        t = run(e, t, 150);
        CHECK_NEAR(e.angle(2), c.lids[0].max_deg, EPS);
        t = run(e, t, 300);
        CHECK_NEAR(e.angle(2), c.lids[0].neutral_deg, EPS);
    }
    // Once in fail-safe, it stays latched even after a silence long enough to wrap the counter.
    {
        EyeController e(c);
        uint32_t t = 0;
        e.ping(t);
        e.update(t);
        e.update(1000);
        CHECK(e.in_failsafe());
        e.update(0x80000000u);
        e.update(UINT32_MAX);
        e.update(100);  // now - last_cmd == 100 after a full wrap
        CHECK(e.in_failsafe());
    }
}

static void test_set_servo_clamped_and_rate_limited() {
    EyeConfig c = make_cfg();
    for (int i = 0; i < 6; ++i) {
        const ServoCfg& s = servo(c, i);
        EyeController e(c);
        uint32_t t = 0;
        e.ping(t);
        e.update(t);
        // Above max: the first step is limited to max_speed_dps * dt, then it settles at max.
        e.set_servo(i, 500.0f, t);
        t += 10;
        e.update(t);
        CHECK_NEAR(e.angle(i), fminf(s.neutral_deg + s.max_speed_dps * 0.01f, s.max_deg), EPS);
        t = run(e, t, 400);
        CHECK_NEAR(e.angle(i), s.max_deg, EPS);
        // Below min.
        e.set_servo(i, -500.0f, t);
        float before = e.angle(i);
        t += 10;
        e.ping(t);
        e.update(t);
        CHECK(fabsf(e.angle(i) - before) <= s.max_speed_dps * 0.01f + EPS);
        t = run(e, t, 400);
        CHECK_NEAR(e.angle(i), s.min_deg, EPS);
        // In range: exact.
        float mid = 0.5f * (s.min_deg + s.max_deg) + 0.25f;
        e.set_servo(i, mid, t);
        t = run(e, t, 400);
        CHECK_NEAR(e.angle(i), mid, EPS);
        // Only servo i moved.
        for (int j = 0; j < 6; ++j) {
            if (j != i) CHECK_NEAR(e.angle(j), servo(c, j).neutral_deg, EPS);
        }
    }
}

static void test_set_servo_lid_cancels_blink() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 0;
    e.ping(t);
    e.update(t);
    e.blink(300, t);
    t = run(e, t, 100);  // all lids closed
    // Move lid 1 (lower_left, index 3) during the blink.
    e.set_servo(3, 95.0f, t);
    t = run(e, t, 400);  // well past when the blink would have reopened the lids
    CHECK_NEAR(e.angle(3), 95.0f, EPS);
    // The other lids hold their targets (closed): the cancelled blink does not reopen them.
    for (int l = 0; l < 4; ++l) {
        if (l == 1) continue;
        const ServoCfg& s = c.lids[l];
        CHECK_NEAR(e.angle(2 + l), c.lid_closed_at_max[l] ? s.max_deg : s.min_deg, EPS);
    }
    // center resets everything.
    e.center(t);
    t = run(e, t, 400);
    for (int i = 0; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).neutral_deg, EPS);

    // A pan/tilt servo command does not cancel a blink.
    e.blink(200, t);
    e.set_servo(0, 100.0f, t);
    t = run(e, t, 100);
    CHECK_NEAR(e.angle(2), c.lids[0].max_deg, EPS);
    t = run(e, t, 300);
    CHECK_NEAR(e.angle(2), c.lids[0].neutral_deg, EPS);
    CHECK_NEAR(e.angle(0), 100.0f, EPS);
}

static void test_set_servo_then_look_and_failsafe() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 0;
    e.set_servo(0, 70.0f, t);
    CHECK(!e.in_failsafe());  // a valid command
    e.set_servo(1, 110.0f, t);
    for (int i = 2; i < 6; ++i) e.set_servo(i, servo(c, i).min_deg, t);
    t = run(e, t, 400);
    CHECK_NEAR(e.angle(0), 70.0f, EPS);
    CHECK_NEAR(e.angle(1), 110.0f, EPS);
    // look overrides pan/tilt but leaves the lids.
    e.look(0.0f, 0.0f, t);
    t = run(e, t, 400);
    CHECK_NEAR(e.angle(0), c.pan.neutral_deg, EPS);
    CHECK_NEAR(e.angle(1), c.tilt.neutral_deg, EPS);
    for (int i = 2; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).min_deg, EPS);
    // Fail-safe: every servo back to neutral.
    for (int i = 0; i < 6; ++i) e.set_servo(i, servo(c, i).max_deg, t);
    t = run(e, t, 1500, 10, false);
    CHECK(e.in_failsafe());
    for (int i = 0; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).neutral_deg, EPS);
}

static void test_set_servo_invalid_ignored() {
    EyeConfig c = make_cfg();
    EyeController e(c);
    uint32_t t = 0;
    // Invalid index or non-finite angle: no motion, and not a command (stays in fail-safe).
    e.set_servo(-1, 100.0f, t);
    e.set_servo(6, 100.0f, t);
    e.set_servo(1000, 100.0f, t);
    e.set_servo(0, NAN, t);
    e.set_servo(2, INFINITY, t);
    e.set_servo(3, -INFINITY, t);
    CHECK(e.in_failsafe());
    t = run(e, t, 400, 10, false);
    for (int i = 0; i < 6; ++i) CHECK_NEAR(e.angle(i), servo(c, i).neutral_deg, EPS);

    // Ignored while running too: targets unchanged, and it does not hold off the fail-safe.
    EyeController f(c);
    uint32_t u = 0;
    f.ping(u);
    f.update(u);
    for (int k = 0; k < 100; ++k) {
        u += 10;
        f.set_servo(6, 100.0f, u);
        f.set_servo(-1, 100.0f, u);
        f.set_servo(0, NAN, u);
        f.update(u);
    }
    CHECK(f.in_failsafe());
    for (int i = 0; i < 6; ++i) CHECK_NEAR(f.angle(i), servo(c, i).neutral_deg, EPS);
}

// Deterministic LCG so failures are reproducible.
static uint32_t g_rng = 12345u;
static uint32_t rnd() {
    g_rng = g_rng * 1664525u + 1013904223u;
    return g_rng >> 8;
}
static float rndf(float lo, float hi) { return lo + (hi - lo) * (float)(rnd() & 0xFFFF) / 65535.0f; }

static void test_randomized_within_limits() {
    EyeConfig c = make_cfg();
    for (int seed = 1; seed <= 20; ++seed) {
        g_rng = (uint32_t)seed * 2654435761u;
        EyeController e(c);
        uint32_t t = rnd() << 8;  // random start, including near wraparound
        int out_of_range = 0;
        for (int step = 0; step < 5000; ++step) {
            switch (rnd() % 9) {
                case 0: e.look(rndf(-3.0f, 3.0f), rndf(-3.0f, 3.0f), t); break;
                case 1: e.look(rndf(-1.0f, 1.0f), rndf(-1.0f, 1.0f), t); break;
                case 2: e.blink((uint16_t)(rnd() & 0xFFFF), t); break;
                case 3: e.center(t); break;
                case 4: e.ping(t); break;
                case 5: e.look(NAN, rndf(-1.0f, 1.0f), t); break;
                case 6: e.set_servo((int)(rnd() % 8) - 1, rndf(-200.0f, 400.0f), t); break;
                case 7: e.set_servo((int)(rnd() % 6), rndf(0.0f, 180.0f), t); break;
                default: break;
            }
            t += rnd() % 120;  // includes dt=0 and gaps beyond the timeout
            e.update(t);
            for (int i = 0; i < 6; ++i) {
                float a = e.angle(i);
                const ServoCfg& s = servo(c, i);
                if (!(a >= s.min_deg && a <= s.max_deg)) ++out_of_range;
            }
        }
        CHECK(out_of_range == 0);
    }
}

int main() {
    test_starts_neutral();
    test_look_extremes_and_clamp();
    test_look_nan_ignored();
    test_speed_limit();
    test_blink();
    test_blink_ms_clamped();
    test_center();
    test_failsafe();
    test_wraparound();
    test_set_servo_clamped_and_rate_limited();
    test_set_servo_lid_cancels_blink();
    test_set_servo_then_look_and_failsafe();
    test_set_servo_invalid_ignored();
    test_randomized_within_limits();
    if (g_failures) {
        fprintf(stderr, "%d of %d checks FAILED\n", g_failures, g_checks);
        return 1;
    }
    printf("all %d checks passed\n", g_checks);
    return 0;
}
