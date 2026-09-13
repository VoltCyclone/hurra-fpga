#ifndef STATUS_LED_H
#define STATUS_LED_H

#include <stdint.h>

/* NeoPixel status policy (design spec section 13).
 *
 * A single WS2812 conveys the V5F's operational state by color and animation.
 * The state priority and color/animation table are MMIO-free so they host-
 * compile and are checked by test/status_led_test.c; the WS2812/PIOC drive is
 * behind `#if defined(CH32H417)` and only builds for the target. */

/* Ordered highest priority (0) to lowest, matching the spec's ladder:
 * FAULT > BOOT > WAIT_MOUSE > PASSTHROUGH_ONLY > READY > LEASED > COMMAND_PULSE. */
typedef enum {
    STATUS_LED_FAULT = 0,        /* red blink:      active fault. */
    STATUS_LED_BOOT,             /* amber breathe:  booting or waiting for FPGA. */
    STATUS_LED_WAIT_MOUSE,       /* blue breathe:   link up, waiting for a HID device. */
    STATUS_LED_PASSTHROUGH_ONLY, /* cyan steady:    passthrough; no valid map. */
    STATUS_LED_READY,            /* green steady:   map valid; ready; no lease. */
    STATUS_LED_LEASED,           /* purple steady:  CDC/KMBoxNet owns the lease. */
    STATUS_LED_COMMAND_PULSE,    /* white pulse:    command accepted (transient). */
    STATUS_LED_STATE_COUNT,      /* Also the "no condition active" sentinel. */
} status_led_state_t;

typedef enum {
    STATUS_LED_ANIM_STEADY = 0,
    STATUS_LED_ANIM_BREATHE,
    STATUS_LED_ANIM_BLINK,
    STATUS_LED_ANIM_PULSE,
} status_led_anim_t;

typedef struct {
    uint8_t r;
    uint8_t g;
    uint8_t b;
    status_led_anim_t anim;
} status_led_style_t;

#define STATUS_LED_BIT(state) (1u << (unsigned)(state))

/* Brief white flash duration for an accepted command. */
#define STATUS_LED_COMMAND_PULSE_MS 120u

/* Animation timing. Breathe is a full triangle cycle; blink is a square wave. */
#define STATUS_LED_BREATHE_MS 2000u
#define STATUS_LED_BLINK_MS 500u
#define STATUS_LED_MIN_V 8u /* Breathe trough brightness. */

/* --- Pure policy (host-testable). --- */

/* Highest-priority (lowest-enum) state whose bit is set in `active_mask`, or
 * STATUS_LED_STATE_COUNT when none is set. This is the literal spec ladder and
 * is what verifies the priority order. */
status_led_state_t status_led_resolve(uint32_t active_mask);

/* Color (full brightness) and animation for a state. */
status_led_style_t status_led_style(status_led_state_t state);

/* Animation brightness (0..255) for `anim` at `now_ms`: steady/pulse full,
 * blink square wave, breathe triangle between STATUS_LED_MIN_V and full. */
uint8_t status_led_brightness(status_led_anim_t anim, uint32_t now_ms);

/* Compose the WS2812 GRB frame for `state` at `now_ms`: the state's color scaled
 * by its animation brightness, emitted green-red-blue. */
void status_led_frame(status_led_state_t state, uint32_t now_ms,
                      uint8_t out_grb[3]);

/* --- Stateful policy (host-testable). --- */

void status_led_reset(void);

/* Set the persistent operational state (any of the seven; callers use FAULT..
 * LEASED, and internally FAULT still wins over a pulse). */
void status_led_set_state(status_led_state_t state);

/* Arm the transient command-accepted pulse, timestamped at `now_ms`. */
void status_led_note_command(uint32_t now_ms);

/* Resolve what to show now. Refinement of the spec ladder: FAULT overrides all,
 * an armed pulse overrides any non-fault base (so command-accept stays visible
 * even while LEASED), otherwise the base state shows. */
status_led_state_t status_led_effective(uint32_t now_ms);

/* --- Hardware (target only). --- */

/* Bring up the WS2812/PIOC output. */
void status_led_init(void);

/* Compose and push the current frame, capped at ~60 Hz. */
void status_led_poll(uint32_t now_ms);

#endif
