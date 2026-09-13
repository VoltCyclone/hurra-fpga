#pragma once

/*
 * Inter-core shared window: fixed addresses agreed by both images.
 *
 * core/link_v3f.ld:16 and core/link_v5f.ld:16 both declare:
 *   RAM_SHARED (xrw) : ORIGIN = 0x20178000, LENGTH = 32K
 *
 * The addresses are deliberately absolute. A struct placed in a named section
 * only lands at the same offset in two separately linked images if their
 * section ordering happens to match; neither linker script makes that a
 * contract.
 */

#include <stdint.h>

#define SHARED_WINDOW_BASE UINT32_C(0x20178000)
#define SHARED_WINDOW_SIZE (UINT32_C(32) * UINT32_C(1024))

/*
 * Reserve the top 256 bytes for diagnostics. Phase 2 data-plane allocations
 * grow from the bottom of the window and cannot collide with this block.
 */
#define SHARED_DIAG_BASE (SHARED_WINDOW_BASE + SHARED_WINDOW_SIZE - UINT32_C(256))

#define SHARED_DIAG_WITNESS_OFF UINT32_C(0x00)
#define SHARED_DIAG_MCAUSE_OFF UINT32_C(0x04)
#define SHARED_DIAG_MEPC_OFF UINT32_C(0x08)
#define SHARED_DIAG_CORE_ID_OFF UINT32_C(0x0C)

#define SHARED_DIAG_WORD(offset) \
    (*(volatile uint32_t *)(uintptr_t)(SHARED_DIAG_BASE + (offset)))

#define SHARED_DIAG_WITNESS SHARED_DIAG_WORD(SHARED_DIAG_WITNESS_OFF)
#define SHARED_DIAG_MCAUSE SHARED_DIAG_WORD(SHARED_DIAG_MCAUSE_OFF)
#define SHARED_DIAG_MEPC SHARED_DIAG_WORD(SHARED_DIAG_MEPC_OFF)
#define SHARED_DIAG_CORE_ID SHARED_DIAG_WORD(SHARED_DIAG_CORE_ID_OFF)

/*
 * RM V1.7 section 4.1.1 describes the V5F as out-of-order and permits
 * reordering accesses to Normal-type memory. The shared SRAM window is Normal
 * memory; HSEM and IPC are in Device-typed, strongly ordered PPB memory.
 *
 * Before publishing a flag or ready word that makes another core consume a
 * shared-SRAM payload, write the payload, call ch32_fence(), then publish the
 * flag. Semaphore and doorbell MMIO needs no extra fence; the SRAM payload
 * does. MEMINFO describes an I-cache but no D-cache, so this is an ordering
 * requirement rather than a cache-coherency operation.
 */
static inline void ch32_fence(void)
{
    __asm volatile("fence" ::: "memory");
}
