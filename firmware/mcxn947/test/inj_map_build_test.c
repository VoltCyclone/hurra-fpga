// Host test for src/inj_map_build.c -- mouse layout to MAP_ENTRY payloads.
//
// The golden CRCs were computed independently with Python's zlib.crc32 over
// struct.pack('<HHBBBBHHHBBiiBB', ...) of the expected entries, not by the
// code under test.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_fixtures.h"
#include "hid_mouse_layout.h"
#include "inj_map_build.h"

static void assert_entry(const inj_map_entry_payload_t *e, uint8_t index, uint16_t page,
                         uint16_t usage, uint16_t offset, uint8_t width, uint8_t flags,
                         int32_t minimum, int32_t maximum)
{
    assert(e->entry_index == index);
    assert(e->usage_page == page && e->usage == usage);
    assert(e->bit_offset == offset && e->bit_width == width);
    assert(e->flags == flags);
    assert(e->logical_minimum == minimum && e->logical_maximum == maximum);
    assert(e->channel == 0u);
}

static void test_boot_mouse_entries(void)
{
    hid_mouse_layout_t layout;
    assert(hid_mouse_compile(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE), &layout) ==
           HID_MOUSE_OK);
    const inj_map_target_t target = {
        .descriptor_generation = 3u,
        .map_generation = 1u,
        .interface_number = 0u,
        .endpoint_number = 1u,
        .report_length = 3u,
    };
    inj_map_entry_payload_t entries[HID_MOUSE_MAX_FIELDS];
    const uint8_t count = inj_map_build_entries(&layout, &target, entries);
    assert(count == 3u);
    for (uint8_t i = 0u; i < count; ++i) {
        assert(entries[i].descriptor_generation == 3u && entries[i].map_generation == 1u);
        assert(entries[i].interface_number == 0u && entries[i].endpoint_number == 1u);
        assert(entries[i].report_id == 0u && entries[i].report_length == 3u);
    }
    // SIGNED|RELATIVE|X = 1|2|8, SIGNED|RELATIVE|Y = 1|2|16, BUTTON = 4.
    assert_entry(&entries[0], 0u, 0x01u, 0x30u, 8u, 8u, 11u, -127, 127);
    assert_entry(&entries[1], 1u, 0x01u, 0x31u, 16u, 8u, 19u, -127, 127);
    assert_entry(&entries[2], 2u, 0x09u, 1u, 0u, 3u, 4u, 0, 1);
    assert(inj_map_entries_crc32(entries, count) == 0xDD9D11D2u);
}

static void test_report_id_mouse_entries(void)
{
    hid_mouse_layout_t layout;
    assert(hid_mouse_compile(HID_FIXTURE_ID_MOUSE_16BIT, sizeof(HID_FIXTURE_ID_MOUSE_16BIT),
                             &layout) == HID_MOUSE_OK);
    const inj_map_target_t target = {
        .descriptor_generation = 7u,
        .map_generation = 4u,
        .interface_number = 1u,
        .endpoint_number = 2u,
        .report_length = 8u,
    };
    inj_map_entry_payload_t entries[HID_MOUSE_MAX_FIELDS];
    const uint8_t count = inj_map_build_entries(&layout, &target, entries);
    assert(count == 5u);
    for (uint8_t i = 0u; i < count; ++i) {
        // A nonzero ID must keep every field clear of byte 0, or the FPGA
        // rejects the map with REPORT_ID_PREFIX.
        assert(entries[i].report_id == 2u && entries[i].bit_offset >= 8u);
    }
    assert_entry(&entries[0], 0u, 0x01u, 0x30u, 16u, 16u, 11u, -32767, 32767);
    assert_entry(&entries[1], 1u, 0x01u, 0x31u, 32u, 16u, 19u, -32767, 32767);
    assert_entry(&entries[2], 2u, 0x01u, 0x38u, 48u, 8u, 1u | 2u | 32u, -127, 127);
    assert_entry(&entries[3], 3u, 0x0Cu, 0x0238u, 56u, 8u, 1u | 2u | 64u, -127, 127);
    assert_entry(&entries[4], 4u, 0x09u, 1u, 8u, 5u, 4u, 0, 1);
    assert(inj_map_entries_crc32(entries, count) == 0x01C994FDu);
}

// An unsigned relative axis (minimum >= 0) carries no SIGNED flag.
static void test_unsigned_axis_has_no_signed_flag(void)
{
    hid_mouse_layout_t layout;
    memset(&layout, 0, sizeof(layout));
    layout.field_count = 1u;
    layout.fields[0].kind = HID_MOUSE_WHEEL;
    layout.fields[0].usage_page = 0x01u;
    layout.fields[0].usage = 0x38u;
    layout.fields[0].bit_offset = 24u;
    layout.fields[0].bit_width = 8u;
    layout.fields[0].logical_minimum = 0;
    layout.fields[0].logical_maximum = 255;
    const inj_map_target_t target = {.endpoint_number = 1u, .report_length = 4u};
    inj_map_entry_payload_t entries[HID_MOUSE_MAX_FIELDS];
    assert(inj_map_build_entries(&layout, &target, entries) == 1u);
    assert(entries[0].flags == (2u | 32u));
}

// The target's report length wins over the descriptor's: it is what the device
// was seen sending, and the FPGA only binds on an exact match.
static void test_target_report_length_is_stamped(void)
{
    hid_mouse_layout_t layout;
    assert(hid_mouse_compile(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE), &layout) ==
           HID_MOUSE_OK);
    const inj_map_target_t target = {.endpoint_number = 1u, .report_length = 4u};
    inj_map_entry_payload_t entries[HID_MOUSE_MAX_FIELDS];
    const uint8_t count = inj_map_build_entries(&layout, &target, entries);
    for (uint8_t i = 0u; i < count; ++i) {
        assert(entries[i].report_length == 4u);
    }
}

int main(void)
{
    test_boot_mouse_entries();
    test_report_id_mouse_entries();
    test_unsigned_axis_has_no_signed_flag();
    test_target_report_length_is_stamped();
    printf("inj_map_build_test: ok\n");
    return 0;
}
