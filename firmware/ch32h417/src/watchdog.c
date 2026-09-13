#include "watchdog.h"

/* --- Pure gating (MMIO-free; host-testable). --- */

static uint32_t s_accum;        /* Health bits observed in the current window. */
static uint32_t s_window_start; /* Tick at which the current window opened. */

void watchdog_reset(uint32_t now_ms)
{
    s_accum = 0u;
    s_window_start = now_ms;
}

void watchdog_note(uint32_t health_bits)
{
    s_accum |= health_bits;
}

watchdog_action_t watchdog_evaluate(uint32_t now_ms)
{
    watchdog_action_t action;

    if ((uint32_t)(now_ms - s_window_start) < WATCHDOG_WINDOW_MS) {
        return WATCHDOG_HOLD;
    }

    action = ((s_accum & WATCHDOG_REQUIRED) == WATCHDOG_REQUIRED) ? WATCHDOG_FEED
                                                                  : WATCHDOG_STARVE;
    s_accum = 0u;
    s_window_start = now_ms;
    return action;
}

/* --- IWDG driver (target only). ---
 *
 * The IWDG runs off the LSI, so its exact timeout depends on the LSI trim; the
 * reload below targets ~1 s and the only hard invariant is that it comfortably
 * exceeds WATCHDOG_WINDOW_MS. Final tuning is a Task 6 bench item. The gating
 * state starts from its static zero, so the first window is measured from the
 * first poll rather than needing a reset here. */
#if defined(CH32H417)

#include "ch32h417_port.h"

void watchdog_init(void)
{
    IWDG_WriteAccessCmd(IWDG_WriteAccess_Enable);
    IWDG_SetPrescaler(IWDG_Prescaler_64);
    IWDG_SetReload(0x0271u); /* ~1 s nominal; must exceed WATCHDOG_WINDOW_MS. */
    IWDG_ReloadCounter();
    IWDG_Enable();
}

void watchdog_poll(uint32_t now_ms)
{
    if (watchdog_evaluate(now_ms) == WATCHDOG_FEED) {
        IWDG_ReloadCounter();
    }
}

#endif /* CH32H417 */
