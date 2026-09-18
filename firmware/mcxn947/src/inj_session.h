// Injection session: the MCU-side policy that turns a live, enumerated boot
// mouse into injected motion on the wire. It is the driver that sits above the
// transport (link.c / link_retire.c) and the frame builders (inj_command.c).
//
// The FPGA only acts on a RELATIVE command when a field map is committed and
// active and the command cites that map's generation (gateware.py command_fresh:
// link_ready & session_active & map_store.active_valid & rx_map_generation ==
// active_generation). So this session enforces the mandatory order:
//
//   WAIT_LINK -> WAIT_REPORT -> SEND_BEGIN/ENTRY/COMMIT -> WAIT_COMMIT -> INJECTING
//
// It learns the target report's addressing (interface / endpoint / report_id /
// length) and the descriptor generation from an observed REPORT_FRAGMENT rather
// than hardcoding them, uploads a fixed boot-mouse field map (X at byte 1, Y at
// byte 2 -- the standard [buttons, X, Y, wheel] layout), waits for the FPGA's
// MAP_STATUS to confirm the commit, then emits RELATIVE motion to fight the
// physical device. Everything here is portable and MMIO-free; link.c calls
// inj_session_fill_tx() when it would otherwise stage an IDLE slot, and feeds
// observed telemetry in through the observe_* entry points.
//
// The injected motion is intentionally static (a steady counter-drift, tunable
// below): the goal of this first milestone is to prove injection reaches live
// traffic, not to servo the cursor, and a fixed value keeps the effect and the
// tests deterministic.

#ifndef INJ_SESSION_H
#define INJ_SESSION_H

#include <stdbool.h>
#include <stdint.h>

#include "injection_wire.h"

// The fixed boot-mouse map has two injected axes, X then Y.
#define INJ_SESSION_MAP_ENTRIES 2u

// Steady injected deltas, added to every targeted report's X/Y: a slow
// up-and-left drift laid over the test mouse's motion. Additive with per-field
// overflow rejection, so an out-of-range sum simply passes that report through.
// Kept small on purpose -- see the pace note for why, at 8 kHz, small is fast.
#define INJ_SESSION_DEFAULT_X (-2)
#define INJ_SESSION_DEFAULT_Y (-2)

// Emit one RELATIVE every Nth fill_tx opportunity. At 8 kHz any sustained delta
// accumulates hard: a period of 4 would be ~2000 injections/s (~4000 counts/s per axis)
// and fling the cursor off-screen. Period 200 is ~40/s, so the -2 deltas above
// give ~-80 counts/s per axis -- a gentle drift comparable to the slow-circle
// test mouse, so the injection visibly pushes the cursor rather than blurring
// it. Still far below the FPGA's one-deep command queue retire rate.
#define INJ_SESSION_DEFAULT_PACE 200u

typedef enum {
    INJ_PHASE_WAIT_LINK = 0,  // Transport down; nothing to send.
    INJ_PHASE_WAIT_REPORT,    // Up; learning addressing from a REPORT_FRAGMENT.
    INJ_PHASE_SEND_BEGIN,     // Next fill emits MAP_BEGIN.
    INJ_PHASE_SEND_ENTRY,     // Next fill emits the entry at entry_cursor.
    INJ_PHASE_SEND_COMMIT,    // Next fill emits MAP_COMMIT.
    INJ_PHASE_WAIT_COMMIT,    // Awaiting MAP_STATUS commit acceptance.
    INJ_PHASE_INJECTING,      // Emitting RELATIVE motion.
} inj_phase_t;

typedef struct {
    inj_phase_t phase;

    // Learned from the first usable REPORT_FRAGMENT.
    uint16_t descriptor_generation;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
    uint8_t report_length;

    // Map identity. map_generation is what we propose; it increments on every
    // (re)attempt so a retry can never be mistaken for the rejected map.
    uint16_t map_generation;
    uint16_t active_map_generation;

    // Sequencing. tx_frame_seq is the link-level frame sequence (slot byte 2)
    // and only advances on emitted command frames; IDLE keeps sequence 0 and is
    // filtered by the FPGA before its RX window. command_sequence is the
    // injection-level counter inside the RELATIVE payload.
    uint8_t tx_frame_seq;
    uint16_t command_sequence;
    uint8_t entry_cursor;

    // Injection pattern and pacing.
    uint16_t pace_period;
    uint16_t pace_counter;
    int16_t inject_x;
    int16_t inject_y;

    // Diagnostics (monotonic; cleared only by init).
    uint32_t maps_committed;
    uint32_t map_rejections;
    uint32_t relatives_sent;
} inj_session_t;

// Reset to WAIT_LINK and load the default pattern/pacing.
void inj_session_init(inj_session_t *s);

// Transport up/down, driven from link.c's mcu_ready. A rising edge arms map
// upload (WAIT_REPORT); a falling edge drops all learned state back to
// WAIT_LINK, since the FPGA tears its session and RX window down with the link.
void inj_session_set_link(inj_session_t *s, bool up);

// Feed one observed FPGA->MCU telemetry frame. Non-matching phases ignore it.
void inj_session_observe_report(inj_session_t *s, const inj_report_fragment_payload_t *frag);
void inj_session_observe_map_status(inj_session_t *s, const inj_map_status_payload_t *status);

// Stage the next TX slot. Returns true and writes a command frame into `slot`
// when the session has one to send this slot; returns false when the caller
// should stage an IDLE keepalive instead.
bool inj_session_fill_tx(inj_session_t *s, uint8_t slot[INJ_FRAME_SIZE]);

static inline inj_phase_t inj_session_phase(const inj_session_t *s)
{
    return s->phase;
}

#endif  // INJ_SESSION_H
