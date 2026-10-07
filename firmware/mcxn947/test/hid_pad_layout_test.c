// Host test for src/hid_pad_layout.c -- which fields of a pad injection may set.
//
// Expected values are read off the fixture annotations in hid_fixtures.h:
// HID_FIXTURE_GAMEPAD_DS4_SHAPED is report ID 1, 64 bytes; byte 1..4 are X Y Z
// Rz (8-bit, 0..255), bits 40-43 the hat (0..7, Null), bits 44-57 buttons
// 1-14, bytes 8-9 Rx Ry (0..255).

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_fields.h"
#include "hid_fixtures.h"
#include "hid_mouse_layout.h"
#include "hid_pad_layout.h"

static const hid_layout_field_t *find(const hid_layout_t *l, uint8_t kind, size_t nth)
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

static void assert_channel(const hid_layout_t *l, uint8_t kind, uint16_t page, uint16_t usage,
                           uint16_t offset, uint8_t width, int32_t minimum, int32_t maximum)
{
    const hid_layout_field_t *f = find(l, kind, 0u);
    assert(f != NULL);
    assert(f->usage_page == page && f->usage == usage);
    assert(f->bit_offset == offset && f->bit_width == width);
    assert(f->logical_minimum == minimum && f->logical_maximum == maximum);
    assert((l->axes & HID_LAYOUT_AXIS_BIT(kind)) != 0u);
}

static void test_ds4_shaped_pad(void)
{
    hid_layout_t l;
    assert(hid_pad_compile(HID_FIXTURE_GAMEPAD_DS4_SHAPED, sizeof(HID_FIXTURE_GAMEPAD_DS4_SHAPED),
                           &l) == HID_PAD_OK);
    assert(l.device_class == HID_DEVICE_CLASS_PAD);
    assert(l.report_id == 1u && l.report_length == 64u);
    assert(l.min_report_length == 10u);  // Ry ends at byte 9
    assert(l.field_count == 8u);
    assert_channel(&l, HID_PAD_KIND_LX, 0x01u, 0x30u, 8u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_LY, 0x01u, 0x31u, 16u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_RX, 0x01u, 0x32u, 24u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_RY, 0x01u, 0x35u, 32u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_LT, 0x01u, 0x33u, 64u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_RT, 0x01u, 0x34u, 72u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_HAT, 0x01u, 0x39u, 40u, 4u, 0, 7);
    assert(find(&l, HID_PAD_KIND_HAT, 0u)->null_state == 1u);
    assert(find(&l, HID_PAD_KIND_LX, 0u)->null_state == 0u);

    const hid_layout_field_t *buttons = find(&l, HID_PAD_KIND_BUTTONS, 0u);
    assert(buttons != NULL);
    assert(buttons->usage_page == 0x09u && buttons->usage == 1u);
    assert(buttons->bit_offset == 44u && buttons->bit_width == 14u);
    assert(find(&l, HID_PAD_KIND_BUTTONS, 1u) == NULL);

    // Channel order is fixed -- the upload CRC depends on it.
    for (uint8_t k = HID_PAD_KIND_LX; k <= HID_PAD_KIND_HAT; ++k) {
        assert(l.fields[k].kind == k);
    }
    assert(l.fields[7].kind == HID_PAD_KIND_BUTTONS);
}

// The Input byte 0x42 on the fixture's hat is Data|Var|Abs plus the Null State
// bit this header now names.
static bool record_hat(void *context, const hid_field_t *field)
{
    if (field->usage_page == 0x01u && field->usage == 0x39u) {
        *(uint32_t *)context = field->flags;
    }
    return true;
}

static void test_null_state_flag_is_the_input_items_bit_6(void)
{
    static hid_fields_walker_t walker;
    uint32_t flags = 0u;
    assert(hid_fields_walk(&walker, HID_FIXTURE_GAMEPAD_DS4_SHAPED,
                           sizeof(HID_FIXTURE_GAMEPAD_DS4_SHAPED), record_hat, &flags) ==
           HID_FIELDS_OK);
    assert(HID_FIELD_NULL_STATE == 0x40u);
    assert((flags & HID_FIELD_NULL_STATE) != 0u);
    assert((flags & HID_FIELD_VARIABLE) != 0u);
}

