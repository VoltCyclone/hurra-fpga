// The CDC console: line editing and command dispatch.
//
// Migration step 4 of docs/MCXN947_CONTROLLER.md section 9 introduced this
// portable half; step 5 extends its stats with observed CPU1 liveness.
// Nothing in this module touches MMIO or the USB stack -- it speaks to both
// through the `console_ops_t` callbacks below -- so it host-compiles and
// test/console_test.c drives every command with a captured output buffer.
// usb_console.c is the guarded half that supplies the callbacks and pumps
// TinyUSB.
//
// Design doc section 7 says the two genuinely new modules of this migration
// are "100% portable by construction". This is one of them, and the split is
// what makes that true: a command parser that reached for tud_cdc_write()
// directly would be untestable for the same reason it would be convenient.
//
// --- Why the writer may drop -----------------------------------------------
//
// console_ops_t::write is allowed to accept fewer bytes than it is given and
// is REQUIRED never to block. The console is a diagnostic; the link is the
// product. A writer that spun waiting for a host to drain the CDC FIFO would
// stall the foreground loop, and the foreground loop is what runs ERR051588
// recovery -- so an unplugged or wedged terminal would disable the link's
// fault path. Dropping is visible (`txdrop` in `stats`) and costs nothing the
// link can observe; blocking is invisible until it matters.
//
// This is the same argument the gateware's relay makes for itself
// (src/hurra_cynthion/relay.py): admit or drop whole units, never
// backpressure, and count the drops.

#ifndef HURRA_MCXN947_CONSOLE_H
#define HURRA_MCXN947_CONSOLE_H

#include <stdbool.h>
#include <stdint.h>

// Longest command line accepted. Anything longer is truncated at the limit
// and the line is still dispatched -- an overlong line is a typo or line
// noise, and silently discarding it would look like the console had hung.
#define CONSOLE_LINE_MAX 63u

// Everything `stats` reports. Flat, by value, and a copy: the provider takes
// the snapshot with the retirement interrupt masked, so the console never
// reads counters that an ISR is mid-update on.
//
// The first eleven mirror link_retire_counters_t field for field and by name,
// because the gate for this step is comparing them against the FPGA's own
// registers over JTAG. Renaming one here to read more nicely would break the
// only thing they are for.
typedef struct {
    uint32_t slots;
    uint32_t idle;
    uint32_t deliverable;
    uint32_t bad_sof;
    uint32_t bad_crc;
    uint32_t bad_length;
    uint32_t bad_type;
    uint32_t duplicate;
    uint32_t stale;
    uint32_t sequence_gap;
    uint32_t ring_drop;

    // link.c's own view of the transport, which the FPGA cannot see.
    uint32_t isr_entries;
    uint32_t retire_stalls;
    uint32_t daddr_out_of_range;
    uint32_t recoveries;
    uint32_t framing_recoveries;
    uint32_t gap_wait_timeouts;

    uint32_t uptime_ms;
    uint32_t cpu1_boot_count;
    uint32_t cpu1_heartbeat;
    bool link_ready;
    bool cpu1_alive;
    bool cpu1_released;
    bool cpu1_held_in_reset;
} console_stats_t;

typedef struct {
    // Accepts up to `length` bytes, returns how many it took. Must not block.
    uint32_t (*write)(void *ctx, const char *data, uint32_t length);
    void (*stats)(void *ctx, console_stats_t *out);

    // Stop CPU1, and start it again. Both may be NULL on a build with no CPU1
    // (and are on the host), in which case the commands report that rather
    // than pretending to work.
    //
    // Design doc section 3 requires re-release to sit behind an explicit
    // console command: an automatic one "turns a display bug into a
    // self-concealing reset loop". `cpu1halt` is its counterpart and is the
    // instrument section 9 step 5 configuration (c) is measured with.
    void (*cpu1_halt)(void *ctx);
    void (*cpu1_start)(void *ctx);
    void *ctx;
} console_ops_t;

// Bind the callbacks and clear all console state. Safe to call again on a
// re-connect.
void console_init(const console_ops_t *ops);

// Feed received bytes. Handles echo, backspace and CR/LF, and dispatches a
// command on end of line.
void console_feed(const uint8_t *data, uint32_t length);

// Print the banner and a prompt. Called when a host opens the port.
void console_greet(void);

// --- Bulk load generator ---------------------------------------------------
//
// `flood` exists for one measurement: design doc section 9 step 4 requires the
// step-2 link gate to be re-run UNDER BULK CONSOLE LOAD, because that is the
// empirical test of section 3's decision to put the console on CPU0. Without
// a way to saturate the IN direction from the device side, "bulk load" would
// mean whatever the operator's terminal happened to be doing.
//
// It is not debug scaffolding to be removed: it is the instrument the
// core-placement claim is measured with, and any later change to CPU0's load
// should be re-measured the same way.
bool console_flood_active(void);

// Emit one chunk if flooding. Call from the foreground loop. Stops on its own
// when the writer stops accepting, so it fills the pipe rather than spinning.
void console_flood_step(void);

// Bytes the flood generator has handed to the writer since it was started.
uint32_t console_flood_bytes(void);

// Bytes the writer refused. Reported by `stats` as `txdrop`.
uint32_t console_dropped_bytes(void);

// --- Formatting, exposed for the host test ---------------------------------

// Unsigned decimal into `out`, NUL terminated. Returns the digit count.
// `out` must hold at least 11 bytes.
uint32_t console_format_u32(uint32_t value, char *out);

#endif  // HURRA_MCXN947_CONSOLE_H
