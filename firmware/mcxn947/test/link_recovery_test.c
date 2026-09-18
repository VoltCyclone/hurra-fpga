// Host test for src/link_recovery.c.
//
// The ladder is a table, so a test that only read the table back would be
// tautological. What these cases assert instead is each ORDERING CLAUSE design
// doc section 4 states in prose, named so that a reordering of the table fails
// with the clause it broke rather than with "array differs at index 3".
//
// The fault predicate half carries the step-2 trap: CH_CSR[DONE] latched is
// sticky status on a healthy self-loading ring, not a stalled one, and
// "fixing" it would tear the link down 8,000 times a second.

#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "link_recovery.h"

#define MAX_OPS 32

typedef struct {
    link_recovery_op_t op[MAX_OPS];
    uint32_t count;
} trace_t;

static void record(void *ctx, link_recovery_op_t op)
{
    trace_t *t = (trace_t *)ctx;
    assert(t->count < MAX_OPS);
    t->op[t->count++] = op;
}

// Index of `op` in the trace, or -1. Every ordering assertion below is phrased
// against these, so the failure message names the two rungs involved.
static int at(const trace_t *t, link_recovery_op_t op)
{
    for (uint32_t i = 0u; i < t->count; ++i) {
        if (t->op[i] == op) {
            return (int)i;
        }
    }
    return -1;
}

static void test_ladder_order(void)
{
    trace_t t;
    memset(&t, 0, sizeof(t));

    assert(link_recovery_run(record, &t) == (uint32_t)LINK_RECOVERY_OP_COUNT);
    assert(t.count == (uint32_t)LINK_RECOVERY_OP_COUNT);

    // Every rung runs, exactly once.
    for (uint32_t op = 0u; op < (uint32_t)LINK_RECOVERY_OP_COUNT; ++op) {
        uint32_t seen = 0u;
        for (uint32_t i = 0u; i < t.count; ++i) {
            if (t.op[i] == (link_recovery_op_t)op) {
                seen++;
            }
        }
        assert(seen == 1u);
    }

    const int ready_low = at(&t, LINK_RECOVERY_OP_MCU_READY_LOW);
    const int erq_off = at(&t, LINK_RECOVERY_OP_REQUESTS_OFF);
    const int wait_idle = at(&t, LINK_RECOVERY_OP_WAIT_CHANNELS_IDLE);
    const int fifos = at(&t, LINK_RECOVERY_OP_RESET_FIFOS);
    const int clear = at(&t, LINK_RECOVERY_OP_CLEAR_CHANNEL_STATE);
    const int rearm = at(&t, LINK_RECOVERY_OP_REARM_RINGS);
    const int erq_on = at(&t, LINK_RECOVERY_OP_REQUESTS_ON);
    const int ready_high = at(&t, LINK_RECOVERY_OP_MCU_READY_HIGH);

    // Section 4 rung 1: "Drive mcu_ready LOW" is FIRST. It is what stops the
    // FPGA consuming TX frames (send_message = mcu_ready & tx_queued) and what
    // gates all four error counters off (they are gated on transfer_ready), so
    // any rung ahead of it would be counted against us as a real fault.
    assert(ready_low == 0);
    for (uint32_t i = 0u; i < t.count; ++i) {
        assert(i == 0u || t.op[i] != LINK_RECOVERY_OP_MCU_READY_LOW);
    }

    // Rung 2: clear ERQ, then wait for ACTIVE = 0. Waiting before clearing the
    // request would wait on a channel that is still being fed.
    assert(erq_off < wait_idle);
    assert(ready_low < erq_off);

    // Rung 3: the FIFOs are reset only once both channels are quiet. Resetting
    // the transmit FIFO while the DMA is still writing TDR reintroduces exactly
    // the corruption being recovered from.
    assert(wait_idle < fifos);

    // The erratum's own workaround clause: "reset the transmit FIFO (CR[RTF])
    // BEFORE writing any new data". Both the descriptor rewrite and the moment
    // requests come back on are after it.
    assert(fifos < rearm);
    assert(fifos < erq_on);

    // Rung 4: latched channel state is cleared before the descriptors are
    // rewritten. enableHaltOnError is true by default, so an error on one
    // channel halts BOTH -- installing a TCD into a halted controller arms
    // nothing.
    assert(clear < rearm);
    assert(fifos < clear);

    // Rung 5 then 6: requests back on BEFORE mcu_ready goes high. The other
    // order advertises a ready MCU whose DMA is not yet feeding the FIFO,
    // which is a transmit underrun -- the fault this ladder exists for.
    assert(erq_on < ready_high);
    assert(rearm < erq_on);

    // Rung 6 is last: nothing may run after the link is declared live again.
    assert(ready_high == (int)t.count - 1);
}

static void test_run_tolerates_no_callback(void)
{
    assert(link_recovery_run(NULL, NULL) == 0u);
}

static void test_fault_predicate(void)
{
    link_fault_t f;

    memset(&f, 0, sizeof(f));
    assert(!link_recovery_required(&f));
    assert(!link_recovery_required(NULL));

    // Each fault on its own is sufficient.
    memset(&f, 0, sizeof(f));
    f.lpspi_transmit_error = true;  // SR[TEF] -- ERR051588 itself
    assert(link_recovery_required(&f));

    memset(&f, 0, sizeof(f));
    f.lpspi_receive_error = true;
    assert(link_recovery_required(&f));

    memset(&f, 0, sizeof(f));
    f.dma_channel_error = true;
    assert(link_recovery_required(&f));

    memset(&f, 0, sizeof(f));
    f.dma_controller_halted = true;
    assert(link_recovery_required(&f));

    // The term that comes from retired content rather than from a register.
    // A slave whose frame boundary was fixed mid-frame at CR[MEN] reports a
    // perfectly clean SR while every slot on the wire is unreadable, so without
    // this the ladder would never run on it.
    memset(&f, 0, sizeof(f));
    f.receive_framing_lost = true;
    assert(link_recovery_required(&f));

    // And the one that is NOT a fault. CH_CSR[DONE] is sticky status: it lives
    // in CH_CSR rather than in the TCD, so a scatter-gather reload never clears
    // it while ERQ stays set, and it reads as set forever on a healthy ring.
    // Measured at step 2 on a link running 8,000 clean slots a second.
    memset(&f, 0, sizeof(f));
    f.dma_channel_done_latched = true;
    assert(!link_recovery_required(&f));

    // It does not suppress a real fault either -- it is simply ignored.
    f.lpspi_transmit_error = true;
    assert(link_recovery_required(&f));
}

static void test_op_names(void)
{
    for (uint32_t op = 0u; op <= (uint32_t)LINK_RECOVERY_OP_COUNT; ++op) {
        const char *name = link_recovery_op_name((link_recovery_op_t)op);
        assert(name != NULL);
        assert(name[0] != '\0');
    }
}

int main(void)
{
    test_ladder_order();
    test_run_tolerates_no_callback();
    test_fault_predicate();
    test_op_names();

    printf("link_recovery_test: ok\n");
    return 0;
}
