// Host test for src/hid_mouse_layout.c -- which descriptor fields are injectable.
//
// Expected values are read off the fixture annotations in hid_fixtures.h.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_fixtures.h"
#include "hid_mouse_layout.h"
#include "kmcmd.h"

// kmcmd steps X/Y by up to KMCMD_STEP_MAX; a mapped X/Y field must hold at
// least HID_MOUSE_XY_MINIMUM_SPAN. Raising the step past the span would let one
// step overflow a field this module accepted.
_Static_assert(KMCMD_STEP_MAX <= HID_MOUSE_XY_MINIMUM_SPAN,
               "a kmcmd step must fit every X/Y field hid_mouse_layout accepts");

static const hid_mouse_field_t *find(const hid_mouse_layout_t *l, uint8_t kind, size_t nth)
{
    for (size_t i = 0u; i < l->field_count; ++i) {
        if (l->fields[i].kind == kind) {
            if (nth == 0u) {
                return &l->fields[i];
            }
            nth--;
        }
    }
    return NULL;
}

static void assert_axis(const hid_mouse_layout_t *l, uint8_t kind, uint16_t page, uint16_t usage,
                        uint16_t offset, uint8_t width, int32_t minimum, int32_t maximum)
{
    const hid_mouse_field_t *f = find(l, kind, 0u);
    assert(f != NULL);
    assert(f->usage_page == page && f->usage == usage);
    assert(f->bit_offset == offset && f->bit_width == width);
    assert(f->logical_minimum == minimum && f->logical_maximum == maximum);
}

static void assert_buttons(const hid_mouse_layout_t *l, size_t nth, uint16_t first,
                           uint16_t offset, uint8_t count)
{
    const hid_mouse_field_t *f = find(l, HID_MOUSE_BUTTONS, nth);
    assert(f != NULL);
    assert(f->usage_page == 0x09u && f->usage == first);
    assert(f->bit_offset == offset && f->bit_width == count);
}

static void test_boot_mouse(void)
{
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE), &l) ==
           HID_MOUSE_OK);
    assert(l.report_id == 0u && l.report_length == 3u && l.min_report_length == 3u);
    assert(l.field_count == 3u);
    assert(l.axes == (HID_MOUSE_AXIS_BIT(HID_MOUSE_X) | HID_MOUSE_AXIS_BIT(HID_MOUSE_Y)));
    assert_axis(&l, HID_MOUSE_X, 0x01u, 0x30u, 8u, 8u, -127, 127);
    assert_axis(&l, HID_MOUSE_Y, 0x01u, 0x31u, 16u, 8u, -127, 127);
    assert_buttons(&l, 0u, 1u, 0u, 3u);
    // Order is fixed -- the upload CRC depends on it.
    assert(l.fields[0].kind == HID_MOUSE_X && l.fields[1].kind == HID_MOUSE_Y &&
           l.fields[2].kind == HID_MOUSE_BUTTONS);
}

static void test_report_id_mouse_with_16bit_axes_wheel_and_pan(void)
{
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(HID_FIXTURE_ID_MOUSE_16BIT, sizeof(HID_FIXTURE_ID_MOUSE_16BIT),
                             &l) == HID_MOUSE_OK);
    assert(l.report_id == 2u && l.report_length == 8u && l.min_report_length == 8u);
    assert(l.field_count == 5u);
    assert_axis(&l, HID_MOUSE_X, 0x01u, 0x30u, 16u, 16u, -32767, 32767);
    assert_axis(&l, HID_MOUSE_Y, 0x01u, 0x31u, 32u, 16u, -32767, 32767);
    assert_axis(&l, HID_MOUSE_WHEEL, 0x01u, 0x38u, 48u, 8u, -127, 127);
    assert_axis(&l, HID_MOUSE_PAN, 0x0Cu, 0x0238u, 56u, 8u, -127, 127);
    assert_buttons(&l, 0u, 1u, 8u, 5u);
    assert(l.fields[2].kind == HID_MOUSE_WHEEL && l.fields[3].kind == HID_MOUSE_PAN);
}

static void test_12bit_axes_straddling_bytes(void)
{
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(HID_FIXTURE_MOUSE_12BIT, sizeof(HID_FIXTURE_MOUSE_12BIT), &l) ==
           HID_MOUSE_OK);
    assert(l.report_id == 0u && l.report_length == 4u);
    assert_axis(&l, HID_MOUSE_X, 0x01u, 0x30u, 8u, 12u, -2047, 2047);
    assert_axis(&l, HID_MOUSE_Y, 0x01u, 0x31u, 20u, 12u, -2047, 2047);
    assert_buttons(&l, 0u, 1u, 0u, 8u);
}

