// Slot-counter policy first; the one trailing guard owns every GPIO access.

#include "status_led.h"

_Static_assert((STATUS_LED_CYCLE_SLOTS & (STATUS_LED_CYCLE_SLOTS - 1u)) == 0u,
               "cycle must be a power of two so the phase mask is exact");
_Static_assert(STATUS_LED_BEAT1_END < STATUS_LED_GAP_END
                   && STATUS_LED_GAP_END < STATUS_LED_BEAT2_END
                   && STATUS_LED_BEAT2_END < STATUS_LED_CYCLE_SLOTS,
               "segments must be ordered and fit inside one cycle");
_Static_assert(STATUS_LED_ON_SLOTS_PER_CYCLE
                   == STATUS_LED_BEAT1_END
                          + (STATUS_LED_BEAT2_END - STATUS_LED_GAP_END),
               "lit-slot count must match the two beats");

// Two-beat cardiac pulse, phase-locked to retired slots. See status_led.h for
// why this is a rhythm rather than a fade: the caller rewrites this pin about
// 20 times a second, which is a strobe rate, not a PWM rate.
//
// Being a pure function of slot_counter is the liveness property. Advancing
// slots animate it; a stalled slot_counter pins it to one level, so a halted
// link reads as a dark (usually) or steady LED rather than as a blink that
// keeps going after the data behind it has stopped.
bool status_led_for_slot_counter(uint32_t slot_counter)
{
    // Exact across the 2^32 wrap: the cycle is a power of two that divides
    // 2^32, so the rhythm runs continuously through the rollover.
    const uint32_t phase = slot_counter & (STATUS_LED_CYCLE_SLOTS - 1u);

    if (phase < STATUS_LED_BEAT1_END) {
        return true;  // First beat.
    }
    if (phase < STATUS_LED_GAP_END) {
        return false;  // Gap between the two beats.
    }
    return phase < STATUS_LED_BEAT2_END;  // Second beat, then the long rest.
}

#if defined(MCXN947)

#include "fsl_clock.h"
#include "fsl_gpio.h"
#include "fsl_port.h"

// FRDM-MCXN947 blue LED: P1_2, GPIO1 pin 2, mux ALT0, active low. CPU0 owns
// green P0_27 for its heartbeat and reserves red P0_10 for hard link faults;
// CPU1 must not touch either of those pins.
#define STATUS_LED_PORT PORT1
#define STATUS_LED_GPIO GPIO1
#define STATUS_LED_PIN 2u

void status_led_hw_init(void)
{
    // These are chip-wide gate-register writes, but unlike the SystemInit
    // sequence rejected at step 5 they enable only a peripheral CPU1 owns;
    // they do not replay core-LDO, glitch-detect, RAM-ECC or flash-cache
    // policy. Section 4(a)'s ladder releases CPU1 at rung 8, strictly after
    // CPU0 has finished its clock setup, so the two cores cannot race here.
    CLOCK_EnableClock(kCLOCK_Port1);
    CLOCK_EnableClock(kCLOCK_Gpio1);

    PORT_SetPinMux(STATUS_LED_PORT, STATUS_LED_PIN, kPORT_MuxAlt0);
    const gpio_pin_config_t led = {
        .pinDirection = kGPIO_DigitalOutput,
        .outputLogic = 1u,  // Active low: 1 is off.
    };
    GPIO_PinInit(STATUS_LED_GPIO, STATUS_LED_PIN, &led);
}

void status_led_hw_set(bool on)
{
    if (on) {
        GPIO_PortClear(STATUS_LED_GPIO, 1u << STATUS_LED_PIN);
    } else {
        GPIO_PortSet(STATUS_LED_GPIO, 1u << STATUS_LED_PIN);
    }
}

#endif  // MCXN947
