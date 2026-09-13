#include "status_led.h"

#include <stdbool.h>

/* --- Pure policy (MMIO-free; host-testable). --- */

static const status_led_style_t s_styles[STATUS_LED_STATE_COUNT] = {
    [STATUS_LED_FAULT] = {255u, 0u, 0u, STATUS_LED_ANIM_BLINK},
    [STATUS_LED_BOOT] = {255u, 176u, 0u, STATUS_LED_ANIM_BREATHE},
    [STATUS_LED_WAIT_MOUSE] = {0u, 0u, 255u, STATUS_LED_ANIM_BREATHE},
    [STATUS_LED_PASSTHROUGH_ONLY] = {0u, 255u, 255u, STATUS_LED_ANIM_STEADY},
    [STATUS_LED_READY] = {0u, 255u, 0u, STATUS_LED_ANIM_STEADY},
    [STATUS_LED_LEASED] = {160u, 0u, 255u, STATUS_LED_ANIM_STEADY},
    [STATUS_LED_COMMAND_PULSE] = {255u, 255u, 255u, STATUS_LED_ANIM_PULSE},
};

static status_led_state_t s_state;
static uint32_t s_pulse_start;
static bool s_pulse_armed;

status_led_state_t status_led_resolve(uint32_t active_mask)
{
    for (unsigned state = 0u; state < (unsigned)STATUS_LED_STATE_COUNT; ++state) {
        if ((active_mask & (1u << state)) != 0u) {
            return (status_led_state_t)state;
        }
    }
    return STATUS_LED_STATE_COUNT;
}

status_led_style_t status_led_style(status_led_state_t state)
{
    if ((unsigned)state >= (unsigned)STATUS_LED_STATE_COUNT) {
        status_led_style_t off = {0u, 0u, 0u, STATUS_LED_ANIM_STEADY};
        return off;
    }
    return s_styles[state];
}

void status_led_reset(void)
{
    s_state = STATUS_LED_BOOT;
    s_pulse_start = 0u;
    s_pulse_armed = false;
}

void status_led_set_state(status_led_state_t state)
{
    s_state = state;
}

void status_led_note_command(uint32_t now_ms)
{
    s_pulse_start = now_ms;
    s_pulse_armed = true;
}

uint8_t status_led_brightness(status_led_anim_t anim, uint32_t now_ms)
{
    switch (anim) {
    case STATUS_LED_ANIM_BLINK:
        return ((now_ms % STATUS_LED_BLINK_MS) < (STATUS_LED_BLINK_MS / 2u))
                   ? 255u
                   : 0u;
    case STATUS_LED_ANIM_BREATHE: {
        uint32_t phase = now_ms % STATUS_LED_BREATHE_MS;
        uint32_t half = STATUS_LED_BREATHE_MS / 2u;
        uint32_t up = (phase < half) ? phase : (STATUS_LED_BREATHE_MS - phase);
        uint32_t span = 255u - STATUS_LED_MIN_V;

        return (uint8_t)(STATUS_LED_MIN_V + (span * up) / half);
    }
    case STATUS_LED_ANIM_STEADY:
    case STATUS_LED_ANIM_PULSE:
    default:
        return 255u;
    }
}

void status_led_frame(status_led_state_t state, uint32_t now_ms,
                      uint8_t out_grb[3])
{
    status_led_style_t style = status_led_style(state);
    uint8_t v = status_led_brightness(style.anim, now_ms);

    out_grb[0] = (uint8_t)((uint16_t)style.g * v / 255u);
    out_grb[1] = (uint8_t)((uint16_t)style.r * v / 255u);
    out_grb[2] = (uint8_t)((uint16_t)style.b * v / 255u);
}

status_led_state_t status_led_effective(uint32_t now_ms)
{
    /* Fault is critical and overrides even a command pulse. */
    if (s_state == STATUS_LED_FAULT) {
        return STATUS_LED_FAULT;
    }
    /* An armed pulse overrides the non-fault base until it expires, so an
     * accepted command is visible even while LEASED (see header). */
    if (s_pulse_armed &&
        (uint32_t)(now_ms - s_pulse_start) < STATUS_LED_COMMAND_PULSE_MS) {
        return STATUS_LED_COMMAND_PULSE;
    }
    return s_state;
}

/* --- WS2812 drive (target only). --- */
#if defined(CH32H417)

#include "ws2812.h"

static uint32_t s_last_push_ms;
static bool s_pushed;

void status_led_init(void)
{
    status_led_reset();
    ws2812_init();
    s_last_push_ms = 0u;
    s_pushed = false;
}

void status_led_poll(uint32_t now_ms)
{
    uint8_t grb[3];

    if (s_pushed &&
        (uint32_t)(now_ms - s_last_push_ms) < WS2812_MIN_INTERVAL_MS) {
        return; /* ~60 Hz cap. */
    }
    status_led_frame(status_led_effective(now_ms), now_ms, grb);
    ws2812_send_grb(grb);
    s_last_push_ms = now_ms;
    s_pushed = true;
}

#endif /* CH32H417 */