// The protection this module exists for: neither a keyboard nor a pad yields
// a layout, even though the pad has X and Y usages and buttons.
static void test_keyboard_and_gamepad_are_not_mice(void)
{
    hid_mouse_layout_t l;
    memset(&l, 0xA5, sizeof(l));
    assert(hid_mouse_compile(HID_FIXTURE_BOOT_KEYBOARD, sizeof(HID_FIXTURE_BOOT_KEYBOARD), &l) ==
           HID_MOUSE_NOT_MOUSE);
    assert(l.field_count == 0u);
    assert(hid_mouse_compile(HID_FIXTURE_GAMEPAD_DS4_SHAPED,
                             sizeof(HID_FIXTURE_GAMEPAD_DS4_SHAPED), &l) == HID_MOUSE_NOT_MOUSE);
}

// Injection is additive, so an absolute pointer (a tablet in mouse clothing)
// has nothing to add to.
static void test_absolute_axes_are_not_injectable(void)
{
    uint8_t d[sizeof(HID_FIXTURE_BOOT_MOUSE)];
    memcpy(d, HID_FIXTURE_BOOT_MOUSE, sizeof(d));
    assert(d[sizeof(d) - 4u] == 0x81u && d[sizeof(d) - 3u] == 0x06u);
    d[sizeof(d) - 3u] = 0x02u;  // X/Y Input: Rel -> Abs
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_NO_AXES);
}

// A keyboard application and a mouse application in ONE interface: only the
// mouse's report is mapped.
static void test_keyboard_beside_a_mouse_in_one_interface(void)
{
    const uint8_t keyboard[] = {
        0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01, 0x05, 0x07, 0x19, 0xE0,
        0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0xC0,
    };
    uint8_t d[sizeof(keyboard) + sizeof(HID_FIXTURE_ID_MOUSE_16BIT)];
    memcpy(d, keyboard, sizeof(keyboard));
    memcpy(&d[sizeof(keyboard)], HID_FIXTURE_ID_MOUSE_16BIT, sizeof(HID_FIXTURE_ID_MOUSE_16BIT));
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_OK);
    assert(l.report_id == 2u && l.report_length == 8u);
    assert_axis(&l, HID_MOUSE_X, 0x01u, 0x30u, 16u, 16u, -32767, 32767);
}

// Buttons listed one usage at a time merge into one run, which is the shape
// the FPGA's button entries take (usage U, width N covers U..U+N-1).
static void test_listed_buttons_merge_into_one_run(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
        0x05, 0x09, 0x09, 0x01, 0x09, 0x02, 0x09, 0x03,
        0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x03, 0x81, 0x02,
        0x95, 0x05, 0x81, 0x01,
        0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
        0x95, 0x02, 0x81, 0x06,
        0xC0,
    };
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_OK);
    assert_buttons(&l, 0u, 1u, 0u, 3u);
    assert(find(&l, HID_MOUSE_BUTTONS, 1u) == NULL);
}

// X/Y must hold a full kmcmd step (+/-64) and then some: an axis that cannot
// hold +/-127 is not injectable. Wheel and pan are stepped by 1, so +/-1 is
// enough for them.
static void test_axis_range_rules(void)
{
    const uint8_t narrow_xy[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
        0x09, 0x30, 0x09, 0x31, 0x15, 0xF8, 0x25, 0x07, 0x75, 0x04, 0x95, 0x02, 0x81, 0x06,
        0xC0,
    };
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(narrow_xy, sizeof(narrow_xy), &l) == HID_MOUSE_NO_AXES);

    const uint8_t unit_wheel[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
        0x09, 0x30, 0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x02, 0x81, 0x06,
        0x09, 0x38, 0x15, 0xFF, 0x25, 0x01, 0x95, 0x01, 0x81, 0x06,
        0xC0,
    };
    assert(hid_mouse_compile(unit_wheel, sizeof(unit_wheel), &l) == HID_MOUSE_OK);
    assert_axis(&l, HID_MOUSE_WHEEL, 0x01u, 0x38u, 16u, 8u, -1, 1);
}

// X and Y in different reports cannot share one layout, and a command cites
// exactly one report ID.
static void test_x_and_y_in_different_reports(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01,
        0x85, 0x01, 0x09, 0x30, 0x81, 0x06,
        0x85, 0x02, 0x09, 0x31, 0x81, 0x06,
        0xC0,
    };
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_NO_AXES);
}

// The FPGA's report buffer is 64 bytes; a longer mouse report cannot be mapped.
static void test_report_longer_than_the_link_carries(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
        0x09, 0x30, 0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x02, 0x81, 0x06,
        0x06, 0x00, 0xFF, 0x09, 0x01, 0x95, 0x3F, 0x81, 0x02,  // 63 vendor bytes -> 65 total
        0xC0,
    };
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_UNSUPPORTED);
}

