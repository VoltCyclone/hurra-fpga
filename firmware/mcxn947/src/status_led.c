// Slot-counter policy first; the one trailing guard owns every GPIO access.

#include "status_led.h"

bool status_led_for_slot_counter(uint32_t slot_counter)
{
    return ((slot_counter >> STATUS_LED_SLOT_BIT) & 1u) != 0u;
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
