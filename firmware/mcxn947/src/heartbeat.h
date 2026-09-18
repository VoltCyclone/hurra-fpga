// A blink cadence and the FRDM-MCXN947 LED it drives.
//
// Step 1 has nothing to report yet, so the LED means only "CPU0 reached its
// foreground loop with a working tick". The green LED is used deliberately:
// the design reserves red for CPU0 driving a hard link fault directly, and a
// heartbeat that squatted on it would have to be moved later.
//
// Declarations are portable C; only the definitions in heartbeat.c that touch
// GPIO are guarded.

#ifndef HURRA_MCXN947_HEARTBEAT_H
#define HURRA_MCXN947_HEARTBEAT_H

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t half_period_ticks;  // ticks per level, so the full period is 2x
    uint32_t ticks_remaining;
    bool on;
} heartbeat_t;

// Portable. A half period of 0 is clamped to 1 rather than dividing by zero or
// freezing the LED.
void heartbeat_reset(heartbeat_t *hb, uint32_t half_period_ticks);

// Portable. Advances one tick; returns true on the tick where `on` flipped.
bool heartbeat_tick(heartbeat_t *hb);

// Target-only definitions: mux and drive the LED, and arm the module's own
// static state so SysTick_Handler has something to advance.
void heartbeat_hw_init(void);
void heartbeat_hw_set(bool on);
void heartbeat_start(uint32_t half_period_ticks);

#endif  // HURRA_MCXN947_HEARTBEAT_H
