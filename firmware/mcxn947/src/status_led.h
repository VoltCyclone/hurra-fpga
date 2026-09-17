// CPU1's presentation-only status LED: portable slot-counter policy plus a
// target-only blue GPIO implementation in status_led.c.

#ifndef HURRA_MCXN947_STATUS_LED_H
#define HURRA_MCXN947_STATUS_LED_H

#include <stdbool.h>
#include <stdint.h>

// A full bit-11 cycle is 2^(11 + 1) = 4096 retired slots. At about 8,000
// slots/s that is 8000 / 4096 = 1.95 Hz: visible and countable by eye.
#define STATUS_LED_SLOT_BIT 11u

bool status_led_for_slot_counter(uint32_t slot_counter);

void status_led_hw_init(void);
void status_led_hw_set(bool on);

#endif  // HURRA_MCXN947_STATUS_LED_H
