#include "eye_protocol.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

// A small strict JSON reader, sized for one datagram. It validates the whole input with the
// same grammar as Python's json module (strict mode), skips values of unknown keys (including
// nested objects/arrays), and keeps the last occurrence of each key it knows about, which is
// what json.loads does with duplicate keys.

namespace {

constexpr int kSeqMax = 2147483647;  // 2^31 - 1
constexpr double kBlinkDefaultMs = 150.0;
// Each nesting level costs at least two bytes, so this can never reject a datagram that fits.
constexpr int kMaxDepth = EYE_MAX_DATAGRAM / 2 + 1;

enum class Kind : uint8_t { None, Number, String, True, False, Null, Object, Array };

struct Value {
    Kind kind = Kind::None;
    bool is_int = false;  // Number with no fraction and no exponent (a Python int)
    const char* start = nullptr;  // Number: literal text (points into the datagram)
    size_t len = 0;
    bool ascii_ok = false;  // String: decoded fully into `text` (short, ASCII only)
    char text[8] = {};
};

struct Reader {
    const char* p;
    const char* end;

    bool at_end() const { return p >= end; }
    char peek() const { return at_end() ? '\0' : *p; }
    void skip_ws() {
        while (!at_end() && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p;
    }
    bool eat(char ch) {
        if (at_end() || *p != ch) return false;
        ++p;
        return true;
    }
};

bool is_digit(char c) { return c >= '0' && c <= '9'; }

int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// Strict UTF-8 as accepted by Python's bytes.decode("utf-8"): no overlongs, no surrogates,
// nothing above U+10FFFF.
bool valid_utf8(const unsigned char* s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = s[i];
        if (c < 0x80) {
            ++i;
            continue;
        }
        size_t extra;
        unsigned char lo = 0x80, hi = 0xBF;
        if (c >= 0xC2 && c <= 0xDF) {
            extra = 1;
        } else if (c >= 0xE0 && c <= 0xEF) {
            extra = 2;
            if (c == 0xE0) lo = 0xA0;
            if (c == 0xED) hi = 0x9F;
        } else if (c >= 0xF0 && c <= 0xF4) {
            extra = 3;
            if (c == 0xF0) lo = 0x90;
            if (c == 0xF4) hi = 0x8F;
        } else {
            return false;
        }
        if (n - i - 1 < extra) return false;
        if (s[i + 1] < lo || s[i + 1] > hi) return false;
        for (size_t k = 2; k <= extra; ++k) {
            if (s[i + k] < 0x80 || s[i + k] > 0xBF) return false;
        }
        i += extra + 1;
    }
    return true;
}

// String starting at the opening quote. Decodes into out.text when the result is short ASCII.
bool scan_string(Reader& r, Value& out) {
    if (!r.eat('"')) return false;
    out.kind = Kind::String;
    out.ascii_ok = true;
    out.len = 0;
    while (true) {
        if (r.at_end()) return false;
        unsigned char c = static_cast<unsigned char>(*r.p++);
        unsigned cp;
        if (c == '"') break;
        if (c < 0x20) return false;  // raw control characters are not allowed
        if (c == '\\') {
            if (r.at_end()) return false;
            char e = *r.p++;
            switch (e) {
                case '"': cp = '"'; break;
                case '\\': cp = '\\'; break;
                case '/': cp = '/'; break;
                case 'b': cp = '\b'; break;
                case 'f': cp = '\f'; break;
                case 'n': cp = '\n'; break;
                case 'r': cp = '\r'; break;
                case 't': cp = '\t'; break;
                case 'u': {
                    if (r.end - r.p < 4) return false;
                    cp = 0;
                    for (int k = 0; k < 4; ++k) {
                        int h = hex_val(*r.p++);
                        if (h < 0) return false;
                        cp = (cp << 4) | static_cast<unsigned>(h);
                    }
                    break;  // lone surrogates are accepted, as in Python
                }
                default:
                    return false;
            }
        } else {
            cp = c;
        }
        if (cp >= 0x80 || out.len >= sizeof(out.text)) {
            out.ascii_ok = false;  // cannot equal any key or command name we know
        } else if (out.ascii_ok) {
            out.text[out.len++] = static_cast<char>(cp);
        }
    }
    return true;
}

// -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?
bool scan_number(Reader& r, Value& out) {
    const char* s = r.p;
    bool is_int = true;
    r.eat('-');
    if (r.eat('0')) {
        // a leading zero stands alone
    } else if (is_digit(r.peek()) && r.peek() != '0') {
        while (is_digit(r.peek())) ++r.p;
    } else {
        return false;
    }
    if (r.eat('.')) {
        is_int = false;
        if (!is_digit(r.peek())) return false;
        while (is_digit(r.peek())) ++r.p;
    }
    if (r.peek() == 'e' || r.peek() == 'E') {
        ++r.p;
        is_int = false;
        if (r.peek() == '+' || r.peek() == '-') ++r.p;
        if (!is_digit(r.peek())) return false;
        while (is_digit(r.peek())) ++r.p;
    }
    out.kind = Kind::Number;
    out.is_int = is_int;
    out.start = s;
    out.len = static_cast<size_t>(r.p - s);
    return true;
}

bool scan_literal(Reader& r, const char* word, Kind kind, Value& out) {
    size_t n = strlen(word);
    if (static_cast<size_t>(r.end - r.p) < n || memcmp(r.p, word, n) != 0) return false;
    r.p += n;
    out.kind = kind;
    return true;
}

bool scan_value(Reader& r, Value& out, int depth);

// Generic (unknown-key) object or array: validate and discard.
bool skip_container(Reader& r, int depth, Value& out) {
    if (depth >= kMaxDepth) return false;
    bool is_obj = r.peek() == '{';
    ++r.p;
    out.kind = is_obj ? Kind::Object : Kind::Array;
    const char close = is_obj ? '}' : ']';
    r.skip_ws();
    if (r.eat(close)) return true;
    while (true) {
        Value tmp;
        if (is_obj) {
            r.skip_ws();
            if (!scan_string(r, tmp)) return false;
            r.skip_ws();
            if (!r.eat(':')) return false;
        }
        r.skip_ws();
        if (!scan_value(r, tmp, depth + 1)) return false;
        r.skip_ws();
        if (r.eat(close)) return true;
        if (!r.eat(',')) return false;
    }
}

bool scan_value(Reader& r, Value& out, int depth) {
    switch (r.peek()) {
        case '"': return scan_string(r, out);
        case '{':
        case '[': return skip_container(r, depth, out);
        case 't': return scan_literal(r, "true", Kind::True, out);
        case 'f': return scan_literal(r, "false", Kind::False, out);
        case 'n': return scan_literal(r, "null", Kind::Null, out);
        default: return scan_number(r, out);
    }
}

bool str_is(const Value& v, const char* s) {
    return v.kind == Kind::String && v.ascii_ok && v.len == strlen(s) &&
           memcmp(v.text, s, v.len) == 0;
}

// A finite double from a JSON number (int or float). Huge literals that overflow are
// rejected; tiny ones underflow to 0, like Python's float().
bool to_finite(const Value& v, double& out) {
    if (v.kind != Kind::Number) return false;
    char tmp[EYE_MAX_DATAGRAM + 1];
    if (v.len >= sizeof(tmp)) return false;
    memcpy(tmp, v.start, v.len);
    tmp[v.len] = '\0';
    char* endp = nullptr;
    double d = strtod(tmp, &endp);
    if (endp != tmp + v.len) return false;
    if (!isfinite(d)) return false;
    out = d;
    return true;
}

bool valid_seq(const Value& v) {
    if (v.kind != Kind::Number || !v.is_int) return false;
    const char* s = v.start;
    size_t n = v.len;
    if (s[0] == '-') return n == 2 && s[1] == '0';  // -0 is the integer 0
    long long acc = 0;
    for (size_t i = 0; i < n; ++i) {
        acc = acc * 10 + (s[i] - '0');
        if (acc > kSeqMax) return false;
    }
    return true;
}

double clampd(double v, double lo, double hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

// Python's round(): nearest integer, ties to even.
double round_half_even(double v) {
    double f = floor(v);
    double d = v - f;
    if (d > 0.5) return f + 1.0;
    if (d < 0.5) return f;
    return (fmod(f, 2.0) == 0.0) ? f : f + 1.0;
}

}  // namespace

bool parse_command(const char* buf, size_t len, Command& out) {
    if (buf == nullptr || len > EYE_MAX_DATAGRAM) return false;
    if (!valid_utf8(reinterpret_cast<const unsigned char*>(buf), len)) return false;

    Reader r{buf, buf + len};
    Value v, seq, cmd, x, y, ms;  // last occurrence wins

    r.skip_ws();
    if (!r.eat('{')) return false;
    r.skip_ws();
    if (!r.eat('}')) {
        while (true) {
            Value key;
            r.skip_ws();
            if (!scan_string(r, key)) return false;
            r.skip_ws();
            if (!r.eat(':')) return false;
            r.skip_ws();
            Value val;
            if (!scan_value(r, val, 1)) return false;
            if (str_is(key, "v")) v = val;
            else if (str_is(key, "seq")) seq = val;
            else if (str_is(key, "cmd")) cmd = val;
            else if (str_is(key, "x")) x = val;
            else if (str_is(key, "y")) y = val;
            else if (str_is(key, "ms")) ms = val;
            r.skip_ws();
            if (r.eat('}')) break;
            if (!r.eat(',')) return false;
        }
    }
    r.skip_ws();
    if (!r.at_end()) return false;  // trailing data

    // v must be the integer 1 (not 1.0, not true).
    if (!(v.kind == Kind::Number && v.is_int && v.len == 1 && v.start[0] == '1')) return false;
    if (seq.kind != Kind::None && !valid_seq(seq)) return false;

    if (str_is(cmd, "look")) {
        double dx, dy;
        if (!to_finite(x, dx) || !to_finite(y, dy)) return false;
        out.type = CmdType::Look;
        out.x = static_cast<float>(clampd(dx, -1.0, 1.0));
        out.y = static_cast<float>(clampd(dy, -1.0, 1.0));
        out.blink_ms = 0;
        return true;
    }
    if (str_is(cmd, "blink")) {
        double d = kBlinkDefaultMs;
        if (ms.kind != Kind::None && !to_finite(ms, d)) return false;
        d = clampd(d, EyeController::kBlinkMinMs, EyeController::kBlinkMaxMs);
        out.type = CmdType::Blink;
        out.x = out.y = 0.0f;
        out.blink_ms = static_cast<uint16_t>(round_half_even(d));
        return true;
    }
    if (str_is(cmd, "center") || str_is(cmd, "ping")) {
        out.type = str_is(cmd, "center") ? CmdType::Center : CmdType::Ping;
        out.x = out.y = 0.0f;
        out.blink_ms = 0;
        return true;
    }
    return false;
}

void dispatch(const Command& cmd, EyeController& ctrl, uint32_t now_ms) {
    switch (cmd.type) {
        case CmdType::Look: ctrl.look(cmd.x, cmd.y, now_ms); break;
        case CmdType::Blink: ctrl.blink(cmd.blink_ms, now_ms); break;
        case CmdType::Center: ctrl.center(now_ms); break;
        case CmdType::Ping: ctrl.ping(now_ms); break;
    }
}
