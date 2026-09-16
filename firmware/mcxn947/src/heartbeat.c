// Blink cadence (portable) plus the FRDM-MCXN947 green LED and the SysTick ISR
// that drives it (one trailing guard block).

#include "heartbeat.h"

void heartbeat_reset(heartbeat_t *hb, uint32_t half_period_ticks)
{
    hb->half_period_ticks = (half_period_ticks == 0u) ? 1u : half_period_ticks;
    hb->ticks_remaining = hb->half_period_ticks;
    hb->on = false;
}

bool heartbeat_tick(heartbeat_t *hb)
{
    if (hb->ticks_remaining > 1u) {
        hb->ticks_remaining--;
        return false;
    }
    hb->ticks_remaining = hb->half_period_ticks;
    hb->on = !hb->on;
    return true;
}

#if defined(MCXN947)

#include "fsl_clock.h"
#include "fsl_gpio.h"
#include "fsl_port.h"

// FRDM-MCXN947 green LED: P0_27, PORT mux ALT0 (= GPIO on this part), active
// low. Taken from the SDK's own board package for this board --
// boards/frdmmcxn947/project_template/{board.h,pin_mux.c}. Note that the
// MCX-N9XX-EVK maps the RGB LED to GPIO3[2:4] *active high*; an export carrying
// that mapping under the FRDM board name is wrong for this board.
#define HEARTBEAT_LED_PORT PORT0
#define HEARTBEAT_LED_GPIO GPIO0
#define HEARTBEAT_LED_PIN 27u

static heartbeat_t g_heartbeat;

void heartbeat_hw_init(void)
{
    CLOCK_EnableClock(kCLOCK_Port0);
    CLOCK_EnableClock(kCLOCK_Gpio0);

    PORT_SetPinMux(HEARTBEAT_LED_PORT, HEARTBEAT_LED_PIN, kPORT_MuxAlt0);

    gpio_pin_config_t led = {
        .pinDirection = kGPIO_DigitalOutput,
        .outputLogic = 1u,  // active low: 1 == off
    };
    GPIO_PinInit(HEARTBEAT_LED_GPIO, HEARTBEAT_LED_PIN, &led);
}

void heartbeat_hw_set(bool on)
{
    if (on) {
        GPIO_PortClear(HEARTBEAT_LED_GPIO, 1u << HEARTBEAT_LED_PIN);
    } else {
        GPIO_PortSet(HEARTBEAT_LED_GPIO, 1u << HEARTBEAT_LED_PIN);
    }
}

void heartbeat_start(uint32_t half_period_ticks)
{
    heartbeat_reset(&g_heartbeat, half_period_ticks);
    heartbeat_hw_init();
    heartbeat_hw_set(g_heartbeat.on);
}

// The vector table declares SysTick_Handler `.weak` and points it at a stub
// that branches to itself, so this definition only reaches the image because it
// is a strong symbol *and* a retention root -- see CORE0_RETAIN in the
// Makefile. Without the root, --gc-sections is free to drop it and the weak
// spin stub links in its place: the LED would simply never blink, with no
// build error anywhere.
void SysTick_Handler(void)
{
    if (heartbeat_tick(&g_heartbeat)) {
        heartbeat_hw_set(g_heartbeat.on);
    }
}

#endif  // MCXN947
