#include <stdint.h>
#include <stdio.h>

#include "usb_time.h"

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
    usb_time_snapshot_t snap;

    /* --- Wrap-aware frame comparison. --- */
    CHECK(usb_time_reached(0x0001u, 0xFFFFu));  /* 1 is two frames past 0xFFFF. */
    CHECK(!usb_time_reached(0xFFFEu, 0x0001u)); /* 0xFFFE is three frames short. */
    CHECK(usb_time_reached(5u, 5u));            /* Equal counts as reached. */
    CHECK(usb_time_reached(6u, 5u));            /* Just past. */
    CHECK(!usb_time_reached(4u, 5u));           /* Just short. */
    CHECK(usb_time_reached(0x7FFFu, 0u));       /* Last "ahead" value. */
    CHECK(!usb_time_reached(0x8000u, 0u));      /* First "behind" value. */

    /* --- Sync acquisition needs two edges to measure a period. --- */
    usb_time_reset();
    snap = usb_time_snapshot();
    CHECK(snap.frame == 0u);
    CHECK(!snap.synchronized);

    usb_time_on_capture(1000u);
    snap = usb_time_snapshot();
    CHECK(snap.frame == 1u);
    CHECK(!snap.synchronized); /* One edge: no period yet. */

    usb_time_on_capture(2000u);
    snap = usb_time_snapshot();
    CHECK(snap.frame == 2u);
    CHECK(snap.synchronized);
    CHECK(snap.period_ticks == 1000u);
    CHECK(snap.capture_ticks == 2000u);

    /* A steady cadence keeps the period stable through the filter. */
    usb_time_on_capture(3000u);
    usb_time_on_capture(4000u);
    snap = usb_time_snapshot();
    CHECK(snap.synchronized);
    CHECK(snap.period_ticks == 1000u);
    CHECK(snap.frame == 4u);

    /* --- Losing three SOFs clears synchronization; two does not. --- */
    /* last capture at 4000, period 1000. Two frames missing (elapsed 2000). */
    usb_time_poll_at(6000u);
    CHECK(usb_time_snapshot().synchronized);
    /* Three frames missing (elapsed 3000) drops sync. */
    usb_time_poll_at(7000u);
    CHECK(!usb_time_snapshot().synchronized);

    /* --- A recovered pulse restarts the period filter rather than folding in
     *     the outage gap. --- */
    usb_time_on_capture(20000u); /* Re-baseline; gap to 4000 is not a period. */
    snap = usb_time_snapshot();
    CHECK(!snap.synchronized);   /* One edge since recovery: still no period. */
    CHECK(snap.frame == 5u);     /* Frame counter keeps advancing across loss. */

    usb_time_on_capture(20500u); /* First valid post-recovery period is 500. */
    snap = usb_time_snapshot();
    CHECK(snap.synchronized);
    CHECK(snap.period_ticks == 500u); /* Restarted: not blended with the old 1000. */
    CHECK(snap.frame == 6u);

    /* Poll before any sync must not fault or clear anything. */
    usb_time_reset();
    usb_time_poll_at(999999u);
    CHECK(!usb_time_snapshot().synchronized);

    puts("usb_time_test: all passed");
    return 0;
}
