#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "display_panel.h"

int main(void)
{
    uint16_t end_x = 0u;
    uint16_t end_y = 0u;
    uint32_t pixels = 0u;

    assert(display_panel_rect(0u, 0u, DISPLAY_PANEL_WIDTH,
                              DISPLAY_PANEL_HEIGHT, &end_x, &end_y, &pixels));
    assert(end_x == 479u);
    assert(end_y == 319u);
    assert(pixels == 153600u);

    assert(display_panel_rect(472u, 304u, 8u, 16u, &end_x, &end_y, &pixels));
    assert(end_x == 479u);
    assert(end_y == 319u);
    assert(pixels == 128u);

    assert(!display_panel_rect(0u, 0u, 0u, 1u, &end_x, &end_y, &pixels));
    assert(!display_panel_rect(0u, 0u, 1u, 0u, &end_x, &end_y, &pixels));
    assert(!display_panel_rect(479u, 319u, 2u, 1u, &end_x, &end_y, &pixels));
    assert(!display_panel_rect(0u, 319u, 1u, 2u, &end_x, &end_y, &pixels));
    assert(!display_panel_rect(480u, 0u, 1u, 1u, &end_x, &end_y, &pixels));

    printf("display_panel_test: ok\n");
    return 0;
}
