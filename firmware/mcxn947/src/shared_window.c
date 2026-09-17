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
    g_shared_window._reserved = 0u;

    // Magic is the publication store: readers never observe a valid window
    // whose step-5 payload still contains NOLOAD residue.
    g_shared_window.magic = SHARED_WINDOW_MAGIC;
}
