// Host tests for eye_auth (SHA-256, HMAC, tag check, replay window, open_command). Built and
// run by tests/test_eye_core_native.py. Tags are cross-checked against Python in
// tests/test_eye_protocol_cross.py.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "eye_auth.h"

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

static void to_hex(const uint8_t* b, size_t n, char* out) {
    static const char kHex[] = "0123456789abcdef";
    for (size_t i = 0; i < n; ++i) {
        out[2 * i] = kHex[b[i] >> 4];
        out[2 * i + 1] = kHex[b[i] & 0x0f];
    }
    out[2 * n] = '\0';
}

static bool sha_is(const char* msg, size_t len, const char* want) {
    uint8_t d[32];
    char hex[65];
    sha256(reinterpret_cast<const uint8_t*>(msg), len, d);
    to_hex(d, 32, hex);
    if (strcmp(hex, want) != 0) fprintf(stderr, "sha256 got %s want %s\n", hex, want);
    return strcmp(hex, want) == 0;
}

static bool hmac_is(const uint8_t* key, size_t key_len, const char* msg, const char* want) {
    uint8_t d[32];
    char hex[65];
    hmac_sha256(key, key_len, reinterpret_cast<const uint8_t*>(msg), strlen(msg), d);
    to_hex(d, 32, hex);
    if (strcmp(hex, want) != 0) fprintf(stderr, "hmac got %s want %s\n", hex, want);
    return strcmp(hex, want) == 0;
}

