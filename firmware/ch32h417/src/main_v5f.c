// V5F application image: the foreground service loop.
//
// The V3F owns the clock tree and wakes this core (main_v3f.c). By the time
// main() runs here, SYSCLK/HCLK are already programmed; this core only refreshes
// its own view of them and then services the CDC control channel, the board-to-
// board link, the USB frame timebase, and the status LED, all under a health-
// gated watchdog. All work is polled from one loop -- the USBFS, SPI, TIM8
// capture, and TIM3 tick ISRs push state into their modules and this loop
// retires it, so there is no RTOS and no blocking wait.

#include <stdint.h>

#include "trap_witness.h"
#include "timebase.h"
#include "status_led.h"
#include "usb_cdc_fs.h"
#include "usb_time.h"
#include "ch32_link.h"
#include "watchdog.h"
// system_ch32h417.h declares uint32_t clock globals without including <stdint.h>
// itself, so the explicit include above must precede it. Kept explicit rather
// than relying on a module header to pull stdint in first.
#include "system_ch32h417.h"

// Deferred wiring -- intentionally NOT serviced by this loop yet:
//
//   The READY / LEASED / FAULT / COMMAND_PULSE LED states need injection-map
//   validity, lease ownership, and fault decode from the FPGA/CDC, none of which
//   is available here yet. Until then the ladder tops out at PASSTHROUGH_ONLY.

int main(void)
{
    // Last-seen RX-DMA re-arm count, for deriving the watchdog's DMA-liveness
    // bit by delta. Starts at 0 to match ch32_link's reset state.
    uint32_t last_dma_rearm = 0u;

    // Preserved bring-up prologue: clear stale shared-SRAM diagnostics, then
    // latch this core's clock values. Both must run first and in this order --
    // the platform relies on it -- so nothing above these two lines.
    trap_witness_clear();
    SystemAndCoreClockUpdate();

    // The millisecond timebase is clocked from HCLK (the TIM3/HB1 bus clock),
    // NOT the 400 MHz core clock. Feeding it SystemCoreClock would make millis()
    // run several times fast and every timeout with it.
    timebase_init(HCLKClock);
    status_led_init();
    cdc_fs_init();
    usb_time_init();
    ch32_link_init();
    // Arm the IWDG last, once every subsystem it supervises is initialized, so
    // the first health window opens against a live system. With the watchdog
    // armed, a wedged subsystem (or, on a bench with no FPGA clocking SPI, a
    // link that never retires a slot) closes a window unfed and resets the part
    // within ~1 s -- this is the designed liveness guarantee, not a fault.
    watchdog_init();

    for (;;) {
        uint32_t now = millis();

        // Retire all subsystem work before deriving state, so the LED reflects
        // this iteration's freshly serviced health, not last pass's.
        cdc_fs_poll();
        ch32_link_poll();
        usb_time_poll();

        bool link_healthy = ch32_link_healthy(now);

        // Watchdog health, one bit per subsystem; all three required per window:
        //   CDC/net      -- the state machine was polled this pass (a detached
        //                   host still earns the bit; it means "serviced").
        //   SPI progress -- the link retired a fresh slot recently (the poll/
        //                   foreground half is alive).
        //   DMA rearm    -- the RX DMA ISR re-armed since last pass (the
        //                   interrupt half is alive), detected by counter delta.
        // A wedge in any one closes the window unfed; watchdog_poll withholds
        // the IWDG reload and the part resets.
        watchdog_note(HEALTH_CDC_OR_NET);
        if (link_healthy) {
            watchdog_note(HEALTH_SPI_PROGRESS);
        }
        uint32_t dma_rearm = ch32_link_counters()->dma_rearm;
        if (dma_rearm != last_dma_rearm) {
            watchdog_note(HEALTH_DMA_REARM);
            last_dma_rearm = dma_rearm;
        }

        // Status ladder, highest condition first. Link down outranks "no HID
        // yet", which outranks "passthrough running": we are still booting until
        // the FPGA link is healthy, then waiting for the physical mouse until
        // USB_SYNC locks, then passing through.
        status_led_state_t state;
        if (!link_healthy) {
            state = STATUS_LED_BOOT;
        } else if (!usb_time_snapshot().synchronized) {
            state = STATUS_LED_WAIT_MOUSE;
        } else {
            state = STATUS_LED_PASSTHROUGH_ONLY;
        }
        status_led_set_state(state);
        status_led_poll(now);

        // Evaluate the health window last, after every bit for this pass is in.
        watchdog_poll(now);
    }
}
