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
    assert(sizeof(shared_window_t) == 56u);
    printf("shared_window_test: ok\n");
    return 0;
}
