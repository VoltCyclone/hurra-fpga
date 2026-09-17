// CPU1 reset release. Portable register-value construction first; the only
// MMIO is in the one trailing target guard.

#include <stdint.h>

#include "core1_release.h"

uint32_t core1_cpuctrl_assert_reset(uint32_t cpuctrl_now)
{
    return cpuctrl_now | CORE1_CPUCTRL_KEY | CORE1_CPUCTRL_CLK_ENA |
           CORE1_CPUCTRL_RESET_ENA;
}

uint32_t core1_cpuctrl_release(uint32_t cpuctrl_now)
{
    return (cpuctrl_now | CORE1_CPUCTRL_KEY | CORE1_CPUCTRL_CLK_ENA) &
           ~CORE1_CPUCTRL_RESET_ENA;
}

static core1_release_status_t s_last_status = CORE1_SKIPPED_NO_IMAGE;

core1_release_status_t core1_release_last_status(void)
{
    return s_last_status;
}

bool core1_image_valid(uint32_t initial_msp, uint32_t reset_vector)
{
    // An erased region reads 0xFFFFFFFF for both, which fails every clause
    // below. So does a half-written image, which is the other way this goes
    // wrong in practice.
    if (initial_msp <= CORE1_RAM_START || initial_msp > CORE1_RAM_END) {
        return false;
    }
    if ((initial_msp & 0x7u) != 0u) {
        return false;  // AAPCS requires an 8-byte aligned stack pointer.
    }
    if (reset_vector < CORE1_FLASH_START || reset_vector >= CORE1_FLASH_END) {
        return false;
    }
    // Armv8-M has no ARM state: bit 0 of a reset vector is always set. A clear
    // bit would fault on the first fetch, which is the failure being avoided.
    return (reset_vector & 1u) != 0u;
}

#if defined(MCXN947)

#if !defined(CORE1_VECTOR_ADDR)
#error "CORE1_VECTOR_ADDR must come from the Makefile's CORE1_OFFSET"
#endif

// No parameter. The boot address is CORE1_OFFSET by definition -- it is the
// ORIGIN of m_interrupts in MCXN947_cm33_core1_flash.ld and the offset the
// merge tool places core1 at -- so accepting it as an argument would invent a
// second place for it to be wrong. Design doc section 8 chose the literal over
// the embedded-blob symbol for the same reason.
//
// An earlier draft took the address as an argument and returned early when it
// disagreed with CORE1_VECTOR_ADDR. That is precisely the failure rung 6
// exists to catch: a silent no-op here leaves a working link and a black
// screen, with every counter healthy and nothing anywhere saying CPU1 was
// never released.
core1_release_status_t core1_release(void)
{
    // Two words of flash, read once. If they are not a plausible vector pair
    // there is no image to run, and releasing CPU1 would reset this core.
    const volatile uint32_t *const vectors =
        (const volatile uint32_t *)(uintptr_t)CORE1_VECTOR_ADDR;
    if (!core1_image_valid(vectors[0], vectors[1])) {
        s_last_status = CORE1_SKIPPED_NO_IMAGE;
        return s_last_status;
    }

    volatile uint32_t *const cpuctrl =
        (volatile uint32_t *)(uintptr_t)(CORE1_SYSCON_BASE + CORE1_CPUCTRL_OFFSET);
    volatile uint32_t *const cpboot =
        (volatile uint32_t *)(uintptr_t)(CORE1_SYSCON_BASE + CORE1_CPBOOT_OFFSET);

    *cpboot = CORE1_VECTOR_ADDR;

    // Design doc section 8 and SDK boot_multicore_slave.c: both writes must
    // derive from this same read. Re-reading after asserting reset would let
    // hardware state leak into the release value and make the sequence no
    // longer the vendor's documented transaction.
    const uint32_t cpuctrl_now = *cpuctrl;
    *cpuctrl = core1_cpuctrl_assert_reset(cpuctrl_now);
    *cpuctrl = core1_cpuctrl_release(cpuctrl_now);
    s_last_status = CORE1_RELEASED;
    return s_last_status;
}

void core1_halt(void)
{
    volatile uint32_t *const cpuctrl =
        (volatile uint32_t *)(uintptr_t)(CORE1_SYSCON_BASE + CORE1_CPUCTRL_OFFSET);

    // The assert-reset half of the release transaction, left standing. CPU1
    // stops wherever it was; its clock stays enabled so a later core1_release()
    // is the same three stores as at boot.
    *cpuctrl = core1_cpuctrl_assert_reset(*cpuctrl);
    // NOT "no image": there is one, an operator stopped it. Reporting the two
    // the same way would tell whoever is reading the console to go and reflash
    // a part that is already correctly programmed.
    s_last_status = CORE1_HELD_IN_RESET;
}

#endif  // MCXN947
