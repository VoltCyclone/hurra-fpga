// CPU0 -> CPU1 shared SRAM. Step 5 carries liveness only; the seqlock snapshot
// from design doc section 5 is appended at step 6.

#ifndef HURRA_MCXN947_SHARED_WINDOW_H
#define HURRA_MCXN947_SHARED_WINDOW_H

#include <stdint.h>

#define SHARED_WINDOW_MAGIC 0x48555231u /* "HUR1" */

typedef struct {
    uint32_t magic;
    uint32_t cpu1_boot_count;
    uint32_t cpu1_heartbeat;
    uint32_t _reserved;
} shared_window_t;

_Static_assert(sizeof(shared_window_t) == 16u, "step-5 shared window must be 16 bytes");

extern volatile shared_window_t g_shared_window;

// The linker section is NOLOAD, so startup on neither core initializes it.
// CPU0 calls this after mcu_ready is raised and before CPU1 is released.
void shared_window_reset(void);

#endif  // HURRA_MCXN947_SHARED_WINDOW_H
