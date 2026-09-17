// Page composition and rasterization stay portable. The one trailing target
// guard owns only the panel-backed asynchronous pipeline.

#include "display.h"

#include "glyphs.h"
#include "injection_wire.h"

static char display_hex_digit(uint8_t value)
{
    return value < 10u ? (char)('0' + value)
                       : (char)('A' + (uint8_t)(value - 10u));
}

static void display_put_hex32(text_grid_t *grid, uint8_t row, uint8_t col,
                              uint32_t value, uint8_t digits, uint8_t attr)
{
    char text[11] = {'0', 'x', '0', '0', '0', '0', '0', '0', '0', '0', '\0'};
    for (uint8_t i = 0u; i < digits; ++i) {
        const uint8_t shift = (uint8_t)((digits - i - 1u) * 4u);
        text[2u + i] = display_hex_digit((uint8_t)((value >> shift) & 0x0fu));
    }
    text[2u + digits] = '\0';
    (void)text_grid_put(grid, row, col, text, attr);
}

static void display_page_row(text_grid_t *grid, uint8_t row,
                             const char *label)
{
    text_grid_fill_row(grid, row, ' ', DISPLAY_ATTR_NORMAL);
    (void)text_grid_put(grid, row, 1u, label, DISPLAY_ATTR_NORMAL);
}

void display_attr_rgb565(uint8_t attr, uint16_t *foreground,
                         uint16_t *background)
{
    *background = 0x0000u;
    switch (attr) {
    case DISPLAY_ATTR_HEADER:
        *foreground = 0xffffu;
        *background = 0x001fu;
        break;
    case DISPLAY_ATTR_SLOT:
        *foreground = 0xffe0u;
        break;
    case DISPLAY_ATTR_GOOD:
        *foreground = 0x07e0u;
        break;
    case DISPLAY_ATTR_FAULT:
        *foreground = 0xf800u;
        break;
    default:
        *foreground = 0xc618u;
        break;
    }
}

void display_compose_page(text_grid_t *grid, const link_snapshot_t *snapshot,
                          uint32_t cpu1_heartbeat)
{
    text_grid_fill_row(grid, 0u, ' ', DISPLAY_ATTR_HEADER);
    (void)text_grid_put(grid, 0u, 2u, "HURRA  MCXN947 LINK",
                        DISPLAY_ATTR_HEADER);

    display_page_row(grid, 2u, "SLOT COUNTER");
    text_grid_fill_row(grid, DISPLAY_SLOT_VALUE_ROW, ' ', DISPLAY_ATTR_SLOT);
    display_put_hex32(grid, DISPLAY_SLOT_VALUE_ROW, 2u,
                      snapshot->slot_counter, 8u, DISPLAY_ATTR_SLOT);

    display_page_row(grid, 5u, "LINK READY");
    const bool ready =
        (snapshot->link_flags & INJ_LINK_STATUS_FLAG_RELAY_READY) != 0u;
    (void)text_grid_put(grid, 5u, 16u, ready ? "YES" : "NO ",
                        ready ? DISPLAY_ATTR_GOOD : DISPLAY_ATTR_FAULT);

    display_page_row(grid, 6u, "LINK FLAGS");
    display_put_hex32(grid, 6u, 16u, snapshot->link_flags, 4u,
                      DISPLAY_ATTR_NORMAL);

    display_page_row(grid, 7u, "FAULT FLAGS");
    display_put_hex32(grid, 7u, 16u, snapshot->fault_flags, 4u,
                      snapshot->fault_flags == 0u ? DISPLAY_ATTR_GOOD
                                                  : DISPLAY_ATTR_FAULT);

    display_page_row(grid, 9u, "LAST RX SEQ");
    display_put_hex32(grid, 9u, 16u, snapshot->last_rx_sequence, 2u,
                      DISPLAY_ATTR_NORMAL);

    display_page_row(grid, 11u, "CPU1 HEARTBEAT");
    display_put_hex32(grid, 11u, 16u, cpu1_heartbeat, 8u,
                      DISPLAY_ATTR_NORMAL);

    display_page_row(grid, 12u, "SNAPSHOT SEQ");
    display_put_hex32(grid, 12u, 16u, snapshot->seq, 8u,
                      DISPLAY_ATTR_NORMAL);
}

