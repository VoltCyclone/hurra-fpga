// Injection session: the MCU-side policy that turns a live, enumerated mouse
// into injected motion on the wire. It is the driver that sits above the
// transport (link.c / link_retire.c) and the frame builders (inj_command.c).
//
// The FPGA only acts on a RELATIVE command when a field map is committed and
// active and the command cites that map's generation (gateware.py command_fresh:
// link_ready & session_active & map_store.active_valid & rx_map_generation ==
// active_generation). So this session enforces the mandatory order:
//
//   WAIT_LINK -> WAIT_DESCRIPTOR -> WAIT_REPORT -> SEND_BEGIN/ENTRY/COMMIT
//             -> WAIT_COMMIT -> INJECTING
//
// The map is COMPILED from the device's own report descriptor, never assumed.
// Enumeration accepts any HID device, and a fixed boot-mouse map laid over a
// keyboard types keys, over a game pad moves a stick, and over a report-ID or
// 16-bit mouse lands in the wrong bytes. The foreground compiles each
// interface's descriptor (hid_mouse_layout.c) and offers the verdict here; only
// an interface inside a Generic Desktop / Mouse application ever gets a map. A
// device with none ends in NO_MOUSE, which kmcmd reports as `nomouse`.
//
// Addressing the descriptor cannot supply -- the endpoint, and the length the
// device actually sends -- is learned from the first matching REPORT_FRAGMENT.
// Everything here is portable and MMIO-free; link.c calls inj_session_fill_tx()
// when it would otherwise stage an IDLE slot, and feeds observed telemetry in
// through the observe_* entry points.
//
// The injected drift is intentionally static (a steady counter-drift, tunable
// below): it proves injection reaches live traffic, and a fixed value keeps the
// effect and the tests deterministic.

#ifndef INJ_SESSION_H
#define INJ_SESSION_H

#include <stdbool.h>
#include <stdint.h>

#include "hid_mouse_layout.h"
#include "injection_wire.h"

// How long descriptor traffic must be quiet, in retired slots (~8 kHz, so
// ~64 ms), before a device whose every completed descriptor was judged "not a
// mouse" is declared NO_MOUSE. The FPGA sends no "export finished" marker, and a
// composite device's mouse descriptor may still be in flight behind its
// keyboard's; declaring earlier would answer `nomouse` for a mouse.
#define INJ_SESSION_SETTLE_SLOTS 512u

// How long to wait for the FPGA's MAP_STATUS after MAP_COMMIT, in retired
// slots (~0.5 s). It normally answers within a few slots; a lost status frame
// would otherwise park the session in WAIT_COMMIT until the link dropped.
#define INJ_SESSION_COMMIT_TIMEOUT_SLOTS 4096u

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

// Which kind of command occupies the single request slot. All three share it
// because the FPGA's command queue is one deep.
#define INJ_SESSION_REQ_NONE 0u
#define INJ_SESSION_REQ_RELATIVE 1u
#define INJ_SESSION_REQ_BUTTONS 2u
#define INJ_SESSION_REQ_PHYSICAL_MASK 3u

typedef enum {
    INJ_PHASE_WAIT_LINK = 0,   // Transport down; nothing to send.
    INJ_PHASE_WAIT_DESCRIPTOR, // Up; no mouse layout offered yet, verdict pending.
    INJ_PHASE_NO_MOUSE,        // Every completed descriptor judged; none injectable.
    INJ_PHASE_WAIT_REPORT,     // Layout adopted; waiting for one of its reports.
    INJ_PHASE_SEND_BEGIN,      // Next fill emits MAP_BEGIN.
    INJ_PHASE_SEND_ENTRY,      // Next fill emits the entry at entry_cursor.
    INJ_PHASE_SEND_COMMIT,     // Next fill emits MAP_COMMIT.
    INJ_PHASE_WAIT_COMMIT,     // Awaiting MAP_STATUS commit acceptance.
    INJ_PHASE_INJECTING,       // Emitting RELATIVE motion.
} inj_phase_t;

