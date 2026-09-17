// CPU0 -> CPU1 shared SRAM. Step 6 preserves the four-word step-5 prefix and
// appends the seqlock snapshot from design doc section 5.

#ifndef HURRA_MCXN947_SHARED_WINDOW_H
#define HURRA_MCXN947_SHARED_WINDOW_H

#include <stdint.h>

#include "link_snapshot.h"

#define SHARED_WINDOW_MAGIC 0x48555231u /* "HUR1" */

typedef struct {
    uint32_t magic;
    uint32_t cpu1_boot_count;
    uint32_t cpu1_heartbeat;
    // Section 5 makes a stale LPCAC read cheap and visible on a display. This
    // echo makes it numeric before the panel exists: CPU1 writes only what it
    // actually read, CPU0 observes it but never waits on it or treats it as a
    // command, exactly like the heartbeat word beside it.
    uint32_t cpu1_seen_slot_counter;

    // Frames CPU1 has completed, and what its panel is doing. Same category
    // again: CPU1 data that CPU0 observes and never waits on.
    //
    // Section 9 step 7's gate is "20 Hz repaint with link counters still flat",
    // which is a RATE, and a rate cannot be read off a panel by looking at it.
    // Without this the only evidence the display works is a human saying it
    // looks right, which is exactly what step 6 replaced for the LPCAC
    // question. `stats` prints frames/s next to the link counters so both
    // halves of that gate are one measurement.
    uint32_t cpu1_frames;
    uint32_t cpu1_display_flags;
    uint32_t cpu1_blits;
    uint32_t cpu1_blit_rejects;

    // The CPU0 -> CPU1 payload proper. Kept last so every CPU1 -> CPU0
    // observation word above it keeps its offset when the snapshot grows.
    link_snapshot_t snapshot;
} shared_window_t;

// Bit 0 -- the panel initialised. Clear means display_init() failed, and the
// distinction matters: CPU1 deliberately keeps its heartbeat, echo and LED
// running with a dead panel, so a blank screen with a live heartbeat is
// otherwise indistinguishable from a panel that is simply not plugged in.
#define SHARED_DISPLAY_FLAG_READY 0x00000001u
// Bit 1 -- a blit was rejected or the transport reported an error since boot.
#define SHARED_DISPLAY_FLAG_FAULT 0x00000002u

_Static_assert(sizeof(shared_window_t) == 64u, "step-7 shared window must be 64 bytes");

extern volatile shared_window_t g_shared_window;

// The linker section is NOLOAD, so startup on neither core initializes it.
// CPU0 calls this after mcu_ready is raised and before CPU1 is released.
void shared_window_reset(void);

#endif  // HURRA_MCXN947_SHARED_WINDOW_H