bool display_rasterize_run(const text_grid_t *grid,
                           const text_grid_run_t *run, uint16_t *pixels,
                           size_t pixel_capacity)
{
    if (run->row >= TEXT_GRID_ROWS || run->length == 0u ||
        run->start_col >= TEXT_GRID_COLS ||
        (uint16_t)run->start_col + (uint16_t)run->length > TEXT_GRID_COLS) {
        return false;
    }

    const size_t row_pixels = (size_t)run->length * GLYPH_WIDTH;
    const size_t required = row_pixels * GLYPH_HEIGHT;
    if (pixels == NULL || pixel_capacity < required) {
        return false;
    }

    for (uint8_t glyph_row = 0u; glyph_row < GLYPH_HEIGHT; ++glyph_row) {
        size_t output = (size_t)glyph_row * row_pixels;
        for (uint8_t offset = 0u; offset < run->length; ++offset) {
            const uint8_t col = (uint8_t)(run->start_col + offset);
            const text_grid_cell_t *const cell =
                text_grid_cell_at(grid, run->row, col);
            const uint8_t *const glyph = glyphs_for_char(cell->character);
            uint16_t foreground;
            uint16_t background;
            display_attr_rgb565(cell->attr, &foreground, &background);

            for (uint8_t x = 0u; x < GLYPH_WIDTH; ++x) {
                pixels[output++] =
                    (glyph[glyph_row] & (uint8_t)(0x80u >> x)) != 0u
                        ? foreground
                        : background;
            }
        }
    }
    return true;
}

#if defined(MCXN947)

#include "display_panel.h"

#define DISPLAY_ROW_BUFFER_PIXELS \
    (DISPLAY_PANEL_WIDTH * GLYPH_HEIGHT)

_Static_assert(TEXT_GRID_COLS * GLYPH_WIDTH == DISPLAY_PANEL_WIDTH,
               "text columns must cover the panel width");
_Static_assert(TEXT_GRID_ROWS * GLYPH_HEIGHT == DISPLAY_PANEL_HEIGHT,
               "text rows must cover the panel height");

typedef struct {
    text_grid_t grid;
    _Alignas(32) uint16_t row_buffers[2][DISPLAY_ROW_BUFFER_PIXELS];
    text_grid_run_t staged_run;
    uint8_t staged_buffer;
    uint8_t write_buffer;
    bool staged;
    bool active;
    bool first_frame;
    bool available;
} display_state_t;

static display_state_t s_display;

static bool display_prepare_next(void)
{
    if (s_display.staged) {
        return true;
    }
    if (!text_grid_take_dirty_run(&s_display.grid,
                                  &s_display.staged_run)) {
        return false;
    }

    s_display.staged_buffer = s_display.write_buffer;
    if (!display_rasterize_run(
            &s_display.grid, &s_display.staged_run,
            s_display.row_buffers[s_display.staged_buffer],
            DISPLAY_ROW_BUFFER_PIXELS)) {
        s_display.active = false;
        return false;
    }
    s_display.staged = true;
    return true;
}

static void display_kick_prepared(void)
{
    if (!s_display.staged || display_panel_blit_busy()) {
        return;
    }

    const uint16_t x =
        (uint16_t)((uint16_t)s_display.staged_run.start_col * GLYPH_WIDTH);
    const uint16_t y =
        (uint16_t)((uint16_t)s_display.staged_run.row * GLYPH_HEIGHT);
    const uint16_t width =
        (uint16_t)((uint16_t)s_display.staged_run.length * GLYPH_WIDTH);
    if (!display_panel_blit(
            x, y, width, GLYPH_HEIGHT,
            s_display.row_buffers[s_display.staged_buffer])) {
        s_display.active = false;
        s_display.staged = false;
        return;
    }

    s_display.staged = false;
    s_display.write_buffer ^= 1u;
    // The just-started eDMA owns the other buffer. Rasterizing one following
    // run here is the ping-pong overlap required by section 2.
    (void)display_prepare_next();
}

