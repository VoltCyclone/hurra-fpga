// Host test for src/hid_fields.c -- the report-descriptor field walker.
//
// Expected offsets are read off the fixture annotations in hid_fixtures.h, not
// derived from the walker.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_fields.h"
#include "hid_fixtures.h"

#define MAX_RECORDED 32u

typedef struct {
    hid_field_t fields[MAX_RECORDED];
    size_t count;
    size_t stop_after;  // 0 = never stop
} recorder_t;

static bool record(void *context, const hid_field_t *field)
{
    recorder_t *r = context;
    assert(r->count < MAX_RECORDED);
    r->fields[r->count++] = *field;
    return r->stop_after == 0u || r->count < r->stop_after;
}

static hid_fields_walker_t walker;  // static: the target keeps it off the stack too

static hid_fields_status_t walk(const uint8_t *d, size_t length, recorder_t *r)
{
    memset(r, 0, sizeof(*r));
    return hid_fields_walk(&walker, d, length, record, r);
}

static void test_boot_mouse_fields(void)
{
    recorder_t r;
    assert(walk(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE), &r) == HID_FIELDS_OK);
    // buttons run, padding, X, Y
    assert(r.count == 4u);

    const hid_field_t *buttons = &r.fields[0];
    assert(buttons->report_id == 0u && buttons->bit_offset == 0u);
    assert(buttons->bit_size == 1u && buttons->count == 3u);
    assert(buttons->usage_page == 0x09u && buttons->usage == 1u && buttons->usage_is_range);
    assert((buttons->flags & (HID_FIELD_CONSTANT | HID_FIELD_VARIABLE | HID_FIELD_RELATIVE)) ==
           HID_FIELD_VARIABLE);
    assert(buttons->app_usage_page == 0x01u && buttons->app_usage == 0x02u);

    const hid_field_t *pad = &r.fields[1];
    assert(pad->bit_offset == 3u && pad->bit_size == 5u && (pad->flags & HID_FIELD_CONSTANT));

    const hid_field_t *x = &r.fields[2];
    assert(x->bit_offset == 8u && x->bit_size == 8u && x->count == 1u);
    assert(x->usage_page == 0x01u && x->usage == 0x30u && !x->usage_is_range);
    assert(x->logical_minimum == -127 && x->logical_maximum == 127);
    assert((x->flags & (HID_FIELD_VARIABLE | HID_FIELD_RELATIVE)) ==
           (HID_FIELD_VARIABLE | HID_FIELD_RELATIVE));

    const hid_field_t *y = &r.fields[3];
    assert(y->bit_offset == 16u && y->usage == 0x31u && y->count == 1u);

    assert(hid_fields_input_bits(&walker, 0u) == 24u);
}

// The LED output report sits between the reserved byte and the key array. If
// Output items moved the input cursor the key array would land at bit 24.
static void test_keyboard_output_items_do_not_move_the_input_cursor(void)
{
    recorder_t r;
    assert(walk(HID_FIXTURE_BOOT_KEYBOARD, sizeof(HID_FIXTURE_BOOT_KEYBOARD), &r) ==
           HID_FIELDS_OK);
    assert(r.count == 3u);  // modifiers, reserved, key array -- no LED fields
    assert(r.fields[0].bit_offset == 0u && r.fields[0].usage_page == 0x07u);
    assert(r.fields[1].bit_offset == 8u && (r.fields[1].flags & HID_FIELD_CONSTANT));
    const hid_field_t *keys = &r.fields[2];
    assert(keys->bit_offset == 16u && keys->bit_size == 8u && keys->count == 6u);
    assert((keys->flags & HID_FIELD_VARIABLE) == 0u);  // an array
    assert(keys->app_usage_page == 0x01u && keys->app_usage == 0x06u);
    assert(hid_fields_input_bits(&walker, 0u) == 64u);
}

