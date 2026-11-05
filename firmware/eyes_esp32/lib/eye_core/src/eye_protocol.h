// Eye command datagram parser (docs/eye-protocol.md). Plain C++17: no Arduino, no heap,
// no exceptions, no third-party JSON library. Accepts and rejects exactly what
// brain/eye_protocol.py decode() accepts and rejects, with the same clamping.
#ifndef EYE_CORE_EYE_PROTOCOL_H
#define EYE_CORE_EYE_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#include "eye_controller.h"

// Must match brain/eye_protocol.py (DEFAULT_PORT, MAX_DATAGRAM). Checked by pytest.
#define EYE_UDP_PORT 4210
#define EYE_MAX_DATAGRAM 128

enum class CmdType : uint8_t { Look, Blink, Center, Ping };

struct Command {
    CmdType type;
    float x, y;         // Look only: clamped to [-1, 1]
    uint16_t blink_ms;  // Blink only: clamped to [50, 1000], rounded half to even, default 150
};

// Parse one datagram (not necessarily NUL-terminated). Returns false, leaving `out`
// unspecified, if the datagram is invalid per docs/eye-protocol.md. Invalid datagrams must
// be ignored and must not reset the fail-safe timer.
bool parse_command(const char* buf, size_t len, Command& out);

// Call the EyeController method matching `cmd`.
void dispatch(const Command& cmd, EyeController& ctrl, uint32_t now_ms);

#endif  // EYE_CORE_EYE_PROTOCOL_H