// Bring-up aid, deliberately kept. Paints the whole panel one colour through
// exactly the same SelectArea + WritePixels path a real run uses.
//
// It exists because "blits succeed and the screen is black" is not a
// diagnosis: it is consistent with a dead bus AND with a renderer that emits
// nothing visible, and those have completely different fixes. A solid fill
// separates them in one look at the board -- colour means the transport works
// and the bug is above it. Every other check (pins, mux alternates, shifter
// and timer indices, baud divider, callback wiring) was read against NXP's own
// LVGL support file for this exact panel and matched, which is precisely why
// reading more code was not going to settle it.
static bool display_fill_screen(uint16_t rgb565)
{
    for (uint32_t i = 0u; i < DISPLAY_ROW_BUFFER_PIXELS; ++i) {
        s_display.row_buffers[0][i] = rgb565;
    }

    const uint16_t band = (uint16_t)(DISPLAY_ROW_BUFFER_PIXELS / DISPLAY_PANEL_WIDTH);
    for (uint16_t y = 0u; y < DISPLAY_PANEL_HEIGHT; y = (uint16_t)(y + band)) {
        uint16_t height = band;
        if ((uint32_t)y + height > DISPLAY_PANEL_HEIGHT) {
            height = (uint16_t)(DISPLAY_PANEL_HEIGHT - y);
        }
        if (!display_panel_blit(0u, y, DISPLAY_PANEL_WIDTH, height,
                                s_display.row_buffers[0])) {
            return false;
        }
        while (display_panel_blit_busy()) {
        }
    }
    return true;
}

bool display_init(void)
{
    text_grid_init(&s_display.grid, ' ', DISPLAY_ATTR_NORMAL);
    s_display.staged = false;
    s_display.active = false;
    s_display.first_frame = true;
    s_display.write_buffer = 0u;
    s_display.available = display_panel_init();

    // Paint the panel solid before any text. Two jobs: it proves the transport
    // reaches the glass (see display_fill_screen), and it clears the ST7796S's
    // power-on GRAM, which is undefined -- NXP's own bring-up clears video RAM
    // between ST7796S_Init and EnableDisplay for the same reason.
    if (s_display.available) {
        (void)display_fill_screen(DISPLAY_FILL_ON_INIT);
        // Hold it. The first rendered frame marks every cell dirty and repaints
        // the whole grid on a black background, so without this the fill is
        // gone in well under a second and whoever is watching the board cannot
        // say whether they saw it. CPU1 stalling here is free: it is
        // non-load-bearing by construction and the link cannot observe it.
        display_panel_delay_us(DISPLAY_FILL_HOLD_US);
    }
    return s_display.available;
}

bool display_start_frame(const link_snapshot_t *snapshot,
                         uint32_t cpu1_heartbeat)
{
    if (!s_display.available || !display_panel_ok() || s_display.active) {
        return false;
    }

    display_compose_page(&s_display.grid, snapshot, cpu1_heartbeat);
    if (s_display.first_frame) {
        text_grid_mark_all_dirty(&s_display.grid);
        s_display.first_frame = false;
    }

    s_display.active = true;
    if (!display_prepare_next()) {
        s_display.active = false;
        return true;
    }
    display_kick_prepared();
    return s_display.active;
}

void display_poll(void)
{
    if (!s_display.active) {
        return;
    }
    if (!display_panel_ok()) {
        s_display.available = false;
        s_display.active = false;
        s_display.staged = false;
        return;
    }
    if (display_panel_blit_busy()) {
        return;
    }
    if (!s_display.staged && !display_prepare_next()) {
        s_display.active = false;
        return;
    }
    display_kick_prepared();
}

bool display_available(void)
{
    return s_display.available && display_panel_ok();
}

bool display_render_active(void)
{
    return s_display.active;
}

#endif  // MCXN947
