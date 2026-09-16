// CPU0 entry for the MCXN947 controller, migration step 1: clock and blink.
//
// Nothing here touches MMIO -- platform.c and heartbeat.c own that behind their
// guards -- so this file is portable by default, per the guard-polarity rule.
//
// Deliberately absent, and each arriving with its own step: LPSPI6 and the
// eDMA0 slot ring (step 2), `mcu_ready` (step 2), the RX retirement path
// (step 3), the TinyUSB CDC console (step 4), the CPU1 release and the shared
// window (step 5), and the watchdog (step 9). Step 1 exists to settle the
// linker script, the startup file, the retention-root mechanism and the
// toolchain while there is nothing else that a failure could be blamed on.

#include "heartbeat.h"
#include "platform.h"

int main(void)
{
    platform_init();

    // One second period: 500 ms lit, 500 ms dark.
    heartbeat_start(PLATFORM_TICK_HZ / 2u);

    // SysTick_Handler does the work. A plain spin rather than __WFI(): WFI is a
    // CMSIS intrinsic and would pull a vendor header into the one file that is
    // meant to have none.
    for (;;) {
    }
}