typedef struct {
    inj_phase_t phase;

    // The device the session is working against. A DESCRIPTOR_FRAGMENT of
    // another generation means it re-enumerated: everything learned is dropped.
    // Reports of another generation are ignored (see adopt_generation).
    uint16_t descriptor_generation;
    bool have_generation;

    // The adopted mouse layout and the interface it came from. Composite
    // devices: the FIRST interface offered with a mouse layout wins.
    hid_mouse_layout_t layout;
    bool have_layout;
    // Descriptor-set slots (hid_descriptor_set.h), not interface numbers:
    uint8_t complete_mask;  // slots whose descriptor completed this generation
    uint8_t judged_mask;    // ... and have been offered a verdict
    uint16_t settle_slots;  // quiet descriptor traffic, while WAIT_DESCRIPTOR
    uint16_t commit_wait_slots;  // time in WAIT_COMMIT
    bool no_mouse_verdict;  // NO_MOUSE was reached this generation; kept across link drops

    // Addressing of the target report: the interface the layout came from, and
    // the endpoint its report was seen on (the descriptor does not say). The
    // report ID is the layout's.
    uint8_t interface_number;
    uint8_t endpoint_number;

    // The map being uploaded, built once per attempt (and its CRC with it).
    inj_map_entry_payload_t entries[HID_MOUSE_MAX_FIELDS];
    uint8_t entry_count;
    uint32_t entries_crc32;

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

    // Injection pattern and pacing. inject_x/y of (0,0) silences the paced
    // RELATIVE entirely rather than emitting an additive no-op -- see
    // inj_session_set_drift.
    uint16_t pace_period;
    uint16_t pace_counter;
    int16_t inject_x;
    int16_t inject_y;

    // One-deep external command request: written by whoever drives injection
    // from outside (the foreground loop, via kmcmd) and consumed by
    // inj_session_fill_tx(), which runs in the 8 kHz RX ISR. It is ONE slot
    // because the FPGA's command queue is one deep, so a second pending
    // request would have nowhere to go; see inj_session_request_relative.
    //
    // This field is touched from two contexts. The writer must mask the
    // retirement interrupt around the whole request, because a request is only
    // consistent once every req_* field and the kind tag are written together.
    //
    // req_pending is a KIND, not a bitmask: all three request kinds share the
    // one slot, so at most one can ever be in flight and a bitmask would imply
    // otherwise.
    int16_t req_x;
    int16_t req_y;
    int16_t req_wheel;
    int16_t req_pan;
    uint64_t req_buttons;
    uint64_t req_button_mask;
    uint16_t req_hold_reports;
    uint8_t req_pending;  // INJ_SESSION_REQ_*

    // Diagnostics (monotonic; cleared only by init).
    uint32_t requests_sent;
    uint32_t requests_refused;
    uint32_t maps_committed;
    uint32_t map_rejections;
    uint32_t relatives_sent;
    // The device's report was a different length from its descriptor's figure:
    // adopted when it still reaches every mapped field, refused otherwise. The
    // FPGA binds a layout only on an exact length match, so a silent mismatch
    // would be a map that commits and never applies.
    uint32_t length_overrides;
    uint32_t length_conflicts;
    // Requested wheel/pan components dropped because the mouse has no such axis.
    uint32_t axis_drops;
    // BUTTON_STATE / PHYSICAL_MASK requests dropped because the layout maps no
    // buttons.
    uint32_t button_drops;
    // MAP_COMMITs abandoned for want of a MAP_STATUS.
    uint32_t commit_timeouts;
} inj_session_t;

// Reset to WAIT_LINK and load the default pattern/pacing.
void inj_session_init(inj_session_t *s);

// Transport up/down, driven from link.c's mcu_ready. A falling edge returns to
// WAIT_LINK and voids everything tied to the FPGA's session (map, sequences,
// queued request), since the FPGA tears both down with the link. The layout and
// the NO_MOUSE verdict are kept: the FPGA re-exports the same generation's
// descriptors on link-up, which are not new completions and are never
// re-judged. A rising edge resumes at WAIT_REPORT, NO_MOUSE or WAIT_DESCRIPTOR.
void inj_session_set_link(inj_session_t *s, bool up);

// Feed one observed FPGA->MCU telemetry frame. Non-matching phases ignore it.
void inj_session_observe_report(inj_session_t *s, const inj_report_fragment_payload_t *frag);
void inj_session_observe_map_status(inj_session_t *s, const inj_map_status_payload_t *status);

