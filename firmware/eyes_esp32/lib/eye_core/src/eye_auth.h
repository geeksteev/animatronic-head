// Datagram authentication for the eye protocol (docs/eye-protocol.md, "Authentication").
// Plain C++17: no Arduino, no heap. Matches brain/eye_protocol.py sign()/verify() and
// ReplayGuard.
//
// datagram = payload || tag
// tag      = lowercase hex of the first 16 bytes of HMAC-SHA256(key, payload)  (32 chars)
#ifndef EYE_CORE_EYE_AUTH_H
#define EYE_CORE_EYE_AUTH_H

#include <stddef.h>
#include <stdint.h>

#include "eye_protocol.h"

#define EYE_AUTH_KEY_LEN 32  // bytes; 64 hex characters in secrets.h / config/secrets.yaml

struct AuthKey {
    uint8_t bytes[EYE_AUTH_KEY_LEN];
};

// SHA-256 and HMAC-SHA256 (FIPS 180-4, RFC 2104). Exposed for tests.
void sha256(const uint8_t* msg, size_t len, uint8_t out[32]);
void hmac_sha256(const uint8_t* key, size_t key_len, const uint8_t* msg, size_t len, uint8_t out[32]);

// Parse a NUL-terminated string of exactly 64 hex characters (either case). Returns false
// for anything else, including an all-zero key.
bool parse_auth_key(const char* hex, AuthKey& out);

// Check the tag at the end of `buf`. On success, sets `payload_len` (the bytes before the
// tag) and returns true. Rejects datagrams longer than EYE_MAX_DATAGRAM or shorter than a tag.
// The tag comparison takes the same time wherever the first difference is.
bool verify_datagram(const AuthKey& key, const char* buf, size_t len, size_t& payload_len);

// Replay window: a seq is accepted only if it is above every seq accepted before.
// Late, duplicated and replayed datagrams are dropped. State is lost on reboot.
class ReplayGuard {
   public:
    bool fresh(uint64_t seq) const { return !any_ || seq > last_; }
    void commit(uint64_t seq) {
        any_ = true;
        last_ = seq;
    }

   private:
    bool any_ = false;
    uint64_t last_ = 0;
};

// The head's whole receive check: tag, payload (parse_command), seq present, replay window.
// Returns true and commits seq to `guard` only if every check passes; the caller then
// dispatches `out`. On false, nothing changes and the datagram must be ignored (it must not
// reset the fail-safe timer).
bool open_command(const AuthKey& key, ReplayGuard& guard, const char* buf, size_t len, Command& out);

#endif  // EYE_CORE_EYE_AUTH_H
