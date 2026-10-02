// Report descriptors shared by the descriptor-stack tests. Each is annotated
// with the report it describes, so an expected offset can be checked by eye
// against the bytes rather than against the code under test.

#ifndef HURRA_MCXN947_TEST_HID_FIXTURES_H
#define HURRA_MCXN947_TEST_HID_FIXTURES_H

#include <stdint.h>

// HID 1.11 Appendix B.2, the boot mouse. Report (3 bytes, no ID):
//   bits 0-2 buttons 1-3, bits 3-7 padding, byte 1 X, byte 2 Y (rel, -127..127)
static const uint8_t HID_FIXTURE_BOOT_MOUSE[] = {
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
    0x81, 0x02,  //     Input (Data, Var, Abs)
    0x95, 0x01,  //     Report Count (1)
    0x75, 0x05,  //     Report Size (5)
    0x81, 0x01,  //     Input (Const)
    0x05, 0x01,  //     Usage Page (Generic Desktop)
    0x09, 0x30,  //     Usage (X)
    0x09, 0x31,  //     Usage (Y)
    0x15, 0x81,  //     Logical Minimum (-127)
    0x25, 0x7F,  //     Logical Maximum (127)
    0x75, 0x08,  //     Report Size (8)
    0x95, 0x02,  //     Report Count (2)
    0x81, 0x06,  //     Input (Data, Var, Rel)
    0xC0,        //   End Collection
    0xC0,        // End Collection
};

// HID 1.11 Appendix B.1, the boot keyboard. Input report (8 bytes, no ID):
//   byte 0 modifiers, byte 1 reserved, bytes 2-7 key array.
// Output report (1 byte): bits 0-4 LEDs, bits 5-7 padding -- which must NOT
// move the input bit cursor.
static const uint8_t HID_FIXTURE_BOOT_KEYBOARD[] = {
    0x05, 0x01,  // Usage Page (Generic Desktop)
    0x09, 0x06,  // Usage (Keyboard)
    0xA1, 0x01,  // Collection (Application)
    0x05, 0x07,  //   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,  //   Usage Minimum (224)
    0x29, 0xE7,  //   Usage Maximum (231)
    0x15, 0x00,  //   Logical Minimum (0)
    0x25, 0x01,  //   Logical Maximum (1)
    0x75, 0x01,  //   Report Size (1)
    0x95, 0x08,  //   Report Count (8)
    0x81, 0x02,  //   Input (Data, Var, Abs) -- modifiers
    0x95, 0x01,  //   Report Count (1)
    0x75, 0x08,  //   Report Size (8)
    0x81, 0x01,  //   Input (Const) -- reserved
    0x95, 0x05,  //   Report Count (5)
    0x75, 0x01,  //   Report Size (1)
    0x05, 0x08,  //   Usage Page (LEDs)
    0x19, 0x01,  //   Usage Minimum (1)
    0x29, 0x05,  //   Usage Maximum (5)
    0x91, 0x02,  //   Output (Data, Var, Abs) -- LEDs
    0x95, 0x01,  //   Report Count (1)
    0x75, 0x03,  //   Report Size (3)
    0x91, 0x01,  //   Output (Const) -- LED padding
    0x95, 0x06,  //   Report Count (6)
    0x75, 0x08,  //   Report Size (8)
    0x15, 0x00,  //   Logical Minimum (0)
    0x25, 0x65,  //   Logical Maximum (101)
    0x05, 0x07,  //   Usage Page (Keyboard/Keypad)
    0x19, 0x00,  //   Usage Minimum (0)
    0x29, 0x65,  //   Usage Maximum (101)
    0x81, 0x00,  //   Input (Data, Array, Abs) -- key array
    0xC0,        // End Collection
};