// One DESCRIPTOR_FRAGMENT was retired (ISR context) into descriptor-set slot
// `slot`. `complete_edge` is true when it completed that slot's descriptor
// (hid_descriptor_set_push()).
void inj_session_observe_descriptor(inj_session_t *s, uint16_t generation, uint8_t slot,
                                    bool complete_edge);

// Reconcile with what the descriptor set holds for `generation` (foreground,
// masked): `complete_now` has bit i set when slot i's descriptor is complete.
// A slot that is no longer complete stops counting as one the verdict must
// wait for, and loses any judgement it had.
void inj_session_sync_descriptors(inj_session_t *s, uint16_t generation, uint8_t complete_now);

// The foreground's verdict on one slot's compiled descriptor, which belongs to
// interface `interface_number`; call it with the retirement ISR masked.
// `layout` is NULL for "not injectable". A verdict for any generation but the
// current one is stale and ignored.
void inj_session_offer_layout(inj_session_t *s, uint16_t generation, uint8_t slot,
                              uint8_t interface_number, const hid_mouse_layout_t *layout);

// Stage the next TX slot. Returns true and writes a command frame into `slot`
// when the session has one to send this slot; returns false when the caller
// should stage an IDLE keepalive instead.
bool inj_session_fill_tx(inj_session_t *s, uint8_t slot[INJ_FRAME_SIZE]);

// Queue one one-shot RELATIVE, to be emitted on the next TX slot ahead of the
// paced drift. Each argument is added to whatever the physical device reports
// on the same report, within the mapped field's logical range (at least
// +/-127 for X/Y, see hid_mouse_layout.c) -- an out-of-range sum is REJECTED by
// the engine and the report passes through unmodified, so an oversized value
// does not clip, it vanishes. A wheel or pan the mouse does not have is dropped
// at emission and counted in axis_drops. Callers must
// therefore split a large displacement into bounded steps; kmcmd.h explains the
// step cap it uses and why.
//
// Returns false, and queues nothing, when a request is already pending or the
// session is not INJECTING. Both are refusals rather than overwrites: the FPGA
// honours a command only while command_fresh holds (link up, session active,
// map committed, generation matching), and its queue is one deep. A caller that
// is draining a budget should keep the count and retry on its next step.
//
// Not reentrant, and the callers are in different contexts -- see req_pending.
bool inj_session_request_relative(inj_session_t *s, int16_t x, int16_t y,
                                  int16_t wheel, int16_t pan);

// Queue the injected button mask (BUTTON_STATE). `hold_reports` is 0 for a
// plain state change and non-zero for a click's press half -- it is the only
// self-timing field on this link, because engine.button_hold_reports is the
// ONLY hold_reports the gateware wires (RELATIVE's is defined in the contract
// and connected to nothing, so one RELATIVE affects exactly one report).
//
// A mask of 0 is a real command, not an empty one: it is how a held button is
// released. Unlike the RELATIVE path, this is never dropped as a no-op.
bool inj_session_request_buttons(inj_session_t *s, uint64_t mask, uint16_t hold_reports);

// Queue which of the REAL device's buttons are suppressed on the way to the PC
// (PHYSICAL_MASK). Buttons only -- the contract has no motion mask, which is
// why axis locks cannot be expressed on this link at all.
bool inj_session_request_physical_mask(inj_session_t *s, uint64_t button_mask);

// True while a queued request has not yet been emitted. All three request kinds
// share the one slot, so this is what a caller checks before queuing any of
// them.
bool inj_session_pending_request(const inj_session_t *s);

// Replace the steady drift. (0,0) silences the paced RELATIVE completely
// instead of emitting a command whose every field adds zero: that would spend a
// slot and a command sequence number, and burn the FPGA's one-deep queue
// against an externally driven move that actually wants it. Requests are
// unaffected -- this silences the demo pattern, not the session.
void inj_session_set_drift(inj_session_t *s, int16_t x, int16_t y);

static inline inj_phase_t inj_session_phase(const inj_session_t *s)
{
    return s->phase;
}

// The attached device is known to have nothing to inject into.
static inline bool inj_session_no_mouse(const inj_session_t *s)
{
    return s->phase == INJ_PHASE_NO_MOUSE;
}

#endif  // INJ_SESSION_H
