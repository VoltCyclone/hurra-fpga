// CPU0 entry for the MCXN947 controller, migration step 3: clock, blink, the
// FPGA injection link, and the retirement of what the link receives.
//
// Nothing here touches MMIO -- platform.c and heartbeat.c own that behind their
// guards -- so this file is portable by default, per the guard-polarity rule.
//
// Deliberately absent, and each arriving with its own step: the TinyUSB CDC
// console (step 4), the CPU1 release and the shared window (step 5), the map
// uploader (step 8) and the watchdog (step 9). TX is still permanently IDLE:
// originating a command is step 8/9, not this one.
//
// The foreground loop is no longer empty, but it is still not on any deadline.
// Retirement itself runs in the eDMA0 channel 1 ISR, where the 125 us slot
// cadence can be met; link_poll() only does the work that tolerates latency --
// fault detection, ERR051588 recovery, and the debug-UART report.
//
// The blink still means what it meant at step 1: CPU0 reached its foreground
// loop. It is deliberately NOT wired to link health. The design reserves the
// red LED for CPU0 signalling a hard link fault, and moving the green one onto
// link state would make a stalled foreground look like a dead link.

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

    // SysTick_Handler blinks; the eDMA0 channel 1 ISR retires. This loop only
    // services the things that tolerate latency. A plain spin rather than
    // __WFI(): WFI is a CMSIS intrinsic and would pull a vendor header into the
    // one file that is meant to have none.
    for (;;) {
        link_poll();
    }
}
