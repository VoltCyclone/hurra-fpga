// CPU0 entry for the MCXN947 controller through migration step 5: clock,
// blink, the FPGA link, USB console, shared window and final CPU1 release.
//
// Nothing here touches MMIO -- platform.c and heartbeat.c own that behind their
// guards -- so this file is portable by default, per the guard-polarity rule.
//
// Deliberately absent, and each arriving with its own step: the display work
// (step 7), map uploader (step 8) and watchdog (step 9). TX is still
// permanently IDLE: originating a command is step 8/9, not this one.
//
// The console arrived at step 4 and its placement in this function IS the
// safety invariant. Design doc section 4(a): `mcu_ready` is raised at rung 4
// and USB is brought up at rung 6, never the other way round. link_init()
// raises the line as its last act, so usb_console_init() below is rung 6 and
// the ordering holds by construction -- but only as long as the two calls stay
// in this order. Nothing in the build checks it; this comment is the check.
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

#include "core1_release.h"
#include "heartbeat.h"
#include "link.h"
#include "platform.h"
#include "shared_window.h"
#include "usb_console.h"

int main(void)
{
    platform_init();

    // One second period: 500 ms lit, 500 ms dark.
    heartbeat_start(PLATFORM_TICK_HZ / 2u);

    // Brings up LPSPI6 and both eDMA0 rings, then raises `mcu_ready`. Ordering
    // inside is the safety invariant's boot ladder; see link.c.
    link_init();

    // Rung 5 is after mcu_ready by construction. This section is NOLOAD in
    // both images, so CPU0 must publish a known window before CPU1 can observe
    // or update it.
    shared_window_reset();

    // Rung 6. After the link, always. A host that never appears, a J11 that is
    // not plugged in, a PHY PLL that never locks -- none of them can reach the
    // link from here, because the link was already running when this was
    // called.
    usb_console_init();

    // Rung 7 (CPU0's watchdog) arrives at migration step 9.

    // Rung 8 is deliberately the final boot action, and it is conditional. Moving it above link_init
    // would make mcu_ready depend on CPU1 startup; moving it above the shared
    // reset would let CPU0 erase live CPU1 state; moving it above USB would
    // violate section 4(a)'s measured ladder. A blank, halted, or crashing CPU1
    // must therefore be indistinguishable to the already-live FPGA link.
    //
    // It refuses to release CPU1 onto a region that does not hold a plausible
    // vector pair. That is not caution, it is measured: with the core1 region
    // erased, releasing CPU1 unconditionally locks it up and resets this core,
    // and CPU0 boot-looped until core1_image_valid() existed. The console
    // reports which happened; nothing here decides anything on the result,
    // because deciding on it would make the link depend on CPU1 after all.
    (void)core1_release();

    // SysTick_Handler blinks; the eDMA0 channel 1 ISR retires; the USB1_HS ISR
    // moves packets. This loop only services the things that tolerate latency.
    // A plain spin rather than __WFI(): WFI is a CMSIS intrinsic and would pull
    // a vendor header into the one file that is meant to have none.
    //
    // Design doc section 3 is measured here rather than argued: CPU0 has a
    // 107.93 us staging window per slot, not a latency deadline, so a console
    // sharing this loop is affordable. Step 4's third gate saturates the
    // console and re-reads the FPGA's counters to check that claim.
    for (;;) {
        link_poll();
        usb_console_poll();
    }
}
