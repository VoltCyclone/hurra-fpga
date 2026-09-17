#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "status_led.h"

int main(void)
{
    // Bit 11 is low for 2048 slots, then high for 2048 slots. These boundary
    // values catch either a wrong bit selection or an off-by-one mask.
    assert(!status_led_for_slot_counter(0u));
    assert(!status_led_for_slot_counter(2047u));
    assert(status_led_for_slot_counter(2048u));
    assert(status_led_for_slot_counter(4095u));
    assert(!status_led_for_slot_counter(4096u));
    assert(status_led_for_slot_counter(6144u));
    printf("status_led_test: ok\n");
    return 0;
}
