// CPU0 entry for the MCXN947 controller, migration step 2: clock, blink, and
// the FPGA injection link.
//
// Nothing here touches MMIO -- platform.c and heartbeat.c own that behind their
// guards -- so this file is portable by default, per the guard-polarity rule.
//
// Deliberately absent, and each arriving with its own step: the RX retirement
// path (step 3), the TinyUSB CDC console (step 4), the CPU1 release and the
// shared window (step 5), and the watchdog (step 9).
//
// After link_init() returns there is nothing left to service. The eDMA0 rings
// are self-loading and the TX banks are never refilled, so the link runs with
// no CPU involvement at all and the foreground loop is genuinely empty. The
// blink is the only thing still moving, and at this step it means exactly what
// it meant at step 1: CPU0 reached its foreground loop. It is deliberately not
// wired to link health -- there is no link health to report until step 3
// retires a slot.

#include "heartbeat.h"
#include "link.h"
#include "platform.h"

int main(void)
{
    platform_init();

    // One second period: 500 ms lit, 500 ms dark.
    heartbeat_start(PLATFORM_TICK_HZ / 2u);

    // Brings up LPSPI6 and both eDMA0 rings, then raises `mcu_ready`. Ordering
    // inside is the safety invariant's boot ladder; see link.c.
    link_init();

    // SysTick_Handler does the work. A plain spin rather than __WFI(): WFI is a
    // CMSIS intrinsic and would pull a vendor header into the one file that is
    // meant to have none.
    for (;;) {
    }
}
