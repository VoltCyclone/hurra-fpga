#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "display.h"
#include "glyphs.h"
#include "injection_wire.h"

static void assert_text(const text_grid_t *grid, uint8_t row, uint8_t col,
                        const char *expected)
{
    while (*expected != '\0') {
        const text_grid_cell_t *const cell = text_grid_cell_at(grid, row, col);
        assert(cell != NULL);
        assert(cell->character == (uint8_t)*expected);
        ++expected;
        ++col;
    }
}

static void test_page_contains_required_snapshot_fields(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    const link_snapshot_t snapshot = {
        .seq = 0x2468ACE0u,
        .slot_counter = 0x1234ABCDu,
        .native_report_count = 1234u,
        .usb_frame = 0x04D2u,
        .usb_subframe = 5u,
        .link_flags = 0x0008u,
        .descriptor_generation = 3u,
        .map_generation = 7u,
        .fault_flags = 0x55AAu,
        .last_rx_sequence = 0x5Au,
    };

    display_compose_page(&grid, &snapshot, 0x10203040u, 998u, true);

    // Right pane: the raw register image, every snapshot field verbatim.
    assert_text(&grid, 3u, DISPLAY_REG_VALUE_COL, "0x1234ABCD");
    assert_text(&grid, 4u, DISPLAY_REG_VALUE_COL, "0x2468ACE0");
    assert_text(&grid, 5u, DISPLAY_REG_VALUE_COL, "0x000004D2");
    assert_text(&grid, 6u, DISPLAY_REG_VALUE_COL, "0x0008");
    assert_text(&grid, 7u, DISPLAY_REG_VALUE_COL, "0x55AA");
    assert_text(&grid, 8u, DISPLAY_REG_VALUE_COL, "0x5A");
    assert_text(&grid, 9u, DISPLAY_REG_VALUE_COL, "0x04D2");
    assert_text(&grid, 10u, DISPLAY_REG_VALUE_COL, "0x0005");
    assert_text(&grid, 11u, DISPLAY_REG_VALUE_COL, "0x0003");
    assert_text(&grid, 12u, DISPLAY_REG_VALUE_COL, "0x0007");
    assert_text(&grid, 13u, DISPLAY_REG_VALUE_COL, "0x10203040");

    // Left pane: decoded and decimal.
    assert_text(&grid, 3u, DISPLAY_HEALTH_VALUE_COL, "READY");
    assert_text(&grid, 5u, DISPLAY_HEALTH_VALUE_COL, "      1234");
    assert_text(&grid, 6u, DISPLAY_HEALTH_VALUE_COL, "   998");
    assert_text(&grid, 10u, DISPLAY_HEALTH_VALUE_COL, "ALIVE");

    // The slot counter stays the eye-catching colour it had before the split.
    assert(text_grid_cell_at(&grid, 3u, DISPLAY_REG_VALUE_COL)->attr ==
           DISPLAY_ATTR_SLOT);
    // A non-zero fault must read as a fault on both sides.
    assert(text_grid_cell_at(&grid, 4u, DISPLAY_HEALTH_VALUE_COL)->attr ==
           DISPLAY_ATTR_FAULT);
    assert(text_grid_cell_at(&grid, 7u, DISPLAY_REG_VALUE_COL)->attr ==
           DISPLAY_ATTR_FAULT);

    // The column rule survives the left pane's row fills.
    assert(text_grid_cell_at(&grid, 3u, DISPLAY_SPLIT_COL)->character ==
           (uint8_t)'|');
}

static void test_clean_snapshot_reads_as_healthy(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    const link_snapshot_t snapshot = {
        .link_flags = INJ_LINK_STATUS_FLAG_RELAY_READY,
        .fault_flags = 0u,
    };

    display_compose_page(&grid, &snapshot, 0u, 0u, true);

    assert_text(&grid, 4u, DISPLAY_HEALTH_VALUE_COL, "NONE");
    assert(text_grid_cell_at(&grid, 4u, DISPLAY_HEALTH_VALUE_COL)->attr ==
           DISPLAY_ATTR_GOOD);
    assert(text_grid_cell_at(&grid, 3u, DISPLAY_HEALTH_VALUE_COL)->attr ==
           DISPLAY_ATTR_GOOD);

    // A stalled link is a fault even when nothing else is wrong.
    display_compose_page(&grid, &snapshot, 0u, 0u, false);
    assert_text(&grid, 10u, DISPLAY_HEALTH_VALUE_COL, "STALLED");
    assert(text_grid_cell_at(&grid, 10u, DISPLAY_HEALTH_VALUE_COL)->attr ==
           DISPLAY_ATTR_FAULT);
}

static void test_report_rate_scales_by_the_slot_clock(void)
{
    // One report per slot is DISPLAY_SLOT_HZ reports per second.
    assert(display_report_rate(8000u, 8000u) == DISPLAY_SLOT_HZ);
    // Half a report per slot.
    assert(display_report_rate(4000u, 8000u) == DISPLAY_SLOT_HZ / 2u);
    // A realistic 1 kHz mouse against the 8 kHz slot clock.
    assert(display_report_rate(1000u, 8000u) == 1000u);
    // No elapsed slots cannot yield a rate, and must not divide by zero.
    assert(display_report_rate(1234u, 0u) == 0u);
    // No reports is a real answer, not a missing one.
    assert(display_report_rate(0u, 8000u) == 0u);
    // The intermediate product must not overflow 32 bits before dividing.
    assert(display_report_rate(0xffffffffu, 0xffffffffu) == DISPLAY_SLOT_HZ);
}

static void test_rasterizer_uses_glyph_bits_and_attribute_colors(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    assert(text_grid_set_cell(&grid, 2u, 3u, 'A', DISPLAY_ATTR_SLOT));
    const text_grid_run_t run = {.row = 2u, .start_col = 3u, .length = 1u};
    uint16_t pixels[GLYPH_WIDTH * GLYPH_HEIGHT];
    uint16_t foreground;
    uint16_t background;

    display_attr_rgb565(DISPLAY_ATTR_SLOT, &foreground, &background);
    assert(display_rasterize_run(&grid, &run, pixels,
                                 sizeof(pixels) / sizeof(pixels[0])));

    const uint8_t *const glyph = glyphs_for_char((uint8_t)'A');
    for (uint8_t y = 0u; y < GLYPH_HEIGHT; ++y) {
        for (uint8_t x = 0u; x < GLYPH_WIDTH; ++x) {
            const uint16_t expected =
                (glyph[y] & (uint8_t)(0x80u >> x)) != 0u ? foreground
                                                         : background;
            assert(pixels[(size_t)y * GLYPH_WIDTH + x] == expected);
        }
    }
}

int main(void)
{
    test_page_contains_required_snapshot_fields();
    test_clean_snapshot_reads_as_healthy();
    test_report_rate_scales_by_the_slot_clock();
    test_rasterizer_uses_glyph_bits_and_attribute_colors();
    printf("display_test: ok\n");
    return 0;
}
