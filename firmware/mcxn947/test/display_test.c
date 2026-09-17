#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "display.h"
#include "glyphs.h"

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
        .link_flags = 0x0008u,
        .fault_flags = 0x55AAu,
        .last_rx_sequence = 0x5Au,
    };

    display_compose_page(&grid, &snapshot, 0x10203040u);

    assert_text(&grid, DISPLAY_SLOT_VALUE_ROW, 2u, "0x1234ABCD");
    assert_text(&grid, 5u, 16u, "YES");
    assert_text(&grid, 6u, 16u, "0x0008");
    assert_text(&grid, 7u, 16u, "0x55AA");
    assert_text(&grid, 9u, 16u, "0x5A");
    assert_text(&grid, 11u, 16u, "0x10203040");
    assert_text(&grid, 12u, 16u, "0x2468ACE0");
    assert(text_grid_cell_at(&grid, DISPLAY_SLOT_VALUE_ROW, 2u)->attr ==
           DISPLAY_ATTR_SLOT);
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
    test_rasterizer_uses_glyph_bits_and_attribute_colors();
    printf("display_test: ok\n");
    return 0;
}