// A gaming-mouse shape: Report ID 2, 16-bit axes, wheel and AC Pan.
// Report (8 bytes): byte 0 ID, bits 8-12 buttons 1-5, bits 13-15 padding,
// bits 16-31 X, bits 32-47 Y, bits 48-55 wheel, bits 56-63 AC Pan.
static const uint8_t HID_FIXTURE_ID_MOUSE_16BIT[] = {
    0x05, 0x01,              // Usage Page (Generic Desktop)
    0x09, 0x02,              // Usage (Mouse)
    0xA1, 0x01,              // Collection (Application)
    0x85, 0x02,              //   Report ID (2)
    0x09, 0x01,              //   Usage (Pointer)
    0xA1, 0x00,              //   Collection (Physical)
    0x05, 0x09,              //     Usage Page (Button)
    0x19, 0x01,              //     Usage Minimum (1)
    0x29, 0x05,              //     Usage Maximum (5)
    0x15, 0x00,              //     Logical Minimum (0)
    0x25, 0x01,              //     Logical Maximum (1)
    0x95, 0x05,              //     Report Count (5)
    0x75, 0x01,              //     Report Size (1)
    0x81, 0x02,              //     Input (Data, Var, Abs)
    0x95, 0x01,              //     Report Count (1)
    0x75, 0x03,              //     Report Size (3)
    0x81, 0x01,              //     Input (Const)
    0x05, 0x01,              //     Usage Page (Generic Desktop)
    0x09, 0x30,              //     Usage (X)
    0x09, 0x31,              //     Usage (Y)
    0x16, 0x01, 0x80,        //     Logical Minimum (-32767)
    0x26, 0xFF, 0x7F,        //     Logical Maximum (32767)
    0x75, 0x10,              //     Report Size (16)
    0x95, 0x02,              //     Report Count (2)
    0x81, 0x06,              //     Input (Data, Var, Rel)
    0x09, 0x38,              //     Usage (Wheel)
    0x15, 0x81,              //     Logical Minimum (-127)
    0x25, 0x7F,              //     Logical Maximum (127)
    0x75, 0x08,              //     Report Size (8)
    0x95, 0x01,              //     Report Count (1)
    0x81, 0x06,              //     Input (Data, Var, Rel)
    0x05, 0x0C,              //     Usage Page (Consumer)
    0x0A, 0x38, 0x02,        //     Usage (AC Pan, 0x0238)
    0x95, 0x01,              //     Report Count (1)
    0x81, 0x06,              //     Input (Data, Var, Rel)
    0xC0,                    //   End Collection
    0xC0,                    // End Collection
};

// No report ID, 8 buttons, 12-bit axes straddling byte boundaries.
// Report (4 bytes): bits 0-7 buttons 1-8, bits 8-19 X, bits 20-31 Y.
static const uint8_t HID_FIXTURE_MOUSE_12BIT[] = {
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x02,        // Usage (Mouse)
    0xA1, 0x01,        // Collection (Application)
    0x09, 0x01,        //   Usage (Pointer)
    0xA1, 0x00,        //   Collection (Physical)
    0x05, 0x09,        //     Usage Page (Button)
    0x19, 0x01,        //     Usage Minimum (1)
    0x29, 0x08,        //     Usage Maximum (8)
    0x15, 0x00,        //     Logical Minimum (0)
    0x25, 0x01,        //     Logical Maximum (1)
    0x75, 0x01,        //     Report Size (1)
    0x95, 0x08,        //     Report Count (8)
    0x81, 0x02,        //     Input (Data, Var, Abs)
    0x05, 0x01,        //     Usage Page (Generic Desktop)
    0x09, 0x30,        //     Usage (X)
    0x09, 0x31,        //     Usage (Y)
    0x16, 0x01, 0xF8,  //     Logical Minimum (-2047)
    0x26, 0xFF, 0x07,  //     Logical Maximum (2047)
    0x75, 0x0C,        //     Report Size (12)
    0x95, 0x02,        //     Report Count (2)
    0x81, 0x06,        //     Input (Data, Var, Rel)
    0xC0,              //   End Collection
    0xC0,              // End Collection
};

