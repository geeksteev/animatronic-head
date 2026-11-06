#include "eye_auth.h"

#include <string.h>

namespace {

constexpr size_t kBlock = 64;
constexpr size_t kDigest = 32;
static_assert(EYE_AUTH_TAG_LEN % 2 == 0 && EYE_AUTH_TAG_LEN / 2 <= kDigest, "tag is truncated digest hex");

constexpr uint32_t kK[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

struct Sha256 {
    uint32_t h[8];
    uint8_t buf[kBlock];
    size_t buf_len;
    uint64_t total;  // bytes

    void init() {
        static const uint32_t kInit[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                          0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
        memcpy(h, kInit, sizeof(h));
        buf_len = 0;
        total = 0;
    }

    void block(const uint8_t* p) {
        uint32_t w[64];
        for (int i = 0; i < 16; ++i) {
            w[i] = (uint32_t{p[4 * i]} << 24) | (uint32_t{p[4 * i + 1]} << 16) |
                   (uint32_t{p[4 * i + 2]} << 8) | uint32_t{p[4 * i + 3]};
        }
        for (int i = 16; i < 64; ++i) {
            uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
            uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 64; ++i) {
            uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = hh + S1 + ch + kK[i] + w[i];
            uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + maj;
            hh = g;
            g = f;
            f = e;
            e = d + t1;
            d = c;
            c = b;
            b = a;
            a = t1 + t2;
        }
        h[0] += a;
        h[1] += b;
        h[2] += c;
        h[3] += d;
        h[4] += e;
        h[5] += f;
        h[6] += g;
        h[7] += hh;
    }

    void update(const uint8_t* p, size_t n) {
        total += n;
        while (n > 0) {
            size_t take = kBlock - buf_len;
            if (take > n) take = n;
            memcpy(buf + buf_len, p, take);
            buf_len += take;
            p += take;
            n -= take;
            if (buf_len == kBlock) {
                block(buf);
                buf_len = 0;
            }
        }
    }

    void final(uint8_t out[kDigest]) {
        const uint64_t bits = total * 8;
        const uint8_t pad = 0x80;
        const uint8_t zero = 0;
        update(&pad, 1);
        while (buf_len != kBlock - 8) update(&zero, 1);
        uint8_t len_be[8];
        for (int i = 0; i < 8; ++i) len_be[i] = static_cast<uint8_t>(bits >> (56 - 8 * i));
        update(len_be, 8);
        for (int i = 0; i < 8; ++i) {
            out[4 * i] = static_cast<uint8_t>(h[i] >> 24);
            out[4 * i + 1] = static_cast<uint8_t>(h[i] >> 16);
            out[4 * i + 2] = static_cast<uint8_t>(h[i] >> 8);
            out[4 * i + 3] = static_cast<uint8_t>(h[i]);
        }
    }
};

int hex_nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

}  // namespace

void sha256(const uint8_t* msg, size_t len, uint8_t out[32]) {
    Sha256 s;
    s.init();
    s.update(msg, len);
    s.final(out);
}

void hmac_sha256(const uint8_t* key, size_t key_len, const uint8_t* msg, size_t len, uint8_t out[32]) {
    uint8_t k[kBlock] = {};
    if (key_len > kBlock) {
        sha256(key, key_len, k);
    } else if (key_len > 0) {
        memcpy(k, key, key_len);
    }
    uint8_t pad[kBlock];
    uint8_t inner[kDigest];

    Sha256 s;
    for (size_t i = 0; i < kBlock; ++i) pad[i] = k[i] ^ 0x36;
    s.init();
    s.update(pad, kBlock);
    s.update(msg, len);
    s.final(inner);

    for (size_t i = 0; i < kBlock; ++i) pad[i] = k[i] ^ 0x5c;
    s.init();
    s.update(pad, kBlock);
    s.update(inner, kDigest);
    s.final(out);
}

bool parse_auth_key(const char* hex, AuthKey& out) {
    if (hex == nullptr) return false;
    uint8_t any = 0;
    for (size_t i = 0; i < EYE_AUTH_KEY_LEN; ++i) {
        // A NUL in either position fails hex_nibble, so a short string never reads past its end.
        int hi = hex_nibble(hex[2 * i]);
        if (hi < 0) return false;
        int lo = hex_nibble(hex[2 * i + 1]);
        if (lo < 0) return false;
        out.bytes[i] = static_cast<uint8_t>(hi * 16 + lo);
        any |= out.bytes[i];
    }
    return hex[2 * EYE_AUTH_KEY_LEN] == '\0' && any != 0;
}

bool verify_datagram(const AuthKey& key, const char* buf, size_t len, size_t& payload_len) {
    if (buf == nullptr || len < EYE_AUTH_TAG_LEN || len > EYE_MAX_DATAGRAM) return false;
    const size_t n = len - EYE_AUTH_TAG_LEN;
    uint8_t mac[kDigest];
    hmac_sha256(key.bytes, EYE_AUTH_KEY_LEN, reinterpret_cast<const uint8_t*>(buf), n, mac);

    static const char kHex[] = "0123456789abcdef";
    const char* got = buf + n;
    uint8_t diff = 0;
    for (size_t i = 0; i < EYE_AUTH_TAG_LEN / 2; ++i) {
        diff |= static_cast<uint8_t>(got[2 * i] ^ kHex[mac[i] >> 4]);
        diff |= static_cast<uint8_t>(got[2 * i + 1] ^ kHex[mac[i] & 0x0f]);
    }
    if (diff != 0) return false;
    payload_len = n;
    return true;
}

bool open_command(const AuthKey& key, ReplayGuard& guard, const char* buf, size_t len, Command& out) {
    size_t payload_len = 0;
    if (!verify_datagram(key, buf, len, payload_len)) return false;
    if (!parse_command(buf, payload_len, out)) return false;
    if (!out.has_seq || !guard.fresh(out.seq)) return false;
    guard.commit(out.seq);
    return true;
}
