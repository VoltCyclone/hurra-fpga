#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "shared_window.h"

int main(void)
{
    g_shared_window.magic = 0u;
    g_shared_window.cpu1_boot_count = UINT32_MAX;
    g_shared_window.cpu1_heartbeat = UINT32_MAX;
    g_shared_window._reserved = UINT32_MAX;

    shared_window_reset();

    assert(g_shared_window.magic == SHARED_WINDOW_MAGIC);
    assert(g_shared_window.cpu1_boot_count == 0u);
    assert(g_shared_window.cpu1_heartbeat == 0u);
    assert(g_shared_window._reserved == 0u);
    printf("shared_window_test: ok\n");
    return 0;
}