// Report ID 2: the ID owns byte 0, so the first field starts at bit 8. A 2-byte
// Usage under the Consumer page names AC Pan; a nonnegative-minimum field keeps
// an unsigned maximum while a negative minimum makes it signed.
static void test_report_id_16bit_axes_and_consumer_pan(void)
{
    recorder_t r;
    assert(walk(HID_FIXTURE_ID_MOUSE_16BIT, sizeof(HID_FIXTURE_ID_MOUSE_16BIT), &r) ==
           HID_FIELDS_OK);
    // buttons, padding, X, Y, wheel, pan
    assert(r.count == 6u);
    for (size_t i = 0u; i < r.count; ++i) {
        assert(r.fields[i].report_id == 2u);
    }
    assert(r.fields[0].bit_offset == 8u && r.fields[0].count == 5u);
    assert(r.fields[1].bit_offset == 13u);
    assert(r.fields[2].bit_offset == 16u && r.fields[2].bit_size == 16u && r.fields[2].usage == 0x30u);
    assert(r.fields[2].logical_minimum == -32767 && r.fields[2].logical_maximum == 32767);
    assert(r.fields[3].bit_offset == 32u && r.fields[3].usage == 0x31u);
    assert(r.fields[4].bit_offset == 48u && r.fields[4].bit_size == 8u && r.fields[4].usage == 0x38u);
    assert(r.fields[5].bit_offset == 56u && r.fields[5].usage_page == 0x0Cu &&
           r.fields[5].usage == 0x0238u);
    assert(hid_fields_input_bits(&walker, 2u) == 64u);
    assert(hid_fields_input_bits(&walker, 0u) == 0u);
}

// 0x26 0xFF 0x00 with a minimum of 0 is 255, and 0x25 0xFF with a minimum of
// 0 is ALSO 255 -- one byte 0xFF is only -1 when the minimum is negative.
static void test_logical_maximum_signedness_follows_the_minimum(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x30,
        0x15, 0x00, 0x25, 0xFF,  // min 0, max 0xFF -> 255
        0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
        0x09, 0x31,
        0x15, 0x80, 0x25, 0xFF,  // min -128, max 0xFF -> -1
        0x81, 0x02,
    };
    recorder_t r;
    assert(walk(d, sizeof(d), &r) == HID_FIELDS_OK);
    assert(r.count == 2u);
    assert(r.fields[0].logical_minimum == 0 && r.fields[0].logical_maximum == 255);
    assert(r.fields[1].logical_minimum == -128 && r.fields[1].logical_maximum == -1);
    // Outside any collection: no application.
    assert(r.fields[0].app_usage_page == 0u && r.fields[0].app_usage == 0u);
}

// Two usages and a count of 4: elements 0 and 1 take the listed usages, the
// remaining two share the last one (HID 1.11 6.2.2.8).
static void test_explicit_usage_list_shorter_than_count(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
        0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
    };
    recorder_t r;
    assert(walk(d, sizeof(d), &r) == HID_FIELDS_OK);
    assert(r.count == 3u);
    assert(r.fields[0].bit_offset == 0u && r.fields[0].count == 1u && r.fields[0].usage == 0x30u);
    assert(r.fields[1].bit_offset == 8u && r.fields[1].count == 1u && r.fields[1].usage == 0x31u);
    assert(r.fields[2].bit_offset == 16u && r.fields[2].count == 2u && r.fields[2].usage == 0x31u);
    assert(!r.fields[2].usage_is_range);
}

// A Usage Minimum/Maximum range names as many elements as it spans; elements
// past it share its maximum (HID 1.11 6.2.2.8), as with a short usage list.
// Buttons 1..3 over eight bits are buttons 1, 2, 3 and five more button 3s.
static void test_usage_range_shorter_than_count(void)
{
    const uint8_t d[] = {
        0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01,
        0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    };
    recorder_t r;
    assert(walk(d, sizeof(d), &r) == HID_FIELDS_OK);
    assert(r.count == 2u);
    assert(r.fields[0].bit_offset == 0u && r.fields[0].count == 3u);
    assert(r.fields[0].usage_page == 0x09u && r.fields[0].usage == 1u);
    assert(r.fields[0].usage_is_range);
    assert(r.fields[1].bit_offset == 3u && r.fields[1].count == 5u);
    assert(r.fields[1].usage_page == 0x09u && r.fields[1].usage == 3u);
    assert(!r.fields[1].usage_is_range);
    assert(hid_fields_input_bits(&walker, 0u) == 8u);
}

