#include <stdint.h>
#include <stdio.h>

#include "watchdog.h"

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,            \
                    #condition);                                               \
            return 1;                                                          \
        }                                                                      \
    } while (0)

int main(void)
{
    /* --- All required bits within a window earns a feed at the boundary. --- */
    watchdog_reset(0u);
    watchdog_note(WATCHDOG_REQUIRED);
    CHECK(watchdog_evaluate(WATCHDOG_WINDOW_MS - 1u) == WATCHDOG_HOLD);
    CHECK(watchdog_evaluate(WATCHDOG_WINDOW_MS) == WATCHDOG_FEED);

    /* --- The bits OR-accumulate across separate notes in one window. --- */
    watchdog_reset(0u);
    watchdog_note(HEALTH_CDC_OR_NET);
    watchdog_note(HEALTH_SPI_PROGRESS);
    watchdog_note(HEALTH_DMA_REARM);
    CHECK(watchdog_evaluate(WATCHDOG_WINDOW_MS) == WATCHDOG_FEED);

    /* --- A missing bit starves the watchdog. --- */
    watchdog_reset(0u);
    watchdog_note(HEALTH_CDC_OR_NET | HEALTH_SPI_PROGRESS); /* no DMA rearm. */
    CHECK(watchdog_evaluate(WATCHDOG_WINDOW_MS) == WATCHDOG_STARVE);

    watchdog_reset(0u);
    watchdog_note(HEALTH_DMA_REARM); /* only DMA. */
    CHECK(watchdog_evaluate(WATCHDOG_WINDOW_MS) == WATCHDOG_STARVE);

    watchdog_reset(0u);
    /* No notes at all. */
    CHECK(watchdog_evaluate(WATCHDOG_WINDOW_MS) == WATCHDOG_STARVE);

    /* --- The window resets after each boundary, feed or starve. --- */
    watchdog_reset(0u);
    watchdog_note(WATCHDOG_REQUIRED);
    CHECK(watchdog_evaluate(WATCHDOG_WINDOW_MS) == WATCHDOG_FEED);
    /* New window: stale bits did not carry over, so a bare boundary starves. */
    CHECK(watchdog_evaluate(2u * WATCHDOG_WINDOW_MS) == WATCHDOG_STARVE);
    /* And a fresh full window feeds again. */
    watchdog_note(WATCHDOG_REQUIRED);
    CHECK(watchdog_evaluate(3u * WATCHDOG_WINDOW_MS) == WATCHDOG_FEED);

    puts("watchdog_test: all passed");
    return 0;
}
