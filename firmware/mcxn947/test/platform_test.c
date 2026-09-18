// Host test for platform.c's portable half.
//
// It compiles src/platform.c WITHOUT -DMCXN947, so it also proves the guard
// polarity: every MMIO access in that file is inside `#if defined(MCXN947)`,
// and nothing outside the guard reaches for a vendor header.

#include <assert.h>
#include <stdio.h>

#include "platform.h"

int main(void)
{
    // The configured pair. 150 MHz / 1 kHz = 150000 cycles.
    assert(platform_systick_reload(PLATFORM_CORE_HZ, PLATFORM_TICK_HZ) == 149999u);

    // Degenerate inputs are refused rather than trapped or truncated.
    assert(platform_systick_reload(0u, PLATFORM_TICK_HZ) == 0u);
    assert(platform_systick_reload(PLATFORM_CORE_HZ, 0u) == 0u);

    // Inexact division is refused. 150 MHz / 7 kHz is not a whole number of
    // cycles, and truncating would put a fixed error into every timeout later
    // derived from the tick.
    assert(platform_systick_reload(150000000u, 7000u) == 0u);

    // SysTick LOAD is 24 bits and counts LOAD + 1, so 0x1000000 cycles is the
    // largest representable period and one more is not.
    assert(platform_systick_reload(0x01000000u, 1u) == 0x00FFFFFFu);
    assert(platform_systick_reload(0x02000000u, 1u) == 0u);

    // A one-cycle period is refused: ARMv8-M disables SysTick when LOAD reads
    // zero, so the tick would stop rather than run fast. Two cycles is the
    // smallest period that works.
    assert(platform_systick_reload(1000u, 1000u) == 0u);
    assert(platform_systick_reload(2000u, 1000u) == 1u);

    printf("platform_test: ok\n");
    return 0;
}
