// ERR051588 recovery ladder. No MMIO: link.c passes in the callback that does
// the register writes, so the order asserted by the host test is the same order
// the hardware runs.

#include "link_recovery.h"

#include <stddef.h>

bool link_recovery_required(const link_fault_t *fault)
{
    if (fault == NULL) {
        return false;
    }
    // dma_channel_done_latched is deliberately absent from this disjunction.
    // See the header: DONE is sticky status on a healthy self-loading ring.
    return fault->lpspi_transmit_error || fault->lpspi_receive_error ||
           fault->dma_channel_error || fault->dma_controller_halted ||
           fault->receive_framing_lost;
}

uint32_t link_recovery_run(link_recovery_apply_fn apply, void *ctx)
{
    if (apply == NULL) {
        return 0u;
    }
    for (uint32_t op = 0u; op < (uint32_t)LINK_RECOVERY_OP_COUNT; ++op) {
        apply(ctx, (link_recovery_op_t)op);
    }
    return (uint32_t)LINK_RECOVERY_OP_COUNT;
}

const char *link_recovery_op_name(link_recovery_op_t op)
{
    switch (op) {
    case LINK_RECOVERY_OP_MCU_READY_LOW:
        return "mcu_ready=0";
    case LINK_RECOVERY_OP_REQUESTS_OFF:
        return "erq=0";
    case LINK_RECOVERY_OP_WAIT_CHANNELS_IDLE:
        return "wait active=0";
    case LINK_RECOVERY_OP_RESET_FIFOS:
        return "rtf+rrf, clear tef";
    case LINK_RECOVERY_OP_CLEAR_CHANNEL_STATE:
        return "clear done/err/halt";
    case LINK_RECOVERY_OP_REARM_RINGS:
        return "rewrite tcds, reseed tx";
    case LINK_RECOVERY_OP_REQUESTS_ON:
        return "erq=1";
    case LINK_RECOVERY_OP_MCU_READY_HIGH:
        return "mcu_ready=1";
    case LINK_RECOVERY_OP_COUNT:
    default:
        return "?";
    }
}