static void test_malformed_descriptors(void)
{
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE) - 1u, &l) ==
           HID_MOUSE_MALFORMED);  // last End Collection missing
    const uint8_t truncated[] = {0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x26, 0xFF};
    assert(hid_mouse_compile(truncated, sizeof(truncated), &l) == HID_MOUSE_MALFORMED);
}

// The layout is the first report carrying BOTH axes, not merely the first X:
// a report with X alone ahead of the real one must not make the mouse NO_AXES.
static void test_first_report_with_both_axes_is_chosen(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
        0x85, 0x01, 0x09, 0x30, 0x95, 0x01, 0x81, 0x06,             // ID 1: X only
        0x85, 0x02, 0x09, 0x30, 0x09, 0x31, 0x95, 0x02, 0x81, 0x06, // ID 2: X and Y
        0xC0,
    };
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_OK);
    assert(l.report_id == 2u && l.report_length == 3u);
    assert_axis(&l, HID_MOUSE_X, 0x01u, 0x30u, 8u, 8u, -127, 127);
    assert_axis(&l, HID_MOUSE_Y, 0x01u, 0x31u, 16u, 8u, -127, 127);
}

// Sixteen buttons listed one usage at a time must not fill the candidate
// workspace and push the axes out: they are one run.
static void test_many_listed_buttons_do_not_crowd_out_the_axes(void)
{
    uint8_t d[6 + 2 + 16 * 2 + 10 + 16 + 1];
    size_t n = 0u;
    const uint8_t head[] = {0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x05, 0x09};
    memcpy(&d[n], head, sizeof(head));
    n += sizeof(head);
    for (uint8_t b = 1u; b <= 16u; ++b) {
        d[n++] = 0x09;
        d[n++] = b;
    }
    const uint8_t buttons[] = {0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x10, 0x81, 0x02};
    memcpy(&d[n], buttons, sizeof(buttons));
    n += sizeof(buttons);
    const uint8_t axes[] = {0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x81,
                            0x25, 0x7F, 0x75, 0x08, 0x95, 0x02, 0x81, 0x06};
    memcpy(&d[n], axes, sizeof(axes));
    n += sizeof(axes);
    d[n++] = 0xC0;
    assert(n == sizeof(d));

    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, n, &l) == HID_MOUSE_OK);
    assert_buttons(&l, 0u, 1u, 0u, 16u);
    assert(find(&l, HID_MOUSE_BUTTONS, 1u) == NULL);
    assert_axis(&l, HID_MOUSE_X, 0x01u, 0x30u, 16u, 8u, -127, 127);
}

// Two listed usages and a count of four: the two trailing bits "share the last
// usage" per the spec, but they are not a second copy of button 2 -- mapping
// them would make one injected button drive two bits.
static void test_button_list_tail_is_not_mapped(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
        0x05, 0x09, 0x09, 0x01, 0x09, 0x02,
        0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x04, 0x81, 0x02,
        0x95, 0x04, 0x81, 0x01,
        0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
        0x95, 0x02, 0x81, 0x06,
        0xC0,
    };
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_OK);
    assert_buttons(&l, 0u, 1u, 0u, 2u);
    assert(find(&l, HID_MOUSE_BUTTONS, 1u) == NULL);
}

// Buttons 1..3 declared over eight bits: the five bits past the range repeat
// button 3, so injecting "button 4" there would press button 3 on the PC.
static void test_button_range_tail_is_not_mapped(void)
{
    const uint8_t d[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
        0x05, 0x09, 0x19, 0x01, 0x29, 0x03,
        0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
        0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
        0x95, 0x02, 0x81, 0x06,
        0xC0,
    };
    hid_mouse_layout_t l;
    assert(hid_mouse_compile(d, sizeof(d), &l) == HID_MOUSE_OK);
    assert_buttons(&l, 0u, 1u, 0u, 3u);
    assert(find(&l, HID_MOUSE_BUTTONS, 1u) == NULL);
}

int main(void)
{
    test_boot_mouse();
    test_report_id_mouse_with_16bit_axes_wheel_and_pan();
    test_12bit_axes_straddling_bytes();
    test_keyboard_and_gamepad_are_not_mice();
    test_absolute_axes_are_not_injectable();
    test_keyboard_beside_a_mouse_in_one_interface();
    test_listed_buttons_merge_into_one_run();
    test_axis_range_rules();
    test_x_and_y_in_different_reports();
    test_report_longer_than_the_link_carries();
    test_malformed_descriptors();
    test_first_report_with_both_axes_is_chosen();
    test_many_listed_buttons_do_not_crowd_out_the_axes();
    test_button_list_tail_is_not_mapped();
    test_button_range_tail_is_not_mapped();
    printf("hid_mouse_layout_test: ok\n");
    return 0;
}
