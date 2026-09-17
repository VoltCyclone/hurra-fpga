// Migration step 6: CPU1 remains presentation-only. It reads CPU0's snapshot,
// echoes the value it actually observed, and drives only its blue status LED.

#include <stdint.h>

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

    for (;;) {
        g_shared_window.cpu1_heartbeat++;
        snapshot_divider++;

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
            }
        }

        // Exact cadence is deliberately not a contract. CPU1 still arms no
        // interrupt or timer; a failed bounded read simply leaves the last
        // echo and LED state untouched while heartbeat continues.
        for (uint32_t i = 0u; i < CORE1_HEARTBEAT_SPIN; ++i) {
            __asm__ volatile("nop");
        }
    }
}