static void test_sha256() {
    // FIPS 180-4 examples, plus lengths around the padding boundary.
    CHECK(sha_is("", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
    CHECK(sha_is("abc", 3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
    const char* m56 = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    CHECK(sha_is(m56, 56, "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"));
    static char a[1000];
    memset(a, 'a', sizeof(a));
    CHECK(sha_is(a, 55, "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318"));
    CHECK(sha_is(a, 64, "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb"));
    CHECK(sha_is(a, 1000, "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3"));
}

static void test_hmac() {
    // RFC 4231 test cases 1, 2 and 6 (key longer than a block).
    uint8_t k1[20];
    memset(k1, 0x0b, sizeof(k1));
    CHECK(hmac_is(k1, sizeof(k1), "Hi There",
                  "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"));
    CHECK(hmac_is(reinterpret_cast<const uint8_t*>("Jefe"), 4, "what do ya want for nothing?",
                  "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"));
    uint8_t k6[131];
    memset(k6, 0xaa, sizeof(k6));
    CHECK(hmac_is(k6, sizeof(k6), "Test Using Larger Than Block-Size Key - Hash Key First",
                  "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"));
}

static const char* kKeyHex = "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20";

static AuthKey test_key() {
    AuthKey k{};
    CHECK(parse_auth_key(kKeyHex, k));
    return k;
}

static void test_parse_key() {
    AuthKey k{};
    CHECK(parse_auth_key(kKeyHex, k));
    CHECK(k.bytes[0] == 0x01 && k.bytes[31] == 0x20);
    CHECK(parse_auth_key("0102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F20", k));
    CHECK(k.bytes[9] == 0x0a);
    CHECK(!parse_auth_key(nullptr, k));
    CHECK(!parse_auth_key("", k));
    CHECK(!parse_auth_key("replace-with-64-hex-characters", k));
    CHECK(!parse_auth_key("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f2", k));
    CHECK(!parse_auth_key("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f200", k));
    CHECK(!parse_auth_key("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1fg0", k));
    CHECK(!parse_auth_key("0000000000000000000000000000000000000000000000000000000000000000", k));
}

// Datagrams signed by brain/eye_protocol.py with key 01..20 (see kKeyHex).
static const char* kPing1 = R"({"v":1,"seq":1,"cmd":"ping"})" "01eee928800ab2f1eaae312901a08397";
static const char* kLook2 = R"({"v":1,"seq":2,"cmd":"look","x":0.5,"y":-1})" "334a97cbf19bba17d3e4a4565356326a";
static const char* kCenter3 = R"({"v":1,"seq":3,"cmd":"center"})" "eff7228e7b68019f9bb2134765ee491f";
static const char* kBlink4 = R"({"v":1,"seq":4,"cmd":"blink","ms":1000})" "7f62f258e8d44d4563aab51a33a40c2d";
static const char* kNoSeq = R"({"v":1,"cmd":"ping"})" "9827dc625993bb8476641b2794bfff08";

static void test_verify() {
    AuthKey k = test_key();
    size_t n = 0;
    CHECK(verify_datagram(k, kPing1, strlen(kPing1), n) && n == strlen(kPing1) - EYE_AUTH_TAG_LEN);
    CHECK(verify_datagram(k, kLook2, strlen(kLook2), n));

    // Any single changed byte, in payload or tag, is rejected.
    char buf[EYE_MAX_DATAGRAM + 8];
    size_t len = strlen(kLook2);
    bool all_rejected = true;
    for (size_t i = 0; i < len; ++i) {
        memcpy(buf, kLook2, len);
        buf[i] ^= 0x01;
        if (verify_datagram(k, buf, len, n)) all_rejected = false;
    }
    CHECK(all_rejected);

    // Upper-case tag, truncated, unsigned, too short, too long, null.
    memcpy(buf, kPing1, strlen(kPing1));
    for (size_t i = strlen(kPing1) - EYE_AUTH_TAG_LEN; i < strlen(kPing1); ++i) {
        if (buf[i] >= 'a' && buf[i] <= 'f') buf[i] = static_cast<char>(buf[i] - 'a' + 'A');
    }
    CHECK(!verify_datagram(k, buf, strlen(kPing1), n));
    CHECK(!verify_datagram(k, kPing1, strlen(kPing1) - 1, n));
    const char* unsigned_ping = R"({"v":1,"seq":1,"cmd":"ping"})";
    CHECK(!verify_datagram(k, unsigned_ping, strlen(unsigned_ping), n));
    CHECK(!verify_datagram(k, kPing1, EYE_AUTH_TAG_LEN - 1, n));
    CHECK(!verify_datagram(k, kPing1, 0, n));
    CHECK(!verify_datagram(k, nullptr, 40, n));
    memset(buf, ' ', sizeof(buf));
    CHECK(!verify_datagram(k, buf, EYE_MAX_DATAGRAM + 1, n));

    // Wrong key.
    AuthKey other = k;
    other.bytes[0] ^= 0x80;
    CHECK(!verify_datagram(other, kPing1, strlen(kPing1), n));

    // An empty payload has a tag too (verify only checks the tag; parsing rejects it later).
    uint8_t mac[32];
    hmac_sha256(k.bytes, EYE_AUTH_KEY_LEN, nullptr, 0, mac);
    to_hex(mac, EYE_AUTH_TAG_LEN / 2, buf);
    CHECK(verify_datagram(k, buf, EYE_AUTH_TAG_LEN, n) && n == 0);
}

static void test_replay_guard() {
    ReplayGuard g;
    CHECK(g.fresh(0));
    g.commit(0);
    CHECK(!g.fresh(0));
    CHECK(g.fresh(1));
    g.commit(10);
    CHECK(!g.fresh(9));
    CHECK(!g.fresh(10));
    CHECK(g.fresh(11));
    CHECK(g.fresh(9007199254740991ull));
}

static bool open(const AuthKey& k, ReplayGuard& g, const char* s, Command& c) {
    return open_command(k, g, s, strlen(s), c);
}

static void test_open_command() {
    AuthKey k = test_key();
    ReplayGuard g;
    Command c{};
    CHECK(open(k, g, kPing1, c) && c.type == CmdType::Ping && c.seq == 1);
    CHECK(!open(k, g, kPing1, c));  // replayed
    CHECK(open(k, g, kLook2, c) && c.type == CmdType::Look && c.seq == 2);
    CHECK(fabsf(c.x - 0.5f) < 1e-6f && fabsf(c.y + 1.0f) < 1e-6f);
    CHECK(open(k, g, kBlink4, c) && c.type == CmdType::Blink && c.blink_ms == 1000);
    CHECK(!open(k, g, kCenter3, c));  // late: 3 < 4

    // A rejected datagram does not move the window.
    ReplayGuard g2;
    CHECK(!open(k, g2, kNoSeq, c));   // valid tag, but no seq
    char forged[EYE_MAX_DATAGRAM];
    const char* body = R"({"v":1,"seq":9007199254740991,"cmd":"ping"})";
    size_t bl = strlen(body);
    memcpy(forged, body, bl);
    memset(forged + bl, 'a', EYE_AUTH_TAG_LEN);
    CHECK(!open_command(k, g2, forged, bl + EYE_AUTH_TAG_LEN, c));  // bad tag, huge seq
    CHECK(open(k, g2, kPing1, c));  // seq 1 still accepted

    // A valid tag on an invalid payload is rejected without committing its seq.
    ReplayGuard g3;
    char buf[EYE_MAX_DATAGRAM];
    const char* bad = R"({"v":2,"seq":50,"cmd":"ping"})";
    size_t n = strlen(bad);
    memcpy(buf, bad, n);
    uint8_t mac[32];
    hmac_sha256(k.bytes, EYE_AUTH_KEY_LEN, reinterpret_cast<const uint8_t*>(bad), n, mac);
    char tag[EYE_AUTH_TAG_LEN + 1];
    to_hex(mac, EYE_AUTH_TAG_LEN / 2, tag);
    memcpy(buf + n, tag, EYE_AUTH_TAG_LEN);
    CHECK(!open_command(k, g3, buf, n + EYE_AUTH_TAG_LEN, c));
    CHECK(open(k, g3, kPing1, c));
}

static EyeConfig make_cfg() {
    EyeConfig c{};
    c.pan = {60.0f, 120.0f, 90.0f, 300.0f};
    c.tilt = {70.0f, 115.0f, 95.0f, 200.0f};
    for (int i = 0; i < 4; ++i) c.lids[i] = {80.0f, 110.0f, 90.0f, 600.0f};
    c.command_timeout_ms = 500;
    return c;
}

// The receive loop in main.cpp: open_command, then dispatch.
static void receive(const AuthKey& k, ReplayGuard& g, EyeController& e, const char* s, uint32_t t) {
    Command c{};
    if (open(k, g, s, c)) dispatch(c, e, t);
}

static void test_failsafe_not_fed_by_unauthenticated() {
    AuthKey k = test_key();
    ReplayGuard g;
    EyeController e(make_cfg());
    uint32_t t = 0;

    receive(k, g, e, kLook2, t);  // authenticated: exits fail-safe
    CHECK(!e.in_failsafe());

    // An attacker keeps sending well-formed but unsigned, wrongly signed and replayed datagrams.
    AuthKey wrong = k;
    wrong.bytes[5] ^= 1;
    for (int i = 0; i < 100; ++i) {
        t += 10;
        receive(k, g, e, R"({"v":1,"seq":99,"cmd":"ping"})", t);
        receive(wrong, g, e, kPing1, t);  // head with a different key
        receive(k, g, e, kLook2, t);      // replay
        receive(k, g, e, kPing1, t);      // replay of an older one
        e.update(t);
    }
    CHECK(e.in_failsafe());
    CHECK(fabsf(e.angle(EyeController::kPan) - 90.0f) < 1e-3f);

    // The real brain's next command still gets through.
    receive(k, g, e, kBlink4, t);
    CHECK(!e.in_failsafe());
}

int main() {
    test_sha256();
    test_hmac();
    test_parse_key();
    test_verify();
    test_replay_guard();
    test_open_command();
    test_failsafe_not_fed_by_unauthenticated();
    printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