// DS4-SHAPED, not a byte-exact DS4 capture: a Game Pad application with Report
// ID 1 and a 64-byte input report whose byte 1..4 are ABSOLUTE sticks, plus an
// output report 5. Input report (64 bytes): byte 0 ID, bytes 1-4 sticks,
// bits 40-43 hat, bits 44-57 buttons 1-14, bits 58-63 vendor counter,
// bytes 8-9 triggers, bytes 10-63 vendor. Replace with a real capture from the
// bench pad before relying on it for anything DS4-specific.
static const uint8_t HID_FIXTURE_GAMEPAD_DS4_SHAPED[] = {
    0x05, 0x01,              // Usage Page (Generic Desktop)
    0x09, 0x05,              // Usage (Game Pad)
    0xA1, 0x01,              // Collection (Application)
    0x85, 0x01,              //   Report ID (1)
    0x09, 0x30,              //   Usage (X)
    0x09, 0x31,              //   Usage (Y)
    0x09, 0x32,              //   Usage (Z)
    0x09, 0x35,              //   Usage (Rz)
    0x15, 0x00,              //   Logical Minimum (0)
    0x26, 0xFF, 0x00,        //   Logical Maximum (255)
    0x75, 0x08,              //   Report Size (8)
    0x95, 0x04,              //   Report Count (4)
    0x81, 0x02,              //   Input (Data, Var, Abs) -- sticks
    0x09, 0x39,              //   Usage (Hat switch)
    0x15, 0x00,              //   Logical Minimum (0)
    0x25, 0x07,              //   Logical Maximum (7)
    0x75, 0x04,              //   Report Size (4)
    0x95, 0x01,              //   Report Count (1)
    0x81, 0x42,              //   Input (Data, Var, Abs, Null)
    0x05, 0x09,              //   Usage Page (Button)
    0x19, 0x01,              //   Usage Minimum (1)
    0x29, 0x0E,              //   Usage Maximum (14)
    0x15, 0x00,              //   Logical Minimum (0)
    0x25, 0x01,              //   Logical Maximum (1)
    0x75, 0x01,              //   Report Size (1)
    0x95, 0x0E,              //   Report Count (14)
    0x81, 0x02,              //   Input (Data, Var, Abs)
    0x06, 0x00, 0xFF,        //   Usage Page (Vendor 0xFF00)
    0x09, 0x20,              //   Usage (0x20)
    0x75, 0x06,              //   Report Size (6)
    0x95, 0x01,              //   Report Count (1)
    0x15, 0x00,              //   Logical Minimum (0)
    0x25, 0x7F,              //   Logical Maximum (127)
    0x81, 0x02,              //   Input (Data, Var, Abs) -- counter
    0x05, 0x01,              //   Usage Page (Generic Desktop)
    0x09, 0x33,              //   Usage (Rx)
    0x09, 0x34,              //   Usage (Ry)
    0x15, 0x00,              //   Logical Minimum (0)
    0x26, 0xFF, 0x00,        //   Logical Maximum (255)
    0x75, 0x08,              //   Report Size (8)
    0x95, 0x02,              //   Report Count (2)
    0x81, 0x02,              //   Input (Data, Var, Abs) -- triggers
    0x06, 0x00, 0xFF,        //   Usage Page (Vendor 0xFF00)
    0x09, 0x21,              //   Usage (0x21)
    0x95, 0x36,              //   Report Count (54)
    0x81, 0x02,              //   Input (Data, Var, Abs)
    0x85, 0x05,              //   Report ID (5)
    0x09, 0x22,              //   Usage (0x22)
    0x95, 0x1F,              //   Report Count (31)
    0x91, 0x02,              //   Output (Data, Var, Abs) -- rumble / lightbar
    0xC0,                    // End Collection
};

#endif  // HURRA_MCXN947_TEST_HID_FIXTURES_H
