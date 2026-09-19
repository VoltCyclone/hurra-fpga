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

    // Only the descriptor block's header is cleared, not its 2 KB of data.
    // length = 0 is what makes the data unreadable, and a reader that honoured
    // the header but not the length would be broken with or without a zeroed
    // buffer. Clearing 2 KB of NOLOAD residue here would put it on the boot
    // path between mcu_ready and the foreground loop, which section 10 is
    // explicit about keeping empty.
    g_shared_window.descriptor.sequence = 0u;
    g_shared_window.descriptor.generation = 0u;
    g_shared_window.descriptor.length = 0u;
    g_shared_window.descriptor.interface_number = 0u;

    // sequence = 0 is "nothing classified yet", which CPU1 renders as neither
    // a fault nor a clean bill of health. Cleared in full because the block is
    // eight bytes, unlike the descriptor's 2 KB of data beside it.
    g_shared_window.fault.sequence = 0u;
    g_shared_window.fault.verdict = 0u;
    g_shared_window.fault.suspect_count = 0u;
    for (uint32_t index = 0u; index < SHARED_FAULT_MAX_SUSPECTS; ++index) {
        g_shared_window.fault.suspect[index] = 0u;
    }

    // Magic is the publication store: readers never observe a valid window
    // whose prefix or snapshot still contains NOLOAD residue.
    g_shared_window.magic = SHARED_WINDOW_MAGIC;
}
