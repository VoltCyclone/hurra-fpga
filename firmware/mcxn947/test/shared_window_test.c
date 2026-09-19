#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "shared_window.h"

int main(void)
{
    g_shared_window.magic = 0u;
    g_shared_window.cpu1_boot_count = UINT32_MAX;
    g_shared_window.cpu1_heartbeat = UINT32_MAX;
    g_shared_window.cpu1_seen_slot_counter = UINT32_MAX;
    memset((void *)&g_shared_window.snapshot, 0xFF,
           sizeof(g_shared_window.snapshot));

    shared_window_reset();

    assert(g_shared_window.magic == SHARED_WINDOW_MAGIC);
    assert(g_shared_window.cpu1_boot_count == 0u);
    assert(g_shared_window.cpu1_heartbeat == 0u);
    assert(g_shared_window.cpu1_seen_slot_counter == 0u);
    const link_snapshot_t zero = {0};
    assert(memcmp((const void *)&g_shared_window.snapshot, &zero, sizeof(zero)) == 0);

    // The descriptor block's HEADER is reset; its 2 KB of data deliberately is
    // not, because length == 0 is what makes the data unreadable and clearing
    // it would put a 2 KB memset on the boot path. A reader that trusted the
    // header but not the length would be broken either way.
    assert(g_shared_window.descriptor.sequence == 0u);
    assert(g_shared_window.descriptor.generation == 0u);
    assert(g_shared_window.descriptor.length == 0u);
    assert(g_shared_window.descriptor.interface_number == 0u);

    // The window is the step-7 prefix plus one descriptor block, and the same
    // numbers are SHARED_WINDOW_SIZE in the Makefile, which is passed to both
    // the merge tool and check_mcxn947_images.py.
    // sequence == 0 is "nothing classified yet", which CPU1 must be able to
    // tell apart from a published verdict of OK -- otherwise a board that has
    // never run the classifier looks like a board with a healthy link.
    assert(g_shared_window.fault.sequence == 0u);
    assert(g_shared_window.fault.verdict == 0u);
    assert(g_shared_window.fault.suspect_count == 0u);
    assert(g_shared_window.fault.suspect[0] == 0u);
    assert(g_shared_window.fault.suspect[1] == 0u);

    assert(sizeof(shared_descriptor_t) == 2060u);
    assert(sizeof(shared_link_fault_t) == 8u);
    assert(sizeof(shared_window_t) == 2132u);
    assert(SHARED_DESCRIPTOR_CAPACITY == 2048u);
    printf("shared_window_test: ok\n");
    return 0;
}