// A range wider than the count names only the elements there are; one whose
// maximum is below its minimum names none of them.
static void test_usage_range_bounds(void)
{
    const uint8_t wide[] = {
        0x05, 0x09, 0x19, 0x01, 0x29, 0x10, 0x75, 0x01, 0x95, 0x05, 0x81, 0x02,
    };
    recorder_t r;
    assert(walk(wide, sizeof(wide), &r) == HID_FIELDS_OK);
    assert(r.count == 1u);
    assert(r.fields[0].count == 5u && r.fields[0].usage == 1u && r.fields[0].usage_is_range);

    const uint8_t inverted[] = {
        0x05, 0x09, 0x19, 0x05, 0x29, 0x02, 0x75, 0x01, 0x95, 0x04, 0x81, 0x02,
    };
    assert(walk(inverted, sizeof(inverted), &r) == HID_FIELDS_OK);
    assert(r.count == 1u);
    assert(r.fields[0].count == 4u && r.fields[0].usage_page == 0u && r.fields[0].usage == 0u);
}

// Locals clear after every main item: a Usage before one Input must not name
// the next Input's elements.
static void test_locals_clear_after_each_main_item(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x30, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
        0x81, 0x06,
    };
    recorder_t r;
    assert(walk(d, sizeof(d), &r) == HID_FIELDS_OK);
    assert(r.count == 2u);
    assert(r.fields[0].usage == 0x30u);
    assert(r.fields[1].usage == 0u && r.fields[1].bit_offset == 8u);
}

// Push saves the globals, Pop restores them: the report size set between them
// must not leak into the next field.
static void test_push_and_pop_restore_globals(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x75, 0x08, 0x95, 0x01,
        0xA4,                    // Push
        0x75, 0x10, 0x09, 0x30, 0x81, 0x06,
        0xB4,                    // Pop
        0x09, 0x31, 0x81, 0x06,
    };
    recorder_t r;
    assert(walk(d, sizeof(d), &r) == HID_FIELDS_OK);
    assert(r.count == 2u);
    assert(r.fields[0].bit_size == 16u);
    assert(r.fields[1].bit_size == 8u && r.fields[1].bit_offset == 16u);
}

// Each top-level Application names its own fields, and interleaved report IDs
// keep separate cursors.
static void test_two_applications_with_interleaved_report_ids(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,  // Keyboard application
        0x85, 0x01, 0x05, 0x07, 0x75, 0x08, 0x95, 0x02, 0x81, 0x00,
        0xC0,
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,  // Mouse application
        0x85, 0x02, 0x09, 0x30, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
        0x85, 0x01, 0x09, 0x31, 0x81, 0x06,  // back to ID 1, inside the mouse app
        0xC0,
    };
    recorder_t r;
    assert(walk(d, sizeof(d), &r) == HID_FIELDS_OK);
    assert(r.count == 3u);
    assert(r.fields[0].report_id == 1u && r.fields[0].bit_offset == 8u);
    assert(r.fields[0].app_usage == 0x06u);
    assert(r.fields[1].report_id == 2u && r.fields[1].bit_offset == 8u);
    assert(r.fields[1].app_usage == 0x02u);
    assert(r.fields[2].report_id == 1u && r.fields[2].bit_offset == 24u);
    assert(r.fields[2].app_usage == 0x02u);
    assert(hid_fields_input_bits(&walker, 1u) == 32u);
    assert(hid_fields_input_bits(&walker, 2u) == 16u);
}

static void test_ds4_shaped_report_is_64_bytes(void)
{
    recorder_t r;
    assert(walk(HID_FIXTURE_GAMEPAD_DS4_SHAPED, sizeof(HID_FIXTURE_GAMEPAD_DS4_SHAPED), &r) ==
           HID_FIELDS_OK);
    assert(hid_fields_input_bits(&walker, 1u) == 512u);
    assert(hid_fields_input_bits(&walker, 5u) == 0u);  // output only
    assert(r.fields[0].app_usage_page == 0x01u && r.fields[0].app_usage == 0x05u);
}

