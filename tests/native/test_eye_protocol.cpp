// Host tests for eye_protocol (parse_command, dispatch). Built and run by
// tests/test_eye_core_native.py.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "eye_protocol.h"

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

static bool parse(const char* s, Command& c) { return parse_command(s, strlen(s), c); }

static bool near(float a, float b) { return fabsf(a - b) <= 1e-4f; }

static void expect_look(const char* s, float x, float y) {
    Command c{};
    bool ok = parse(s, c);
    ++g_checks;
    if (!ok || c.type != CmdType::Look || !near(c.x, x) || !near(c.y, y)) {
        ++g_failures;
        fprintf(stderr, "expected look %g %g from %s (ok=%d x=%g y=%g)\n", x, y, s, ok, c.x, c.y);
    }
}

static void expect_blink(const char* s, unsigned ms) {
    Command c{};
    bool ok = parse(s, c);
    ++g_checks;
    if (!ok || c.type != CmdType::Blink || c.blink_ms != ms) {
        ++g_failures;
        fprintf(stderr, "expected blink %u from %s (ok=%d ms=%u)\n", ms, s, ok,
                static_cast<unsigned>(c.blink_ms));
    }
}

static void expect_type(const char* s, CmdType t) {
    Command c{};
    bool ok = parse(s, c);
    ++g_checks;
    if (!ok || c.type != t) {
        ++g_failures;
        fprintf(stderr, "expected type %d from %s (ok=%d)\n", static_cast<int>(t), s, ok);
    }
}

static void expect_invalid(const char* s) {
    Command c{};
    ++g_checks;
    if (parse(s, c)) {
        ++g_failures;
        fprintf(stderr, "expected invalid: %s\n", s);
    }
}

static void test_valid() {
    expect_look(R"({"v":1,"seq":7,"cmd":"look","x":0.25,"y":-0.5})", 0.25f, -0.5f);
    expect_look(R"({"v":1,"cmd":"look","x":0,"y":0})", 0.0f, 0.0f);
    expect_look(R"({"y":-1,"x":1,"cmd":"look","v":1})", 1.0f, -1.0f);  // any key order
    expect_look(" \t\r\n{ \"v\" : 1 ,\n\"cmd\":\"look\", \"x\": 2.5e-1 ,\"y\":-5E-1 }\n ",
                0.25f, -0.5f);
    expect_blink(R"({"v":1,"seq":8,"cmd":"blink","ms":150})", 150);
    expect_blink(R"({"v":1,"cmd":"blink"})", 150);  // default
    expect_blink(R"({"v":1,"cmd":"blink","ms":400})", 400);
    expect_type(R"({"v":1,"seq":9,"cmd":"center"})", CmdType::Center);
    expect_type(R"({"v":1,"seq":10,"cmd":"ping"})", CmdType::Ping);
    // Escaped strings compare after decoding, as in Python.
    expect_type(R"({"v":1,"cmd":"ping"})", CmdType::Ping);
    // Unknown keys are ignored whatever their value.
    expect_type(R"({"v":1,"cmd":"ping","extra":{"a":[1,true,null,"s\"x"]},"z":"é"})",
                CmdType::Ping);
    // Fields not used by the command are ignored, even if non-numeric.
    expect_type(R"({"v":1,"cmd":"center","x":"left","ms":null})", CmdType::Center);
    // Duplicate keys: the last one wins (json.loads behaviour).
    expect_look(R"({"v":1,"cmd":"look","x":"bad","x":0.5,"y":0})", 0.5f, 0.0f);
    expect_type(R"({"v":1,"cmd":"look","cmd":"ping"})", CmdType::Ping);
    // seq bounds; -0 is the integer 0.
    expect_type(R"({"v":1,"seq":0,"cmd":"ping"})", CmdType::Ping);
    expect_type(R"({"v":1,"seq":-0,"cmd":"ping"})", CmdType::Ping);
    expect_type(R"({"v":1,"seq":2147483647,"cmd":"ping"})", CmdType::Ping);
    // Underflow is fine (becomes 0).
    expect_look(R"({"v":1,"cmd":"look","x":1e-999,"y":-1e-400})", 0.0f, 0.0f);
}

static void test_clamping() {
    expect_look(R"({"v":1,"cmd":"look","x":5,"y":-7.5})", 1.0f, -1.0f);
    expect_look(R"({"v":1,"cmd":"look","x":1e300,"y":-1e300})", 1.0f, -1.0f);
    expect_blink(R"({"v":1,"cmd":"blink","ms":1})", 50);
    expect_blink(R"({"v":1,"cmd":"blink","ms":-20})", 50);
    expect_blink(R"({"v":1,"cmd":"blink","ms":99999})", 1000);
    expect_blink(R"({"v":1,"cmd":"blink","ms":1e9})", 1000);
    // Rounded like Python's round(): ties to even.
    expect_blink(R"({"v":1,"cmd":"blink","ms":150.4})", 150);
    expect_blink(R"({"v":1,"cmd":"blink","ms":150.6})", 151);
    expect_blink(R"({"v":1,"cmd":"blink","ms":150.5})", 150);
    expect_blink(R"({"v":1,"cmd":"blink","ms":151.5})", 152);
    expect_blink(R"({"v":1,"cmd":"blink","ms":2.5e2})", 250);
}

