// KMBox / MAKCU serial command input for the CDC console.
//
// Existing third-party host tooling drives mouse injection by writing ASCII
// `km.*` calls to a serial port. This module accepts that grammar so such
// tooling can drive THIS device, and turns each accepted command into neutral
// motion/button intents handed to the `kmcmd_ops_t` sink below. It is the
// adapter between a protocol we do not control and a wire contract we cannot
// widen.
//
// --- Portability, and what this file deliberately does NOT include ----------
//
// Like console.c this module is MMIO-free and TinyUSB-free, and the Makefile
// recipe for test-kmcmd omits both include paths so a violation breaks the
// build. It also omits `-isystem include`: kmcmd.c must not reach for the
// generated injection_wire.h. The reason is not tidiness. The wire contract is
// generated from protocol/report_injection_wire.json and carries generations,
// sequence numbers, CRCs and a slot layout; a parser for a foreign protocol
// that could see those would end up encoding one in terms of the other, and
// the next contract regeneration would then be a protocol-compatibility
// change. The sink boundary keeps that from happening: this module speaks
// dx/dy/wheel/pan and a button bitmask, and nothing else.
//
// --- What this device can and cannot express --------------------------------
//
// The FPGA offers RELATIVE (additive: candidate = real_delta + injected),
// BUTTON_STATE (a u64 injected button mask plus hold_reports), PHYSICAL_MASK
// (a u64 button mask and NOTHING ELSE -- there is no motion mask) and CLEAR.
// Three consequences run through everything below:
//
//   * Absolute positioning is not expressible. `moveto()` and `getpos()` are
//     refused rather than approximated: injection is additive, nothing on this
//     link ever reports the cursor position, and there is no motion mask to
//     suppress the physical delta an absolute move would have to override.
//   * Axis locks are not expressible, for the same missing motion mask.
//     `lock_mx` and friends are refused; `lock_ml` and the other button locks
//     map onto PHYSICAL_MASK and work.
//   * There is no per-command ack from the FPGA. It sends back only
//     MAP_STATUS, DESCRIPTOR_FRAGMENT and REPORT_FRAGMENT. So every reply this
//     module produces means "the MCU accepted this command", never "the PC saw
//     the motion". A host cannot get end-to-end confirmation from this link,
//     and no framing choice here can invent one.
//
// --- Why a move is a budget and not a delta ---------------------------------
//
// `km.move(300,450)` is a one-shot displacement. The injected X/Y field is
// 8-bit signed with logical range +/-127 (inj_session.c boot_mouse_entries), and
// the engine rejects a field whose sum leaves that range, passing the report
// through unmodified -- so a single RELATIVE carrying 300 does not clip, it
// vanishes. A move is therefore held as a pending budget and drained in bounded
// steps by kmcmd_step(), one RELATIVE per call, summing exactly to the request.
//
// This also answers the 8 kHz hazard from the other direction. A SUSTAINED
// per-report delta accumulates at delta x 8000 counts/s, which is why
// inj_session.c paces its steady drift at ~40 injections/s. A drained budget is
// not sustained: it is a fixed number of counts that stops when it is spent, so
// `km.move(300,0)` moves 300 counts and then stops, regardless of how fast the
// steps are issued. Rate only decides how long the move takes.

#ifndef HURRA_MCXN947_KMCMD_H
#define HURRA_MCXN947_KMCMD_H

#include <stdbool.h>
#include <stdint.h>

// Largest per-axis magnitude in one emitted RELATIVE.
//
// The mapped field's logical range is +/-127, and the injected value is ADDED to
// whatever the physical mouse reported on the same report. A step of 64 leaves
// 63 counts of headroom, so a step is only rejected when the real device is
// itself moving faster than 63 counts per report. A cap of 127 would instead be
// rejected by any concurrent physical motion at all, which is the normal case
// for a device whose whole purpose is to ride on live traffic.
#define KMCMD_STEP_MAX 64

// Longest reply produced. MAKCU's framing is `km.<payload>\r\n>>>` and every
// payload here is a short echo or a refusal reason, so this is generous.
#define KMCMD_REPLY_MAX 96u

// MAKCU's move() takes an optional segment count, documented default 1 and
// ceiling 512.
#define KMCMD_SEGMENTS_MAX 512u

// Injected button bits. Position matches MAKCU's documented button numbering
// (1=left, 2=right, 3=middle, 4=side1, 5=side2) minus one, and is the mask the
// sink passes to BUTTON_STATE / PHYSICAL_MASK.
typedef enum {
    KMCMD_BUTTON_LEFT = 1u << 0,
    KMCMD_BUTTON_RIGHT = 1u << 1,
    KMCMD_BUTTON_MIDDLE = 1u << 2,
    KMCMD_BUTTON_SIDE1 = 1u << 3,
    KMCMD_BUTTON_SIDE2 = 1u << 4,
} kmcmd_button_t;

// Reply framing. This is the one place the two protocols genuinely conflict and
// cannot be reconciled in a single parser, so it is a mode rather than a
// guess:
//
//   MAKCU specifies a reply for EVERY setter -- "all responses start with km.
//   and end with CRLF followed by the prompt >>>" -- and an echo(0) switch to
//   suppress it. KMBox B+ host libraries are fire-and-forget: they write
//   `km.move(x,y)\r\n` and read nothing back. Emitting MAKCU's ACK at a KMBox
//   host fills its input buffer with bytes it never drains; staying silent at a
//   MAKCU host breaks any client that waits for the prompt.
//
// Refusals are reported in BOTH modes. Silence is a correct answer only for
// something that worked; a silent refusal would make an unimplementable
// command look like a successful one.
typedef enum {
    KMCMD_MODE_OFF = 0,  // km lines fall through to the console's own dispatch
    KMCMD_MODE_MAKCU,    // echo ACK + `>>>` prompt
    KMCMD_MODE_KMBOX,    // silent on accepted setters
} kmcmd_mode_t;

