// The FPGA injection link: LPSPI6 as an SPI slave on LP_FLEXCOMM6, fed by a
// self-loading eDMA0 scatter-gather ring.
//
// Migration step 3 of docs/MCXN947_CONTROLLER.md section 9. TX is still
// permanently IDLE -- both banks are seeded with a complete IDLE keepalive and
// nothing ever refills them, so the same inert slot is clocked out forever;
// originating commands is step 8/9. What step 3 adds is the other direction
// and the fault path: received banks are retired through link_retire.c, and
// an ERR051588 transmit underrun is detected and recovered through
// link_recovery.c's ladder.
//
// Declarations here are portable C. Only the definitions in link.c that touch
// MMIO are inside `#if defined(MCXN947)`, so the transport predicates below
// compile and are tested on the host.
//
// --- Why mcu_ready is RAISED at this step, not held low ---------------------
//
// Design doc section 9 step 2 gates on "every `spi_bad_*` and `spi_queue_full`
// flat while `link_ready = 1`, `spi_slots` advancing at 8 kHz, and
// `map_active` never leaving 0". The `link_ready = 1` term is the load-bearing
// one and it is why this firmware raises the line rather than holding it low:
//
//   1. `spi_slots` does not depend on us at all. `slot_counter` is incremented
//      on the fixed cadence boundary in src/hurra_cynthion/spi_link.py:281, in
//      a block whose own comment reads "A fixed slot boundary is generated
//      independently of transfer state" -- it sits outside the `if ~active`
//      arm. It advances at 8 kHz with no MCU attached.
//
//   2. With `mcu_ready` low, no error counter CAN move. `transfer_ready` is
//      latched from `mcu_ready` at slot start (spi_link.py:289) and every one
//      of the four `bad_*` counters is gated on it (`if transfer_ready &
//      (rx_sof != SOF)` at :433, and so on down the Elif chain).
//      `rx_queue_full` is gated on `valid_return`, whose first term is
//      `transfer_ready`. So with the line low the FPGA never validates a
//      returned slot and a flat counter asserts nothing.
//
// Raising it turns `transfer_ready` on and makes all five counters
// load-bearing. Safety does not come from the ready line; it comes from the
// wire contract. `valid_return` ends with `& (rx_type != INJ_TYPE_IDLE)`
// (spi_link.py:183), so a slot whose type byte is INJ_TYPE_IDLE is never
// deliverable however well-formed it is: `rx_valid` is never asserted, nothing
// reaches the injection map, and `map_active` cannot leave 0. An inert slot is
// safe *because* the FPGA validates it, not because it declines to look.
//
// `link_slot_is_inert_idle()` below is that argument as a predicate, and
// test/link_test.c is the argument as a test.

#ifndef HURRA_MCXN947_LINK_H
#define HURRA_MCXN947_LINK_H

#include <stdbool.h>
#include <stdint.h>

#include "injection_wire.h"
#include "link_retire.h"
#include "spi_frame.h"

// Banks per direction. Two, so the ring alternates and the CPU can touch the
// bank the DMA is not pointing at -- the steady-state invariant in design doc
// section 4. Step 3 is the step that needs the rule, and it obeys it by
// deriving the live bank from the TCD rather than tracking it in software; see
// link_retire.h. Power of two, because link_next_bank() masks rather than
// divides.
#define LINK_SLOT_BANKS 2u
_Static_assert((LINK_SLOT_BANKS & (LINK_SLOT_BANKS - 1u)) == 0u,
               "LINK_SLOT_BANKS must be a power of two");

// One slot is moved as 32-bit words: eDMA minor loop of one word per LPSPI
// FIFO request, major loop of eight. A 32-bit access width is not a
// preference -- it is what makes TCR[BYSW] the setting that decides byte
// order, and the two must be reasoned about together (see link.c).
#define LINK_DMA_MINOR_BYTES 4u
#define LINK_DMA_MAJOR_ITER (INJ_FRAME_SIZE / LINK_DMA_MINOR_BYTES)
_Static_assert(INJ_FRAME_SIZE % LINK_DMA_MINOR_BYTES == 0u,
               "a 32-byte slot must divide into whole 32-bit DMA transfers");
_Static_assert(LINK_DMA_MINOR_BYTES * LINK_DMA_MAJOR_ITER == INJ_FRAME_SIZE,
               "the eDMA major loop must move exactly one slot");

// TCR[FRAMESZ] is "bits per frame minus one". The FPGA holds CS low for one
// 256-bit slot, so the frame is the whole slot and the two must agree exactly:
// a short FRAMESZ would restart the frame mid-slot and shift every later byte.
#define LINK_SPI_FRAMESZ (INJ_FRAME_SIZE * 8u - 1u)
_Static_assert(LINK_SPI_FRAMESZ == 255u, "the FPGA clocks 256 bits per CS");

// --- Portable transport predicates (host-tested) ---------------------------

// Write the IDLE keepalive this step transmits forever: SOF, INJ_TYPE_IDLE,
// sequence 0, length 0, zero payload, correct CRC-16. Always writes all
// INJ_FRAME_SIZE bytes.
void link_build_idle_slot(uint8_t slot[INJ_FRAME_SIZE]);