static void test_invalid() {
    // Not an object / not JSON.
    expect_invalid("");
    expect_invalid("   ");
    expect_invalid("[1]");
    expect_invalid("1");
    expect_invalid("\"ping\"");
    expect_invalid("null");
    expect_invalid(R"({"v":1,"cmd":"ping")");
    expect_invalid(R"({"v":1,"cmd":"ping",})");
    expect_invalid(R"({"v":1,,"cmd":"ping"})");
    expect_invalid(R"({"v":1 "cmd":"ping"})");
    expect_invalid(R"({v:1,"cmd":"ping"})");
    expect_invalid(R"({'v':1,'cmd':'ping'})");
    expect_invalid(R"({"v":1,"cmd":"ping"}x)");
    expect_invalid(R"({"v":1,"cmd":"ping"}{})");
    expect_invalid(R"({"v":1,"cmd":"ping","e":[1,]})");
    expect_invalid(R"({"v":1,"cmd":"ping","e":"\x"})");
    expect_invalid(R"({"v":1,"cmd":"ping","e":"\u12"})");
    expect_invalid("{\"v\":1,\"cmd\":\"ping\",\"e\":\"a\tb\"}");  // raw control char
    expect_invalid(R"({"v":1,"cmd":"ping","e":tru})");
    // Version.
    expect_invalid(R"({"cmd":"ping"})");
    expect_invalid(R"({"v":2,"cmd":"ping"})");
    expect_invalid(R"({"v":1.0,"cmd":"ping"})");
    expect_invalid(R"({"v":1e0,"cmd":"ping"})");
    expect_invalid(R"({"v":"1","cmd":"ping"})");
    expect_invalid(R"({"v":true,"cmd":"ping"})");
    expect_invalid(R"({"v":01,"cmd":"ping"})");
    // seq.
    expect_invalid(R"({"v":1,"seq":-1,"cmd":"ping"})");
    expect_invalid(R"({"v":1,"seq":2147483648,"cmd":"ping"})");
    expect_invalid(R"({"v":1,"seq":99999999999999999999,"cmd":"ping"})");
    expect_invalid(R"({"v":1,"seq":1.5,"cmd":"ping"})");
    expect_invalid(R"({"v":1,"seq":"7","cmd":"ping"})");
    expect_invalid(R"({"v":1,"seq":null,"cmd":"ping"})");
    // cmd.
    expect_invalid(R"({"v":1})");
    expect_invalid(R"({"v":1,"cmd":"Ping"})");
    expect_invalid(R"({"v":1,"cmd":"wink"})");
    expect_invalid(R"({"v":1,"cmd":1})");
    expect_invalid(R"({"v":1,"cmd":"pingpong"})");
    // look fields.
    expect_invalid(R"({"v":1,"cmd":"look","x":0.1})");
    expect_invalid(R"({"v":1,"cmd":"look","y":0.1})");
    expect_invalid(R"({"v":1,"cmd":"look","x":"0.1","y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":true,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":null,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":[0],"y":0})");
    // Non-finite and non-JSON numbers.
    expect_invalid(R"({"v":1,"cmd":"look","x":NaN,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":Infinity,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":-Infinity,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":1e999,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":0,"y":-1e400})");
    expect_invalid(R"({"v":1,"cmd":"look","x":+1,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":.5,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":1.,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":0x1,"y":0})");
    expect_invalid(R"({"v":1,"cmd":"look","x":1e,"y":0})");
    // blink ms.
    expect_invalid(R"({"v":1,"cmd":"blink","ms":"150"})");
    expect_invalid(R"({"v":1,"cmd":"blink","ms":null})");
    expect_invalid(R"({"v":1,"cmd":"blink","ms":false})");
    expect_invalid(R"({"v":1,"cmd":"blink","ms":1e999})");
}

