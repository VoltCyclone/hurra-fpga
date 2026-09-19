#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "display.h"
#include "glyphs.h"
#include "injection_wire.h"
#include "link_fault_classify.h"

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

    display_compose_page(&grid, &snapshot, 0x10203040u, 998u, true, NULL, NULL);

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

    display_compose_page(&grid, &snapshot, 0u, 0u, true, NULL, NULL);

    assert_text(&grid, 4u, DISPLAY_HEALTH_VALUE_COL, "NONE");
    assert(text_grid_cell_at(&grid, 4u, DISPLAY_HEALTH_VALUE_COL)->attr ==
           DISPLAY_ATTR_GOOD);
    assert(text_grid_cell_at(&grid, 3u, DISPLAY_HEALTH_VALUE_COL)->attr ==
           DISPLAY_ATTR_GOOD);

    // A stalled link is a fault even when nothing else is wrong.
    display_compose_page(&grid, &snapshot, 0u, 0u, false, NULL, NULL);
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

// With nothing captured the pane must SAY so. A blank bottom third is
// indistinguishable from a decoder that ran and produced nothing, which is the
// same argument display_flags makes about a dead panel versus an absent one.
static void test_descriptor_pane_reports_when_none_is_captured(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    const link_snapshot_t snapshot = {0};

    display_compose_page(&grid, &snapshot, 0u, 0u, true, NULL, NULL);
    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 1u, "HID DESCRIPTOR   (none captured)");

    // A published header with zero length is the same case: length is what
    // makes the bytes readable, so a stale pointer must not be decoded.
    const display_descriptor_t empty = {.bytes = (const uint8_t *)"", .length = 0u};
    display_compose_page(&grid, &snapshot, 0u, 0u, true, &empty, NULL);
    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 1u, "HID DESCRIPTOR   (none captured)");
}

// The fault pane takes the descriptor rows, and it is allowed to because a
// descriptor cannot have crossed a link that is faulted -- the two are
// mutually exclusive by construction, not by policy.
//
// The pin LOCATION is asserted beside the name because the name alone is not
// actionable: "CS" sends a person to the schematic, "J6-3" sends them to the
// connector.
static void test_fault_pane_names_the_suspect_pins(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    const link_snapshot_t snapshot = {0};

    const display_fault_t fault = {
        .sequence = 4u,
        .verdict = (uint8_t)LINK_FAULT_MIS_FRAMED,
        .suspect_count = 2u,
        .suspect = {(uint8_t)LINK_PIN_CS, (uint8_t)LINK_PIN_USB_SYNC},
    };

    display_compose_page(&grid, &snapshot, 0u, 0u, true, NULL, &fault);

    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 1u, "LINK FAULT  MIS-FRAMED");
    assert_text(&grid, DISPLAY_DESC_FIRST_ROW, 1u, "CS");
    assert_text(&grid, DISPLAY_DESC_FIRST_ROW, 12u, "J6-3 P3_23");
    assert_text(&grid, (uint8_t)(DISPLAY_DESC_FIRST_ROW + 1u), 1u, "usb_sync");
    assert_text(&grid, (uint8_t)(DISPLAY_DESC_FIRST_ROW + 1u), 12u, "J3-3 P1_22");
}

// sequence == 0 is "the classifier has not run yet". Rendering that as a clean
// link would be a lie on every board during the first 50 ms, and rendering it
// as a fault would be a lie on every healthy one.
static void test_unclassified_link_leaves_the_descriptor_pane(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    const link_snapshot_t snapshot = {0};
    const display_fault_t unclassified = {0};

    display_compose_page(&grid, &snapshot, 0u, 0u, true, NULL, &unclassified);

    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 1u, "HID DESCRIPTOR   (none captured)");
}

// A published verdict of OK is a working link, so the pane goes back to doing
// its day job.
static void test_healthy_verdict_leaves_the_descriptor_pane(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    const link_snapshot_t snapshot = {0};
    const display_fault_t healthy = {
        .sequence = 9u,
        .verdict = (uint8_t)LINK_FAULT_OK,
    };

    display_compose_page(&grid, &snapshot, 0u, 0u, true, NULL, &healthy);

    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 1u, "HID DESCRIPTOR   (none captured)");
}

// A real boot-mouse report descriptor, decoded into the bottom pane.
static void test_descriptor_pane_decodes_a_boot_mouse(void)
{
    static const uint8_t boot_mouse[] = {
        0x05, 0x01,  // Usage Page (Generic Desktop)
        0x09, 0x02,  // Usage (Mouse)
        0xA1, 0x01,  // Collection (Application)
        0x09, 0x01,  //   Usage (Pointer)
        0xA1, 0x00,  //   Collection (Physical)
        0x05, 0x09,  //     Usage Page (Button)
        0x19, 0x01,  //     Usage Minimum (1)
        0x29, 0x03,  //     Usage Maximum (3)
        0x15, 0x00,  //     Logical Minimum (0)
        0x25, 0x01,  //     Logical Maximum (1)
        0x95, 0x03,  //     Report Count (3)
        0x75, 0x01,  //     Report Size (1)
        0x81, 0x02,  //     Input (Data,Var,Abs)
        0xC0,        //   End Collection
        0xC0,        // End Collection
    };

    text_grid_t grid;
    text_grid_init(&grid, ' ', DISPLAY_ATTR_NORMAL);
    const link_snapshot_t snapshot = {0};
    const display_descriptor_t descriptor = {
        .bytes = boot_mouse,
        .length = (uint16_t)sizeof(boot_mouse),
        .generation = 12u,
        .interface_number = 1u,
    };

    display_compose_page(&grid, &snapshot, 0u, 0u, true, &descriptor, NULL);

    // The header carries the identity, so a pane of decoded text can always be
    // tied back to the enumeration it came from.
    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 1u, "HID DESCRIPTOR");
    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 17u, "if");
    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 22u, "gen");
    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 30u, "len");

    // Something decoded into the first line of the pane. The exact wording
    // belongs to hid_report_decoder_test; what this pins is that the pane is
    // populated at all and in the right cells.
    const text_grid_cell_t *const first =
        text_grid_cell_at(&grid, DISPLAY_DESC_FIRST_ROW, DISPLAY_DESC_COL_A);
    assert(first != NULL);
    assert(first->character != (uint8_t)' ');

    // This descriptor decodes to more lines than the pane holds, so the
    // truncation marker must be present -- silently showing the first ten items
    // of a descriptor looks exactly like a descriptor with ten items.
    assert_text(&grid, DISPLAY_DESC_HEADER_ROW, 49u, "more");
}

int main(void)
{
    test_page_contains_required_snapshot_fields();
    test_descriptor_pane_reports_when_none_is_captured();
    test_fault_pane_names_the_suspect_pins();
    test_unclassified_link_leaves_the_descriptor_pane();
    test_healthy_verdict_leaves_the_descriptor_pane();
    test_descriptor_pane_decodes_a_boot_mouse();
    test_clean_snapshot_reads_as_healthy();
    test_report_rate_scales_by_the_slot_clock();
    test_rasterizer_uses_glyph_bits_and_attribute_colors();
    printf("display_test: ok\n");
    return 0;
}