static void test_errors(void)
{
    recorder_t r;

    const uint8_t stray_end[] = {0x09, 0x30, 0xC0};
    assert(walk(stray_end, sizeof(stray_end), &r) == HID_FIELDS_UNBALANCED);

    const uint8_t unclosed[] = {0x05, 0x01, 0x09, 0x02, 0xA1, 0x01};
    assert(walk(unclosed, sizeof(unclosed), &r) == HID_FIELDS_UNBALANCED);

    const uint8_t id_zero[] = {0x85, 0x00};
    assert(walk(id_zero, sizeof(id_zero), &r) == HID_FIELDS_BAD_ID);

    const uint8_t truncated[] = {0x05, 0x01, 0x26, 0xFF};
    assert(walk(truncated, sizeof(truncated), &r) == HID_FIELDS_TRUNCATED);

    const uint8_t pop_empty[] = {0xB4};
    assert(walk(pop_empty, sizeof(pop_empty), &r) == HID_FIELDS_LIMIT);

    uint8_t deep[2u * (HID_FIELDS_MAX_COLLECTION_DEPTH + 1u)];
    for (size_t i = 0u; i < sizeof(deep); i += 2u) {
        deep[i] = 0xA1;
        deep[i + 1u] = 0x00;
    }
    assert(walk(deep, sizeof(deep), &r) == HID_FIELDS_LIMIT);

    memset(&r, 0, sizeof(r));
    r.stop_after = 2u;
    assert(hid_fields_walk(&walker, HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE),
                           record, &r) == HID_FIELDS_STOPPED);
    assert(r.count == 2u);
}

// Every prefix of every fixture must terminate without reading past its end.
static void test_every_prefix_terminates(void)
{
    const struct {
        const uint8_t *bytes;
        size_t length;
    } fixtures[] = {
        {HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE)},
        {HID_FIXTURE_BOOT_KEYBOARD, sizeof(HID_FIXTURE_BOOT_KEYBOARD)},
        {HID_FIXTURE_ID_MOUSE_16BIT, sizeof(HID_FIXTURE_ID_MOUSE_16BIT)},
        {HID_FIXTURE_MOUSE_12BIT, sizeof(HID_FIXTURE_MOUSE_12BIT)},
        {HID_FIXTURE_GAMEPAD_DS4_SHAPED, sizeof(HID_FIXTURE_GAMEPAD_DS4_SHAPED)},
    };
    for (size_t f = 0u; f < sizeof(fixtures) / sizeof(fixtures[0]); ++f) {
        for (size_t cut = 0u; cut <= fixtures[f].length; ++cut) {
            (void)hid_fields_walk(&walker, fixtures[f].bytes, cut, NULL, NULL);
        }
    }
}

// A 4-byte usage carries its own page, including vendor pages at 0xFF00 and
// up. It must not be mistaken for a short usage resolved against the current
// Usage Page -- that would turn vendor usage 0xFF00:0030 into Generic Desktop X.
static void test_extended_vendor_usage_keeps_its_page(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x0B, 0x30, 0x00, 0x00, 0xFF,  // Usage (0xFF00:0x0030)
        0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
        0x0B, 0x31, 0x00, 0x01, 0x00,              // Usage (0x0001:0x0031) -- Y
        0x81, 0x06,
    };
    recorder_t r;
    assert(walk(d, sizeof(d), &r) == HID_FIELDS_OK);
    assert(r.count == 2u);
    assert(r.fields[0].usage_page == 0xFF00u && r.fields[0].usage == 0x30u);
    assert(r.fields[1].usage_page == 0x01u && r.fields[1].usage == 0x31u);
}

int main(void)
{
    test_boot_mouse_fields();
    test_keyboard_output_items_do_not_move_the_input_cursor();
    test_report_id_16bit_axes_and_consumer_pan();
    test_logical_maximum_signedness_follows_the_minimum();
    test_explicit_usage_list_shorter_than_count();
    test_usage_range_shorter_than_count();
    test_usage_range_bounds();
    test_locals_clear_after_each_main_item();
    test_push_and_pop_restore_globals();
    test_two_applications_with_interleaved_report_ids();
    test_ds4_shaped_report_is_64_bytes();
    test_errors();
    test_every_prefix_terminates();
    test_extended_vendor_usage_keeps_its_page();
    printf("hid_fields_test: ok\n");
    return 0;
}
