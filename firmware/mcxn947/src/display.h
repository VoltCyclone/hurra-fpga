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

// Page geometry. The grid is TEXT_GRID_COLS x TEXT_GRID_ROWS (60x20); the page
// is split into a readable health pane on the left and a raw register image on
// the right.
#define DISPLAY_SPLIT_COL 30u
#define DISPLAY_HEALTH_VALUE_COL 12u
#define DISPLAY_REG_LABEL_COL 32u
#define DISPLAY_REG_VALUE_COL 40u

// The FPGA link delivers one slot every 125 us. This is the only clock CPU1
// has -- see display_report_rate().
#define DISPLAY_SLOT_HZ 8000u

enum {
    DISPLAY_ATTR_NORMAL = 0u,
    DISPLAY_ATTR_HEADER = 1u,
    DISPLAY_ATTR_SLOT = 2u,
    DISPLAY_ATTR_GOOD = 3u,
    DISPLAY_ATTR_FAULT = 4u,
};

void display_attr_rgb565(uint8_t attr, uint16_t *foreground,
                         uint16_t *background);
// Reports per second from a slot delta. Pure, so it is host-tested directly.
uint32_t display_report_rate(uint32_t delta_reports, uint32_t delta_slots);

// Rows 14..19 are the decoded HID report descriptor. They were free -- the
// health and register panes end at row 13 -- so this pane costs the existing
// page nothing and needs no mode switch on a core with no input.
#define DISPLAY_DESC_HEADER_ROW 14u
#define DISPLAY_DESC_FIRST_ROW 15u
#define DISPLAY_DESC_LAST_ROW 19u
// Two columns of decoded lines per row. The decoder emits lines of at most 30
// characters, and the grid is 60 wide, so a second column doubles what fits for
// the price of one more base offset.
#define DISPLAY_DESC_COL_A 0u
#define DISPLAY_DESC_COL_B 30u
#define DISPLAY_DESC_LINES \
    (((DISPLAY_DESC_LAST_ROW - DISPLAY_DESC_FIRST_ROW) + 1u) * 2u)

// What CPU1 knows about the captured descriptor. A plain by-value struct rather
// than a pointer into the shared window: display.c stays portable and
// host-testable, and never learns that shared memory exists. `bytes` is NULL
// when nothing has been published, which is the state at boot and after a
// re-enumeration.
typedef struct {
    const uint8_t *bytes;
    uint16_t length;
    uint16_t generation;
    uint8_t interface_number;
} display_descriptor_t;

// What CPU1 knows about the link fault classifier's verdict. By value, for the
// same reason as display_descriptor_t above. `sequence == 0` means nothing has
// been classified yet, which is NOT the same as a verdict of OK and must not
// render as a clean link.
//
// `verdict` holds a link_fault_verdict_t and each `suspect` a link_pin_t; they
// are bytes here because this struct mirrors the cross-core block, where an
// enum's width would be a compiler's choice rather than a contract.
typedef struct {
    uint32_t sequence;
    uint8_t verdict;
    uint8_t suspect_count;
    uint8_t suspect[2];
} display_fault_t;

// `fault` may be NULL, which renders the descriptor pane as before.
//
// When a fault IS present the pane is given over to it. The two never compete
// for the rows: a descriptor can only arrive over a working link, so whenever
// there is a fault worth reporting that pane is necessarily empty anyway.
void display_compose_page(text_grid_t *grid, const link_snapshot_t *snapshot,
                          uint32_t cpu1_heartbeat, uint32_t reports_per_sec,
                          bool link_alive, const display_descriptor_t *descriptor,
                          const display_fault_t *fault);
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
