// Cross-check harness for eye_protocol and eye_auth. Built and driven by
// tests/test_eye_protocol_cross.py.
//
//   parse_stdin            each line is a payload: parse_command
//   parse_stdin KEY_HEX    each line is a datagram: open_command with one ReplayGuard for
//                          the whole run, as on the head
//
// Reads one hex-encoded input per line (hex so any bytes, including newlines, can be sent)
// and prints one result line per input: "look X Y", "blink MS", "center", "ping",
// "servo CH DEG" or "invalid". In KEY_HEX mode accepted lines are prefixed with "SEQ ".
#include <stdio.h>
#include <string.h>

#include "eye_auth.h"
#include "eye_protocol.h"

static int hex_val(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int main(int argc, char** argv) {
    AuthKey key{};
    ReplayGuard guard;
    const bool auth = argc > 1;
    if (auth && !parse_auth_key(argv[1], key)) {
        fprintf(stderr, "bad key\n");
        return 2;
    }
    // Up to 4 * EYE_MAX_DATAGRAM bytes so oversize inputs can be tested too.
    static char line[16 * EYE_MAX_DATAGRAM];
    static char data[8 * EYE_MAX_DATAGRAM];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        size_t n = strcspn(line, "\r\n");
        size_t len = 0;
        bool ok = (n % 2 == 0) && (n / 2 <= sizeof(data));
        for (size_t i = 0; ok && i < n; i += 2) {
            int hi = hex_val(line[i]), lo = hex_val(line[i + 1]);
            if (hi < 0 || lo < 0) ok = false;
            else data[len++] = static_cast<char>(hi * 16 + lo);
        }
        Command c{};
        if (!ok) {
            printf("error\n");
        } else if (auth ? !open_command(key, guard, data, len, c) : !parse_command(data, len, c)) {
            printf("invalid\n");
        } else {
            if (auth) printf("%llu ", static_cast<unsigned long long>(c.seq));
            switch (c.type) {
                case CmdType::Look: printf("look %.3f %.3f\n", c.x, c.y); break;
                case CmdType::Blink: printf("blink %u\n", static_cast<unsigned>(c.blink_ms)); break;
                case CmdType::Center: printf("center\n"); break;
                case CmdType::Ping: printf("ping\n"); break;
                case CmdType::Servo: printf("servo %u %.3f\n", static_cast<unsigned>(c.ch), c.deg); break;
            }
        }
        fflush(stdout);
    }
    return 0;
}
