// Migration step 5: CPU1 proves only that it was released. It owns no
// peripheral, interrupt, clock, timer, or pin until later migration steps.

#include <stdint.h>

#include "shared_window.h"

#define CORE1_HEARTBEAT_SPIN 50000u

int main(void)
{
    g_shared_window.cpu1_boot_count++;

    for (;;) {
        g_shared_window.cpu1_heartbeat++;

        // At CPU0's configured 150 MHz, loop overhead plus this NOP lands near
        // 1 kHz. Exact cadence is deliberately not a contract; CPU0 observes
        // progress only, and CPU1 must not arm even a core-private SysTick in
        // the step whose purpose is proving it cannot affect CPU0.
        for (uint32_t i = 0u; i < CORE1_HEARTBEAT_SPIN; ++i) {
            __asm__ volatile("nop");
        }
    }
}
