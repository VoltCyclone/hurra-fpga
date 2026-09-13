#include "trap_witness.h"

#include <stdint.h>

#include "ch32h417_port.h"
#include "shared_window.h"

/*
 * Both startups enter main() in U-mode, but traps enter these handlers in
 * M-mode. Reading the MRO/MRW mcause and mepc CSRs is therefore legal here;
 * the same reads from application code would trap.
 */
enum {
    TRAP_WITNESS_HARD_FAULT = 0x48415244u, /* "HARD" */
    TRAP_WITNESS_NMI = 0x4E4D4921u,        /* "NMI!" */
    TRAP_WITNESS_ECALL_M = 0x45434D21u,    /* "ECM!" */
    TRAP_WITNESS_ECALL_U = 0x45435521u,    /* "ECU!" */
};

static inline __attribute__((always_inline, noreturn)) void
trap_witness_record(uint32_t witness)
{
    /*
     * Treat witness as the publication flag. Clear any old publication,
     * populate the payload, release it with a data fence, then publish the new
     * witness. A consumer must fence after observing a non-zero witness before
     * reading the payload.
    */
    SHARED_DIAG_WITNESS = 0u;
    ch32_fence();
    SHARED_DIAG_MCAUSE = __get_MCAUSE();
    SHARED_DIAG_MEPC = __get_MEPC();
    SHARED_DIAG_CORE_ID = NVIC_GetCurrentCoreID();
    ch32_fence();
    SHARED_DIAG_WITNESS = witness;
    ch32_fence();

    for (;;) {
        __asm volatile("" ::: "memory");
    }
}

void trap_witness_clear(void)
{
    SHARED_DIAG_WITNESS = 0u;
    SHARED_DIAG_MCAUSE = 0u;
    SHARED_DIAG_MEPC = 0u;
    SHARED_DIAG_CORE_ID = 0u;
    ch32_fence();
}

void HardFault_Handler(void) WCH_IRQ;
void HardFault_Handler(void)
{
    trap_witness_record(TRAP_WITNESS_HARD_FAULT);
}

void NMI_Handler(void) WCH_IRQ;
void NMI_Handler(void)
{
    trap_witness_record(TRAP_WITNESS_NMI);
}

void Ecall_M_Mode_Handler(void) WCH_IRQ;
void Ecall_M_Mode_Handler(void)
{
    trap_witness_record(TRAP_WITNESS_ECALL_M);
}

void Ecall_U_Mode_Handler(void) WCH_IRQ;
void Ecall_U_Mode_Handler(void)
{
    trap_witness_record(TRAP_WITNESS_ECALL_U);
}
