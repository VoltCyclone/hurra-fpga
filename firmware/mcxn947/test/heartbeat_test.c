// Host test for heartbeat.c's portable half.
//
// It compiles src/heartbeat.c WITHOUT -DMCXN947: the GPIO writes and the
// SysTick ISR are behind the guard, so only the cadence is under test here.

#include <assert.h>
#include <stdio.h>

#include "heartbeat.h"

static uint32_t edges_over(uint32_t half_period, uint32_t ticks)
{
    heartbeat_t hb;
    heartbeat_reset(&hb, half_period);

    uint32_t edges = 0u;
    for (uint32_t i = 0u; i < ticks; i++) {
        if (heartbeat_tick(&hb)) {
            edges++;
        }
    }
    return edges;
}

int main(void)
{
    heartbeat_t hb;

    // Starts dark, so the first edge lights the LED.
    heartbeat_reset(&hb, 3u);
    assert(hb.on == false);
    assert(heartbeat_tick(&hb) == false);
    assert(heartbeat_tick(&hb) == false);
    assert(heartbeat_tick(&hb) == true);
    assert(hb.on == true);

    // ... and the next edge is a full half period later.
    assert(heartbeat_tick(&hb) == false);
    assert(heartbeat_tick(&hb) == false);
    assert(heartbeat_tick(&hb) == true);
    assert(hb.on == false);

    // The configured cadence: 500 ticks per level at 1 kHz is 1 Hz, so one
    // second of ticks is exactly two edges.
    assert(edges_over(500u, 1000u) == 2u);

    // A zero half period is clamped to 1 rather than dividing by zero or
    // leaving the LED frozen.
    heartbeat_reset(&hb, 0u);
    assert(hb.half_period_ticks == 1u);
    assert(heartbeat_tick(&hb) == true);
    assert(heartbeat_tick(&hb) == true);

    printf("heartbeat_test: ok\n");
    return 0;
}
