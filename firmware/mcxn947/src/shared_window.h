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
    link_snapshot_t snapshot;
} shared_window_t;

_Static_assert(sizeof(shared_window_t) == 48u, "step-6 shared window must be 48 bytes");

extern volatile shared_window_t g_shared_window;

// The linker section is NOLOAD, so startup on neither core initializes it.
// CPU0 calls this after mcu_ready is raised and before CPU1 is released.
void shared_window_reset(void);

#endif  // HURRA_MCXN947_SHARED_WINDOW_H
