#include "shared_window.h"

#if defined(MCXN947)
volatile shared_window_t g_shared_window
    __attribute__((section(".noinit.$rpmsg_sh_mem")));
#else
// Mach-O section attributes require a segment/section pair. The host test is
// proving reset semantics, not linker placement; both target images take the
// ELF spelling above and the artifact gate proves its absolute address.
volatile shared_window_t g_shared_window;
#endif

void shared_window_reset(void)
{
    g_shared_window.magic = 0u;
    g_shared_window.cpu1_boot_count = 0u;
    g_shared_window.cpu1_heartbeat = 0u;
    g_shared_window.cpu1_seen_slot_counter = 0u;
    g_shared_window.cpu1_frames = 0u;
    g_shared_window.cpu1_display_flags = 0u;
    g_shared_window.cpu1_blits = 0u;
    g_shared_window.cpu1_blit_rejects = 0u;
    g_shared_window.snapshot = (link_snapshot_t){0};

    // Magic is the publication store: readers never observe a valid window
    // whose prefix or snapshot still contains NOLOAD residue.
    g_shared_window.magic = SHARED_WINDOW_MAGIC;
}