// The protection this module shares with hid_mouse_layout.c, from the other
// side: a mouse is not a pad, and neither is a keyboard.
static void test_mouse_and_keyboard_are_not_pads(void)
{
    hid_layout_t l;
    memset(&l, 0xA5, sizeof(l));
    assert(hid_pad_compile(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE), &l) ==
           HID_PAD_NOT_PAD);
    assert(l.field_count == 0u && l.device_class == HID_DEVICE_CLASS_NONE);
    assert(hid_pad_compile(HID_FIXTURE_BOOT_KEYBOARD, sizeof(HID_FIXTURE_BOOT_KEYBOARD), &l) ==
           HID_PAD_NOT_PAD);
}

// A hat that is not 0..7 is skipped, not an error: the FPGA writes the held
// value unclamped, so 8 must mean "centred" in the device's own convention.
static void test_hat_outside_0_to_7_is_not_mapped(void)
{
    uint8_t d[sizeof(HID_FIXTURE_GAMEPAD_DS4_SHAPED)];
    memcpy(d, HID_FIXTURE_GAMEPAD_DS4_SHAPED, sizeof(d));
    // Hat: Usage 0x39 at 27..28, then 0x15 0x00 0x25 0x07 at 29..32 -> 1..8.
    assert(d[27] == 0x09u && d[28] == 0x39u && d[29] == 0x15u && d[31] == 0x25u);
    d[30] = 0x01u;
    d[32] = 0x08u;
    hid_layout_t l;
    assert(hid_pad_compile(d, sizeof(d), &l) == HID_PAD_OK);
    assert(find(&l, HID_PAD_KIND_HAT, 0u) == NULL);
    assert(l.field_count == 7u);
    assert((l.axes & HID_LAYOUT_AXIS_BIT(HID_PAD_KIND_HAT)) == 0u);
}

// Simulation-page Accelerator/Brake fill LT/RT when the pad has no Rx/Ry: a
// racing wheel's pedals are its triggers.
static void test_simulation_triggers_fill_lt_rt_when_rx_ry_absent(void)
{
    static const uint8_t wheel[] = {
        0x05, 0x01,        // Usage Page (Generic Desktop)
        0x09, 0x04,        // Usage (Joystick)
        0xA1, 0x01,        // Collection (Application)
        0x09, 0x30,        //   Usage (X)
        0x09, 0x31,        //   Usage (Y)
        0x15, 0x00,        //   Logical Minimum (0)
        0x26, 0xFF, 0x03,  //   Logical Maximum (1023)
        0x75, 0x10,        //   Report Size (16)
        0x95, 0x02,        //   Report Count (2)
        0x81, 0x02,        //   Input (Data, Var, Abs) -- bits 0-31
        0x05, 0x02,        //   Usage Page (Simulation Controls)
        0x09, 0xC4,        //   Usage (Accelerator)
        0x09, 0xC5,        //   Usage (Brake)
        0x26, 0xFF, 0x00,  //   Logical Maximum (255)
        0x75, 0x08,        //   Report Size (8)
        0x81, 0x02,        //   Input (Data, Var, Abs) -- bytes 4, 5
        0xC0,              // End Collection
    };
    hid_layout_t l;
    assert(hid_pad_compile(wheel, sizeof(wheel), &l) == HID_PAD_OK);
    assert(l.report_id == 0u && l.report_length == 6u);
    assert(l.field_count == 4u);
    assert_channel(&l, HID_PAD_KIND_LX, 0x01u, 0x30u, 0u, 16u, 0, 1023);
    assert_channel(&l, HID_PAD_KIND_LY, 0x01u, 0x31u, 16u, 16u, 0, 1023);
    assert_channel(&l, HID_PAD_KIND_LT, 0x02u, 0xC4u, 32u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_RT, 0x02u, 0xC5u, 40u, 8u, 0, 255);

    // With Rx/Ry present the pedals are NOT the triggers.
    static const uint8_t both[] = {
        0x05, 0x01, 0x09, 0x04, 0xA1, 0x01,
        0x09, 0x30, 0x09, 0x31, 0x09, 0x33, 0x09, 0x34,
        0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x04, 0x81, 0x02,  // bytes 0-3
        0x05, 0x02, 0x09, 0xC4, 0x09, 0xC5, 0x95, 0x02, 0x81, 0x02,        // bytes 4-5
        0xC0,
    };
    assert(hid_pad_compile(both, sizeof(both), &l) == HID_PAD_OK);
    assert_channel(&l, HID_PAD_KIND_LT, 0x01u, 0x33u, 16u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_RT, 0x01u, 0x34u, 24u, 8u, 0, 255);
    assert(l.field_count == 4u);
}