static void test_bytes() {
    Command c{};
    // Embedded NUL and invalid UTF-8.
    const char nul[] = "{\"v\":1,\"cmd\":\"ping\"}\0";
    CHECK(!parse_command(nul, sizeof(nul) - 1, c));
    CHECK(parse_command(nul, sizeof(nul) - 2, c) && c.type == CmdType::Ping);
    CHECK(!parse_command("{\"v\":1,\"cmd\":\"ping\",\"e\":\"\xff\"}", 27, c));
    CHECK(!parse_command("{\"v\":1,\"cmd\":\"ping\",\"e\":\"\xc0\xaf\"}", 28, c));  // overlong
    CHECK(!parse_command("{\"v\":1,\"cmd\":\"ping\",\"e\":\"\xed\xa0\x80\"}", 29, c));  // surrogate
    CHECK(!parse_command(nullptr, 0, c));
    // Length is honoured: no reading past len.
    const char* s = R"({"v":1,"cmd":"ping"}garbage)";
    CHECK(parse_command(s, 20, c) && c.type == CmdType::Ping);

    // Size limit: exactly 128 bytes is fine, 129 is rejected.
    char buf[EYE_MAX_DATAGRAM + 2];
    const char* head = "{\"v\":1,\"cmd\":\"ping\",\"pad\":\"";
    size_t hl = strlen(head);
    memset(buf, 'a', sizeof(buf));
    memcpy(buf, head, hl);
    buf[EYE_MAX_DATAGRAM - 2] = '"';
    buf[EYE_MAX_DATAGRAM - 1] = '}';
    CHECK(parse_command(buf, EYE_MAX_DATAGRAM, c) && c.type == CmdType::Ping);
    buf[EYE_MAX_DATAGRAM - 1] = ' ';
    buf[EYE_MAX_DATAGRAM] = '}';
    CHECK(!parse_command(buf, EYE_MAX_DATAGRAM + 1, c));

    // Deep nesting inside an unknown key is valid JSON and must not crash.
    char deep[EYE_MAX_DATAGRAM + 1];
    size_t n = 0;
    const char* pre = "{\"v\":1,\"cmd\":\"ping\",\"d\":";
    memcpy(deep, pre, strlen(pre));
    n = strlen(pre);
    size_t depth = (EYE_MAX_DATAGRAM - n - 1) / 2;
    for (size_t i = 0; i < depth; ++i) deep[n++] = '[';
    for (size_t i = 0; i < depth; ++i) deep[n++] = ']';
    deep[n++] = '}';
    CHECK(n <= EYE_MAX_DATAGRAM);
    CHECK(parse_command(deep, n, c) && c.type == CmdType::Ping);
}

static EyeConfig make_cfg() {
    EyeConfig c{};
    c.pan = {60.0f, 120.0f, 90.0f, 300.0f};
    c.tilt = {70.0f, 115.0f, 95.0f, 200.0f};
    for (int i = 0; i < 4; ++i) c.lids[i] = {80.0f, 110.0f, 90.0f, 600.0f};
    c.lid_closed_at_max[0] = c.lid_closed_at_max[2] = true;
    c.command_timeout_ms = 500;
    return c;
}

// 400 ms of updates: long enough to reach any target, shorter than the 500 ms timeout.
static void settle(EyeController& e, uint32_t& t) {
    for (int i = 0; i < 40; ++i) {
        t += 10;
        e.update(t);
    }
}

static void test_dispatch() {
    EyeConfig cfg = make_cfg();
    EyeController e(cfg);
    uint32_t t = 0;
    Command c{};

    CHECK(e.in_failsafe());
    CHECK(parse(R"({"v":1,"cmd":"ping"})", c));
    dispatch(c, e, t);
    CHECK(!e.in_failsafe());

    CHECK(parse(R"({"v":1,"cmd":"look","x":0.5,"y":-1})", c));
    dispatch(c, e, t);
    settle(e, t);
    CHECK(fabsf(e.angle(EyeController::kPan) - 105.0f) < 1e-3f);
    CHECK(fabsf(e.angle(EyeController::kTilt) - 70.0f) < 1e-3f);

    CHECK(parse(R"({"v":1,"cmd":"blink","ms":1000})", c));
    dispatch(c, e, t);
    settle(e, t);
    CHECK(fabsf(e.angle(EyeController::kFirstLid) - 110.0f) < 1e-3f);  // closed at max
    CHECK(fabsf(e.angle(EyeController::kFirstLid + 1) - 80.0f) < 1e-3f);

    CHECK(parse(R"({"v":1,"cmd":"center"})", c));
    dispatch(c, e, t);
    settle(e, t);
    CHECK(fabsf(e.angle(EyeController::kPan) - 90.0f) < 1e-3f);
    CHECK(fabsf(e.angle(EyeController::kTilt) - 95.0f) < 1e-3f);
    CHECK(fabsf(e.angle(EyeController::kFirstLid) - 90.0f) < 1e-3f);
    CHECK(!e.in_failsafe());
}

int main() {
    test_valid();
    test_clamping();
    test_invalid();
    test_bytes();
    test_dispatch();
    printf("eye_protocol: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