// True when `slot` is exactly what step 2 promises the FPGA: well formed
// enough that no `spi_bad_*` counter can move, and IDLE, so `valid_return` is
// false and it can never be delivered.
//
// This is not an approximation of the gateware predicate; it is the same
// conjunction. spi_frame_unpack() returns SPI_FRAME_IDLE only when SOF matched,
// the type is one the contract assigns, the length equals the per-type exact
// length, the CRC verifies, AND the type is INJ_TYPE_IDLE -- which is
// term-for-term `transfer_ready & (rx_sof == SOF) & known_type &
// (rx_length == expected_length) & (rx_received_crc == rx_crc)` holding while
// `(rx_type != INJ_TYPE_IDLE)` fails.
bool link_slot_is_inert_idle(const uint8_t slot[INJ_FRAME_SIZE]);

// Successor bank in the ring.
uint8_t link_next_bank(uint8_t bank);

// --- Diagnostics the FPGA cannot see ---------------------------------------
//
// The counters in link_retire.h mirror the FPGA's own by name and are the
// half of the picture both ends can state. These are the other half: things
// only this side of the wire knows, and the ones a `stats` reader needs in
// order to tell "the FPGA is quiet" from "we stopped listening".
//
// Split out rather than added to link_retire_counters_t because that struct
// is the mirror, and putting an MCU-only field in it would make a
// field-by-field comparison against the FPGA read as a mismatch.
typedef struct {
    uint32_t isr_entries;         // eDMA0 channel 1 major-loop ISR entries.
    uint32_t retire_stalls;       // ISR entries that retired no bank.
    uint32_t daddr_out_of_range;  // TCD destination outside the RX bank array.
    uint32_t recoveries;          // ERR051588 ladder runs, all causes.
    uint32_t framing_recoveries;  // Of those, ones triggered by lost framing.
    uint32_t gap_wait_timeouts;   // link_spi_enable_aligned() gave up waiting.
    bool ready;                   // Last value driven onto `mcu_ready`.
} link_diagnostics_t;

// Both reads below mask the retirement interrupt for the duration of the
// copy. That is not defensive tidiness: the ISR advances these at 8 kHz, and
// a console that formatted them field by field straight from the live
// structures would print a sample from several different slots and call it
// one -- the same class of manufactured evidence link_retire_last_slot()
// documents, and it would be far harder to spot here because every individual
// number would look plausible.
void link_diagnostics_read(link_diagnostics_t *out);
void link_counters_read(link_retire_counters_t *out);

// --- Hardware entry points (target only) -----------------------------------

// Mux the mikroBUS J6 pins and `mcu_ready`, seed both TX banks, build and arm
// the eDMA0 scatter-gather rings, enable LPSPI6, and only then raise
// `mcu_ready`. Ordering is the safety invariant's boot ladder and is not an
// implementation detail; see link.c.
void link_init(void);

// Drive `mcu_ready`. Called by link_init() at both ends of the boot ladder,
// and by the first and last rungs of the ERR051588 recovery.
void link_mcu_ready_set(bool ready);

// Foreground service. Retirement itself runs in the eDMA0 channel 1 major-loop
// ISR -- at 8 kHz a foreground that also prints would miss whole rotations of a
// two-bank ring -- so this call does the things that tolerate latency: sample
// the fault status and recover if ERR051588 (or a DMA error) has fired, drive
// the one-shot fault provocation this step exists to prove the recovery with,
// and emit the periodic counter report on the debug UART.
//
// Never blocks on the link. Safe to call as fast as the foreground loop turns.
void link_poll(void);

// --- Driving injection from the foreground loop -----------------------------
//
// The injection session lives here because link_drain_rx() -- the 8 kHz
// retirement interrupt -- is what stages its TX frames. These two wrappers are
// how foreground code (kmcmd, via usb_console.c) reaches it without touching
// ISR-shared state directly: the request fields and their pending flag are only
// consistent when written together, so the write happens with the retirement
// interrupt masked. Binding kmcmd straight to inj_session_request_relative()
// would look identical and be torn by the next slot.
//
// Queue one one-shot RELATIVE. False means "not now" -- a request is already in
// flight, or the session is not in a state the FPGA would honour a command in.
// The caller keeps its budget and retries. See inj_session.h for why an
// oversized field vanishes rather than clipping, and kmcmd.h for the step cap.
bool link_inject_request_relative(int16_t x, int16_t y, int16_t wheel, int16_t pan);

// Queue the injected button mask, or which of the real device's buttons are
// suppressed. All three requests share the session's single slot, so a pending
// motion step refuses a button request and vice versa -- the queue is one deep.
bool link_inject_request_buttons(uint64_t mask, uint16_t hold_reports);
bool link_inject_request_physical_mask(uint64_t button_mask);

// True while the FPGA would honour an injection command: the MCU-side mirror of
// gateware.py's command_fresh (link up, session active, map committed and the
// active generation matching). Leaving this state voids any queued budget.
bool link_inject_ready(void);

#endif  // HURRA_MCXN947_LINK_H
