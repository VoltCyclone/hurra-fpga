// Migration step 7: CPU1 remains presentation-only. A panel failure must not
// stop its heartbeat, snapshot echo, or blue status LED.

#include <stdint.h>

#include "display.h"
#include "display_panel.h"
#include "shared_window.h"
#include "status_led.h"

#define CORE1_HEARTBEAT_SPIN 50000u
// Step 5 measured this loop at about 720 heartbeats/s on hardware. Reading
// every 36th heartbeat therefore lands at about 20 Hz without arming a timer
// or interrupt, which keeps CPU1's failure surface unchanged.
#define CORE1_SNAPSHOT_HEARTBEATS 36u

int main(void)
{
    uint32_t snapshot_divider = 0u;
    link_snapshot_t last_snapshot = {0};

    g_shared_window.cpu1_boot_count++;
    status_led_hw_init();
    status_led_hw_set(false);
    // This result is deliberately not a boot gate. A missing/dead panel leaves
    // the observable CPU1 heartbeat, snapshot echo and LED loop intact. It is
    // published so CPU0 can tell "no panel" from "panel rendering nothing".
    g_shared_window.cpu1_display_flags =
        display_init() ? SHARED_DISPLAY_FLAG_READY : 0u;

    for (;;) {
        g_shared_window.cpu1_heartbeat++;
        snapshot_divider++;
        display_poll();
        g_shared_window.cpu1_blits = display_panel_blit_count();
        g_shared_window.cpu1_blit_rejects = display_panel_reject_count();

        if (snapshot_divider >= CORE1_SNAPSHOT_HEARTBEATS) {
            snapshot_divider = 0u;
            if (link_snapshot_read(&g_shared_window.snapshot, &last_snapshot)) {
                // Section 5's LPCAC question is now two loggable numbers, not
                // only an LED a human must happen to watch. This remains data
                // like heartbeat: CPU0 observes it, never waits on it, and
                // never interprets it as a command.
                g_shared_window.cpu1_seen_slot_counter =
                    last_snapshot.slot_counter;
                status_led_hw_set(
                    status_led_for_slot_counter(last_snapshot.slot_counter));
                if (display_available()) {
                    if (display_start_frame(&last_snapshot,
                                            g_shared_window.cpu1_heartbeat)) {
                        g_shared_window.cpu1_frames++;
                    } else if (!display_panel_ok()) {
                        // Only a transport failure is a fault, which is what
                        // SHARED_DISPLAY_FLAG_FAULT is documented to mean.
                        //
                        // display_start_frame() also returns false when the
                        // previous frame is still in flight, and that is normal
                        // pacing, not an error: the repaint tick is faster than
                        // a full repaint whenever the page is busy. Treating it
                        // as a fault latched the flag permanently on the first
                        // overlap and made `panel=ok/FAULT` mean nothing. The
                        // achieved rate is already visible in cpu1_frames, so a
                        // skipped tick needs no flag of its own.
                        g_shared_window.cpu1_display_flags |=
                            SHARED_DISPLAY_FLAG_FAULT;
                    }
                }
            }
        }

        // Exact cadence is measured at the step-7 gate rather than asserted
        // from this loop. A failed read or display operation leaves the echo
        // and LED machinery independent and alive.
        for (uint32_t i = 0u; i < CORE1_HEARTBEAT_SPIN; ++i) {
            __asm__ volatile("nop");
        }
    }
}