// The sink. Every callback may refuse, and none may block -- the same bargain
// console_ops_t::write makes, and for the same reason: the foreground loop that
// would be stalled is the one running the link's ERR051588 recovery.
//
// --- Wiring this to inj_session (the integrator's one change) ---------------
//
// inj_session.c currently emits a fixed drift (INJ_SESSION_DEFAULT_X/Y paced by
// INJ_SESSION_DEFAULT_PACE). To drive it from here it needs a one-shot request
// slot that inj_session_fill_tx() prefers over the drift:
//
//   in inj_session.h, inside inj_session_t:
//       int16_t req_x, req_y, req_wheel, req_pan;   // pending one-shot RELATIVE
//       uint64_t req_buttons; uint16_t req_hold;    // pending BUTTON_STATE
//       uint8_t req_pending;                        // bit0 relative, bit1 buttons
//
//   in inj_session.h, two new entry points:
//       bool inj_session_request_relative(inj_session_t *s, int16_t x, int16_t y,
//                                         int16_t wheel, int16_t pan);
//       bool inj_session_request_buttons(inj_session_t *s, uint64_t mask,
//                                        uint16_t hold_reports);
//
//   in inj_session_fill_tx(), in the INJ_PHASE_INJECTING arm, ahead of the
//   pace_counter check:
//       if (s->req_pending & 1u) { /* build RELATIVE from req_* */ }
//
// Both request functions return false when a request is already pending, which
// is what makes the one-deep FPGA command queue visible to this module instead
// of overwriting an in-flight command. `ready` is
// `inj_session_phase(s) == INJ_PHASE_INJECTING`, which is exactly the MCU-side
// mirror of gateware.py's command_fresh, and `physical_mask` is the same shape
// as request_buttons against a PHYSICAL_MASK frame.
//
// Until that exists, bind the ops with NULL callbacks: the parser stays fully
// exercised and every motion command is refused with `!nosink`, which is a
// truthful answer rather than a silent no-op.
typedef struct {
    // Queue one one-shot RELATIVE. Each argument is already bounded to
    // +/-KMCMD_STEP_MAX (wheel and pan to +/-1). False means "not now"; the
    // budget is kept and retried on a later kmcmd_step().
    bool (*relative)(void *ctx, int16_t dx, int16_t dy, int16_t wheel, int16_t pan);

    // Set the injected button mask (BUTTON_STATE). `hold_reports` is 0 for a
    // plain state change and non-zero for a click's press half.
    bool (*buttons)(void *ctx, uint32_t mask, uint32_t hold_reports);

    // Set the PHYSICAL_MASK button mask -- which of the real device's buttons
    // are suppressed on the way to the PC.
    bool (*physical_mask)(void *ctx, uint32_t mask);

    // True once the FPGA would honour a command: link up, session active, field
    // map committed and the active generation matching. A NULL `ready` is NOT
    // treated as ready -- it means nothing is wired yet.
    bool (*ready)(void *ctx);

    void *ctx;
} kmcmd_ops_t;

typedef enum {
    // Not a km command. The console must carry on with its own dispatch, so
    // `stats` and friends keep working.
    KMCMD_NOT_MINE = 0,
    // Parsed as a km command -- accepted or refused, either way answered here.
    KMCMD_HANDLED,
} kmcmd_verdict_t;

// Bind the sink and clear all parser state. Safe to call again.
void kmcmd_init(const kmcmd_ops_t *ops);

void kmcmd_set_mode(kmcmd_mode_t mode);
kmcmd_mode_t kmcmd_mode(void);

// Reports per millisecond, used only to turn a click's requested hold in
// milliseconds into BUTTON_STATE.hold_reports. This module has no clock, so the
// rate is declared rather than measured: 8 is the High Speed 8 kHz figure the
// injection plane is designed around, and 1 is the Full Speed case. Setting it
// to 0 makes a click with an explicit delay refuse instead of guessing.
void kmcmd_set_reports_per_ms(uint32_t reports_per_ms);

// Try to handle one edited console line. `reply` receives a NUL-terminated
// response, empty when the mode calls for silence; it is truncated rather than
// overrun when short, and `reply_size` of 0 writes nothing.
kmcmd_verdict_t kmcmd_line(const char *line, char *reply, uint32_t reply_size);

// Transport up/down, from the same edge that drives inj_session_set_link(). A
// falling edge discards any pending budget: the FPGA tears down its session and
// RX window with the link, so queued counts are void and replaying them into a
// fresh session would be a stale move.
void kmcmd_set_link(bool up);

// Drain one step of pending motion or one half of a pending click. Call from
// the foreground loop beside console_flood_step(). Emits at most one sink call
// per invocation -- an unbounded drain would starve link_poll() for the length
// of a large move. Returns true if it emitted something.
bool kmcmd_step(void);

// True while a move, scroll or click still has work left.
bool kmcmd_pending(void);

// Total undrained displacement across all four axes, in counts. kmcmd_pending()
// cannot tell a budget that is draining from one that is stuck; this can.
uint32_t kmcmd_pending_counts(void);

// Commands accepted, commands refused, and sink calls the sink declined.
uint32_t kmcmd_accepted(void);
uint32_t kmcmd_refused(void);
uint32_t kmcmd_dropped(void);

#endif  // HURRA_MCXN947_KMCMD_H
