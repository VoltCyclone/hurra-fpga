// CPU1 status-page composition and two-buffer incremental renderer.

#ifndef HURRA_MCXN947_DISPLAY_H
#define HURRA_MCXN947_DISPLAY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "link_snapshot.h"
#include "text_grid.h"

// Colour painted over the whole panel at init. Red while the bus is being
// brought up, because a red screen is unmistakable evidence that pixels reach
// the glass; set it to 0x0000 once the renderer is trusted.
#define DISPLAY_FILL_ON_INIT 0xf800u

// How long the init fill stays up before the first text frame.
#define DISPLAY_FILL_HOLD_US 3000000u

#define DISPLAY_SLOT_VALUE_ROW 3u

enum {
    DISPLAY_ATTR_NORMAL = 0u,
    DISPLAY_ATTR_HEADER = 1u,
    DISPLAY_ATTR_SLOT = 2u,
    DISPLAY_ATTR_GOOD = 3u,
    DISPLAY_ATTR_FAULT = 4u,
};

void display_attr_rgb565(uint8_t attr, uint16_t *foreground,
                         uint16_t *background);
void display_compose_page(text_grid_t *grid, const link_snapshot_t *snapshot,
                          uint32_t cpu1_heartbeat);
bool display_rasterize_run(const text_grid_t *grid,
                           const text_grid_run_t *run, uint16_t *pixels,
                           size_t pixel_capacity);

bool display_init(void);
bool display_start_frame(const link_snapshot_t *snapshot,
                         uint32_t cpu1_heartbeat);
void display_poll(void);
bool display_available(void);
bool display_render_active(void);

#endif  // HURRA_MCXN947_DISPLAY_H
