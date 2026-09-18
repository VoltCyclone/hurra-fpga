// ERR051588 recovery: the fault predicate and the ordered ladder.
//
// The erratum, live on both mask sets on disk and carrying no "fixed in" note:
//
//   Transmit FIFO pointers are corrupted when a transmit FIFO underrun occurs
//   (SR[TEF]) in slave mode. Workaround: reset the transmit FIFO (CR[RTF] = 1)
//   before writing any new data.
//
// It does not self-heal. Design doc section 4 gives a six-rung recovery, and
// the *order* is the whole of it -- re-arming the rings before the FIFO reset,
// or raising `mcu_ready` before the requests are back on, produces a link that
// looks armed and emits garbage. So the order lives here, in a table, in a file
// with no MMIO in it, and test/link_recovery_test.c asserts each ordering
// clause section 4 states. link.c supplies the register writes through the
// callback and nothing else; there is no second copy of the sequence.
//
// This module is also where the step-2 trap is pinned down. `CH_CSR[DONE]`
// latched does NOT mean a stalled ring: DONE lives in CH_CSR rather than in the
// TCD, so a scatter-gather reload never clears it while ERQ stays set, and it
// reads as set forever on a perfectly healthy self-loading ring. It is sticky
// status, not a fault. link_recovery_required() therefore takes it as an input
// and deliberately ignores it, and the host test asserts that it alone never
// triggers a recovery -- because "fixing" it would mean tearing down a working
// link 8,000 times a second.

#ifndef HURRA_MCXN947_LINK_RECOVERY_H
#define HURRA_MCXN947_LINK_RECOVERY_H

#include <stdbool.h>
#include <stdint.h>

// One snapshot of everything that could mean the link is broken. Each field
// names the register bit it is read from; link.c does the reading, so those
// names are documentation here and the only place the bit positions appear is
// in the vendor header link.c includes.
typedef struct {
    // SR[TEF] -- transmit FIFO underrun in slave mode. ERR051588's own
    // trigger, and the one flag that means the FIFO pointers are now corrupt.
    bool lpspi_transmit_error;
    // SR[REF] -- receive FIFO overrun. Not ERR051588, but it means slots were
    // lost on the wire and the recovery ladder is the right response.
    bool lpspi_receive_error;
    // CH_ES[ERR] on either link channel.
    bool dma_channel_error;
    // MP_CSR[HALT]. enableHaltOnError is true by default, so an error on one
    // channel halts BOTH -- recovery must clear HALT, not merely re-arm ERQ.
    bool dma_controller_halted;
    // CH_CSR[DONE] on either link channel. Present so that a reader of this
    // struct sees it was considered; it is NOT a fault. See the header comment.
    bool dma_channel_done_latched;
    // link_retire_framing_lost(): every slot retired over a window failed to
    // parse. NOT a register bit, and that is the point -- an LPSPI slave whose
    // frame boundary was fixed mid-frame at CR[MEN] reports a clean SR forever
    // (see link_retire.h). Without this term the ladder would never run on the
    // one fault that measurably happens on roughly half of all boots.
    bool receive_framing_lost;
} link_fault_t;

bool link_recovery_required(const link_fault_t *fault);

// Design doc section 4's ladder, one enumerator per step, in the order they
// must run. The mapping to the six numbered rungs, which is not one-to-one:
//
//   1  Drive mcu_ready LOW                     MCU_READY_LOW
//   2  Clear ERQ; wait for ACTIVE = 0          REQUESTS_OFF, WAIT_CHANNELS_IDLE
//   3  CR[RTF] and CR[RRF]; clear SR[TEF]      RESET_FIFOS
//   4  Clear CH_CSR[DONE], rewrite the TCD     CLEAR_CHANNEL_STATE, REARM_RINGS
//      rings, re-seed both TX buffers
//   5  Re-enable ERQ                           REQUESTS_ON
//   6  Raise mcu_ready                         MCU_READY_HIGH
//
// Rung 2 is split because the wait is a distinct, failable operation with a
// timeout; rung 4 because clearing latched channel state and rewriting the
// descriptors are separate register groups and the test asserts the FIFO reset
// precedes both.
typedef enum {
    LINK_RECOVERY_OP_MCU_READY_LOW = 0,
    LINK_RECOVERY_OP_REQUESTS_OFF,
    LINK_RECOVERY_OP_WAIT_CHANNELS_IDLE,
    LINK_RECOVERY_OP_RESET_FIFOS,
    LINK_RECOVERY_OP_CLEAR_CHANNEL_STATE,
    LINK_RECOVERY_OP_REARM_RINGS,
    LINK_RECOVERY_OP_REQUESTS_ON,
    LINK_RECOVERY_OP_MCU_READY_HIGH,
    LINK_RECOVERY_OP_COUNT
} link_recovery_op_t;

// Applies one rung. `ctx` is opaque to this module.
typedef void (*link_recovery_apply_fn)(void *ctx, link_recovery_op_t op);

// Walk the ladder start to finish, calling `apply` once per rung in order.
// Returns the number of rungs applied, which is LINK_RECOVERY_OP_COUNT unless
// `apply` is NULL. There is no early exit: every rung after a failed wait still
// has to run, because a channel that never went idle is exactly the case that
// most needs its FIFO reset and its descriptors rewritten.
uint32_t link_recovery_run(link_recovery_apply_fn apply, void *ctx);

// Short stable name for logging. Never NULL, including for an out-of-range op.
const char *link_recovery_op_name(link_recovery_op_t op);

#endif  // HURRA_MCXN947_LINK_RECOVERY_H
