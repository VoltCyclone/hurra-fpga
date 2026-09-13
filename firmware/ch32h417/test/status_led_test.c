#include <stdint.h>
#include <stdio.h>

#include "status_led.h"

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,            \
                    #condition);                                               \
            return 1;                                                          \
        }                                                                      \
    } while (0)

static int style_is(status_led_state_t state, uint8_t r, uint8_t g, uint8_t b,
                    status_led_anim_t anim)
{
    status_led_style_t s = status_led_style(state);

    return s.r == r && s.g == g && s.b == b && s.anim == anim;
}

static int frame_is(status_led_state_t state, uint32_t now_ms, uint8_t g,
                    uint8_t r, uint8_t b)
{
    uint8_t grb[3];

    status_led_frame(state, now_ms, grb);
    return grb[0] == g && grb[1] == r && grb[2] == b;
}

int main(void)
{
    /* --- Priority ladder: each state outranks every lower one. --- */
    CHECK(status_led_resolve(STATUS_LED_BIT(STATUS_LED_FAULT) |
                             STATUS_LED_BIT(STATUS_LED_BOOT)) ==
          STATUS_LED_FAULT);
    CHECK(status_led_resolve(STATUS_LED_BIT(STATUS_LED_BOOT) |
                             STATUS_LED_BIT(STATUS_LED_WAIT_MOUSE)) ==
          STATUS_LED_BOOT);
    CHECK(status_led_resolve(STATUS_LED_BIT(STATUS_LED_WAIT_MOUSE) |
                             STATUS_LED_BIT(STATUS_LED_PASSTHROUGH_ONLY)) ==
          STATUS_LED_WAIT_MOUSE);
    CHECK(status_led_resolve(STATUS_LED_BIT(STATUS_LED_PASSTHROUGH_ONLY) |
                             STATUS_LED_BIT(STATUS_LED_READY)) ==
          STATUS_LED_PASSTHROUGH_ONLY);
    CHECK(status_led_resolve(STATUS_LED_BIT(STATUS_LED_READY) |
                             STATUS_LED_BIT(STATUS_LED_LEASED)) ==
          STATUS_LED_READY);
    CHECK(status_led_resolve(STATUS_LED_BIT(STATUS_LED_LEASED) |
                             STATUS_LED_BIT(STATUS_LED_COMMAND_PULSE)) ==
          STATUS_LED_LEASED);
    /* Every bit set still resolves to the top of the ladder; empty is a sentinel. */
    CHECK(status_led_resolve(0xFFFFFFFFu) == STATUS_LED_FAULT);
    CHECK(status_led_resolve(0u) == STATUS_LED_STATE_COUNT);
    CHECK(status_led_resolve(STATUS_LED_BIT(STATUS_LED_COMMAND_PULSE)) ==
          STATUS_LED_COMMAND_PULSE);

    /* --- Exact color and animation per the spec table. --- */
    CHECK(style_is(STATUS_LED_FAULT, 255u, 0u, 0u, STATUS_LED_ANIM_BLINK));
    CHECK(style_is(STATUS_LED_BOOT, 255u, 176u, 0u, STATUS_LED_ANIM_BREATHE));
    CHECK(style_is(STATUS_LED_WAIT_MOUSE, 0u, 0u, 255u, STATUS_LED_ANIM_BREATHE));
    CHECK(style_is(STATUS_LED_PASSTHROUGH_ONLY, 0u, 255u, 255u,
                   STATUS_LED_ANIM_STEADY));
    CHECK(style_is(STATUS_LED_READY, 0u, 255u, 0u, STATUS_LED_ANIM_STEADY));
    CHECK(style_is(STATUS_LED_LEASED, 160u, 0u, 255u, STATUS_LED_ANIM_STEADY));
    CHECK(style_is(STATUS_LED_COMMAND_PULSE, 255u, 255u, 255u,
                   STATUS_LED_ANIM_PULSE));

    /* --- Runtime resolution. --- */
    status_led_reset();
    status_led_set_state(STATUS_LED_READY);
    CHECK(status_led_effective(0u) == STATUS_LED_READY);

    /* A command pulse overrides the non-fault base until it expires. */
    status_led_note_command(1000u);
    CHECK(status_led_effective(1000u) == STATUS_LED_COMMAND_PULSE);
    CHECK(status_led_effective(1000u + STATUS_LED_COMMAND_PULSE_MS - 1u) ==
          STATUS_LED_COMMAND_PULSE);
    CHECK(status_led_effective(1000u + STATUS_LED_COMMAND_PULSE_MS) ==
          STATUS_LED_READY);

    /* A leased base still shows the command pulse -- that is who sends commands. */
    status_led_set_state(STATUS_LED_LEASED);
    status_led_note_command(5000u);
    CHECK(status_led_effective(5000u) == STATUS_LED_COMMAND_PULSE);

    /* Fault wins over an armed pulse. */
    status_led_set_state(STATUS_LED_FAULT);
    status_led_note_command(9000u);
    CHECK(status_led_effective(9000u) == STATUS_LED_FAULT);

    /* --- Brightness curves. --- */
    CHECK(status_led_brightness(STATUS_LED_ANIM_STEADY, 12345u) == 255u);
    CHECK(status_led_brightness(STATUS_LED_ANIM_PULSE, 12345u) == 255u);
    CHECK(status_led_brightness(STATUS_LED_ANIM_BLINK, 0u) == 255u);
    CHECK(status_led_brightness(STATUS_LED_ANIM_BLINK, STATUS_LED_BLINK_MS / 2u) ==
          0u);
    CHECK(status_led_brightness(STATUS_LED_ANIM_BREATHE, 0u) == STATUS_LED_MIN_V);
    CHECK(status_led_brightness(STATUS_LED_ANIM_BREATHE,
                                STATUS_LED_BREATHE_MS / 2u) == 255u);

    /* --- Composed GRB frames (green-red-blue wire order). --- */
    CHECK(frame_is(STATUS_LED_READY, 0u, 255u, 0u, 0u));        /* green steady. */
    CHECK(frame_is(STATUS_LED_PASSTHROUGH_ONLY, 0u, 255u, 0u, 255u)); /* cyan. */
    CHECK(frame_is(STATUS_LED_LEASED, 0u, 0u, 160u, 255u));     /* purple steady. */
    CHECK(frame_is(STATUS_LED_FAULT, 0u, 0u, 255u, 0u));        /* red blink, on. */
    CHECK(frame_is(STATUS_LED_FAULT, STATUS_LED_BLINK_MS / 2u, 0u, 0u, 0u)); /* off. */
    CHECK(frame_is(STATUS_LED_BOOT, STATUS_LED_BREATHE_MS / 2u, 176u, 255u, 0u)); /* amber peak: g=176,r=255. */

    puts("status_led_test: all passed");
    return 0;
}