// A pad declaring too many fields to collect is UNSUPPORTED outright. Silent
// truncation would be a channel the MCU believes it holds and the FPGA never
// writes. 15 non-adjacent single buttons (odd numbers, so runs cannot fold)
// plus X and Y is 17 candidates against a 16-deep workspace.
static void test_too_many_fields_is_unsupported_not_truncated(void)
{
    uint8_t d[6 + 14 + 15 * 14 + 1];
    size_t n = 0u;
    const uint8_t head[] = {0x05, 0x01, 0x09, 0x05, 0xA1, 0x01};
    memcpy(&d[n], head, sizeof(head));
    n += sizeof(head);
    const uint8_t axes[] = {0x09, 0x30, 0x09, 0x31, 0x15, 0x00, 0x25, 0x7F,
                            0x75, 0x08, 0x95, 0x02, 0x81, 0x02};  // bytes 0, 1
    memcpy(&d[n], axes, sizeof(axes));
    n += sizeof(axes);
    for (uint8_t b = 0u; b < 15u; ++b) {
        const uint8_t one[] = {0x05, 0x09, 0x09, (uint8_t)(2u * b + 1u), 0x15, 0x00, 0x25,
                               0x01, 0x75, 0x01, 0x95, 0x01, 0x81, 0x02};  // one bit each
        memcpy(&d[n], one, sizeof(one));
        n += sizeof(one);
    }
    d[n++] = 0xC0;
    assert(n == sizeof(d));

    hid_layout_t l;
    memset(&l, 0xA5, sizeof(l));
    assert(hid_pad_compile(d, n, &l) == HID_PAD_UNSUPPORTED);
    assert(l.field_count == 0u);

    // One button fewer fits exactly, and every one of the 14 runs is mapped.
    const size_t fewer = n - 14u - 1u;
    d[fewer] = 0xC0;
    assert(hid_pad_compile(d, fewer + 1u, &l) == HID_PAD_OK);
    assert(l.field_count == 16u);
}

// The real DS4: report ID 1, 64 bytes, the same eight fields as the shaped
// fixture (the capture adds Physical Minimum/Maximum and Unit items around the
// hat, which the walker ignores). Armed by HID_FIXTURE_DS4_CAPTURED.
static void test_real_ds4_compiles_like_the_shaped_fixture(void)
{
    assert(sizeof(HID_FIXTURE_DS4) == 507u);
    bool any_nonzero = false;
    for (size_t i = 0u; i < sizeof(HID_FIXTURE_DS4); ++i) {
        if (HID_FIXTURE_DS4[i] != 0u) {
            any_nonzero = true;
            break;
        }
    }
    const bool captured = HID_FIXTURE_DS4_CAPTURED != 0;
    assert(captured == any_nonzero);
#if HID_FIXTURE_DS4_CAPTURED
    hid_layout_t l;
    assert(hid_pad_compile(HID_FIXTURE_DS4, sizeof(HID_FIXTURE_DS4), &l) == HID_PAD_OK);
    assert(l.report_id == 1u && l.report_length == 64u);
    assert(l.field_count == 8u);
    assert_channel(&l, HID_PAD_KIND_LX, 0x01u, 0x30u, 8u, 8u, 0, 255);
    assert_channel(&l, HID_PAD_KIND_HAT, 0x01u, 0x39u, 40u, 4u, 0, 7);
    assert_channel(&l, HID_PAD_KIND_LT, 0x01u, 0x33u, 64u, 8u, 0, 255);
    assert(find(&l, HID_PAD_KIND_BUTTONS, 0u)->bit_width == 14u);
#else
    printf("hid_pad_layout_test: HID_FIXTURE_DS4 is a placeholder (capture pending)\n");
#endif
}

int main(void)
{
    test_ds4_shaped_pad();
    test_null_state_flag_is_the_input_items_bit_6();
    test_mouse_and_keyboard_are_not_pads();
    test_hat_outside_0_to_7_is_not_mapped();
    test_simulation_triggers_fill_lt_rt_when_rx_ry_absent();
    test_too_many_fields_is_unsupported_not_truncated();
    test_real_ds4_compiles_like_the_shaped_fixture();
    printf("hid_pad_layout_test: ok\n");
    return 0;
}
