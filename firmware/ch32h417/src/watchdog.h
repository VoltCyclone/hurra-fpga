#ifndef WATCHDOG_H
#define WATCHDOG_H

#include <stdint.h>

/* Health-gated independent watchdog.
 *
 * The IWDG is reloaded only when every required subsystem proved liveness in
 * the current window. A subsystem that wedges stops contributing its bit, the
 * window closes without a feed, and the IWDG resets the part. The gating logic
 * is MMIO-free and host-tested; the IWDG register poking is behind
 * `#if defined(CH32H417)`. */

enum watchdog_health {
    HEALTH_CDC_OR_NET = 1u << 0,   /* CDC/Ethernet service state machine polled. */
    HEALTH_SPI_PROGRESS = 1u << 1, /* SPI link retired a slot. */
    HEALTH_DMA_REARM = 1u << 2,    /* SPI DMA re-armed after a completion. */
};

/* All three must be observed within a window to earn a feed. The CDC bit means
 * the CDC/net state machine was serviced, not that a host is attached -- a
 * detached CDC still sets it. */
#define WATCHDOG_REQUIRED                                                      \
    ((uint32_t)HEALTH_CDC_OR_NET | (uint32_t)HEALTH_SPI_PROGRESS |             \
     (uint32_t)HEALTH_DMA_REARM)

/* Window length. Must be shorter than the IWDG timeout so a healthy system
 * always feeds in time, and long enough for every subsystem to tick once. */
#define WATCHDOG_WINDOW_MS 200u

typedef enum {
    WATCHDOG_HOLD = 0, /* Mid-window: take no action. */
    WATCHDOG_FEED,     /* Window closed, all bits seen: reload the IWDG. */
    WATCHDOG_STARVE,   /* Window closed, a bit missing: withhold the reload. */
} watchdog_action_t;

/* --- Hardware (target only). --- */

/* Start the IWDG at a timeout comfortably longer than WATCHDOG_WINDOW_MS. */
void watchdog_init(void);

/* Accumulate/evaluate against the live tick and reload the IWDG on FEED. */
void watchdog_poll(uint32_t now_ms);

/* --- Pure gating (host-testable). --- */

void watchdog_reset(uint32_t now_ms);

/* OR liveness bits into the current window. */
void watchdog_note(uint32_t health_bits);

/* At a window boundary, decide FEED vs STARVE and open the next window; return
 * HOLD before the boundary. */
watchdog_action_t watchdog_evaluate(uint32_t now_ms);

#endif
