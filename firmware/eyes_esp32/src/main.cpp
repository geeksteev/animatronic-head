// ESP32 eye firmware: receives UDP commands (docs/eye-protocol.md) and drives the 6 eye servos.
// I/O only. Authentication, parsing, motion, rate limiting and fail-safe all live in lib/eye_core.
#include <Arduino.h>
#include <ESP32Servo.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <math.h>

#include "eye_auth.h"
#include "eye_controller.h"
#include "eye_protocol.h"
#include "limits_generated.h"
#include "pins.h"

#if __has_include("secrets.h")
#include "secrets.h"
#define EYES_HAVE_WIFI 1
#ifndef EYE_AUTH_KEY_HEX
#error "secrets.h must define EYE_AUTH_KEY_HEX (64 hex characters). See secrets.h.example."
#endif
static_assert(sizeof(EYE_AUTH_KEY_HEX) == 2 * EYE_AUTH_KEY_LEN + 1, "EYE_AUTH_KEY_HEX must be 64 hex characters");
#else
#warning "include/secrets.h not found: building without Wi-Fi (servos stay at neutral). See secrets.h.example."
#define EYES_HAVE_WIFI 0
#endif

static_assert(EYE_SERVO_COUNT == EyeController::kServoCount, "limits header and eye_core disagree on servo count");

namespace {

constexpr uint32_t kWifiRetryMs = 10000;   // restart the Wi-Fi association attempt after this long
constexpr int kMaxPacketsPerLoop = 8;      // bound the work done per loop()

ServoCfg servo_cfg(int i) {
    const EyeServoLimit& l = EYE_SERVO_LIMITS[i];
    return ServoCfg{l.min_deg, l.max_deg, l.neutral_deg, l.max_speed_dps};
}

EyeConfig make_config() {
    EyeConfig cfg{};
    cfg.pan = servo_cfg(EYE_SERVO_EYE_PAN);
    cfg.tilt = servo_cfg(EYE_SERVO_EYE_TILT);
    for (int i = 0; i < 4; ++i) {
        cfg.lids[i] = servo_cfg(EyeController::kFirstLid + i);
        cfg.lid_closed_at_max[i] = LID_CLOSED_AT_MAX[i];
    }
    cfg.command_timeout_ms = EYE_LIMITS_COMMAND_TIMEOUT_MS;
    return cfg;
}

EyeController controller(make_config());
Servo servos[EYE_SERVO_COUNT];
int last_us[EYE_SERVO_COUNT];
bool last_failsafe = true;

// Defence in depth: clamp again against the generated limits right before the pulse conversion.
// Non-finite angles go to neutral.
float clamp_to_limits(int i, float deg) {
    const EyeServoLimit& l = EYE_SERVO_LIMITS[i];
    if (!isfinite(deg)) deg = l.neutral_deg;
    if (deg < l.min_deg) deg = l.min_deg;
    if (deg > l.max_deg) deg = l.max_deg;
    return deg;
}

int angle_to_us(float deg) {
    float us = SERVO_MIN_US + deg * (SERVO_MAX_US - SERVO_MIN_US) / SERVO_RANGE_DEG;
    return static_cast<int>(lroundf(us));
}

void write_servo(int i, float deg) {
    int us = angle_to_us(clamp_to_limits(i, deg));
    if (us == last_us[i]) return;
    servos[i].writeMicroseconds(us);
    last_us[i] = us;
}

void attach_servos() {
    for (int i = 0; i < EYE_SERVO_COUNT; ++i) {
        servos[i].setPeriodHertz(50);
        servos[i].attach(SERVO_PINS[i], SERVO_MIN_US, SERVO_MAX_US);
        last_us[i] = -1;
    }
}

void write_all() {
    for (int i = 0; i < EYE_SERVO_COUNT; ++i) write_servo(i, controller.angle(i));
}

#if EYES_HAVE_WIFI
WiFiUDP udp;
AuthKey auth_key;
bool auth_key_ok = false;  // false: Wi-Fi never starts, servos stay at neutral
ReplayGuard replay_guard;
bool wifi_up = false;
uint32_t wifi_attempt_ms = 0;
char packet[EYE_MAX_DATAGRAM];

void wifi_begin(uint32_t now) {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    wifi_attempt_ms = now;
}

// Non-blocking: only checks status and occasionally restarts the association attempt.
void wifi_service(uint32_t now) {
    bool connected = WiFi.status() == WL_CONNECTED;
    if (connected && !wifi_up) {
        wifi_up = true;
        udp.begin(EYE_UDP_PORT);
        Serial.printf("[wifi] connected, IP %s, UDP port %d\n", WiFi.localIP().toString().c_str(), EYE_UDP_PORT);
    } else if (!connected && wifi_up) {
        wifi_up = false;
        udp.stop();
        Serial.println("[wifi] disconnected, retrying");
        wifi_begin(now);
    } else if (!connected && (uint32_t)(now - wifi_attempt_ms) >= kWifiRetryMs) {
        Serial.println("[wifi] still not connected, retrying");
        WiFi.disconnect();
        wifi_begin(now);
    }
}

void receive_commands() {
    if (!wifi_up) return;
    for (int n = 0; n < kMaxPacketsPerLoop; ++n) {
        int size = udp.parsePacket();
        if (size <= 0) return;
        if (size > EYE_MAX_DATAGRAM) {
            udp.clear();  // oversized: drop without parsing
            continue;
        }
        int len = udp.read(packet, sizeof(packet));
        if (len <= 0) continue;
        // Unauthenticated, malformed or replayed datagrams are dropped here and never reach
        // the controller, so they cannot move the servos or reset the fail-safe timer.
        Command cmd;
        if (open_command(auth_key, replay_guard, packet, static_cast<size_t>(len), cmd)) {
            dispatch(cmd, controller, millis());
        }
    }
}
#endif

void report_failsafe() {
    bool fs = controller.in_failsafe();
    if (fs != last_failsafe) {
        Serial.println(fs ? "[failsafe] ENTER: no commands, servos to neutral" : "[failsafe] EXIT: commands received");
        last_failsafe = fs;
    }
}

}  // namespace

void setup() {
    Serial.begin(115200);
    Serial.println("[eyes] boot");

    attach_servos();
#if EYE_LIMITS_NEUTRAL_ON_BOOT
    write_all();  // controller starts at neutral; drive it before any networking
    Serial.println("[eyes] servos at neutral");
#endif

#if EYES_HAVE_WIFI
    auth_key_ok = parse_auth_key(EYE_AUTH_KEY_HEX, auth_key);
    if (!auth_key_ok) {
        Serial.println("[auth] EYE_AUTH_KEY_HEX is not 64 hex characters (or is all zeros): Wi-Fi disabled");
    } else {
        WiFi.mode(WIFI_STA);
        WiFi.setAutoReconnect(true);
        wifi_begin(millis());
        Serial.println("[wifi] connecting");
    }
#else
    Serial.println("[wifi] no secrets.h, Wi-Fi disabled");
#endif
    Serial.println("[failsafe] active until first command");
}

void loop() {
#if EYES_HAVE_WIFI
    if (auth_key_ok) {
        wifi_service(millis());
        receive_commands();
    }
#endif
    controller.update(millis());
    report_failsafe();
    write_all();
    delay(1);  // yield to the Wi-Fi stack
}
