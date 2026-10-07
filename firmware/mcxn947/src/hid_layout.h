// The compiled injection layout shared by the mouse and pad compilers.
//
// One struct describes one report of one interface: which report ID, how long
// it is, and the fields injection may touch. `device_class` says which compiler
// produced it and therefore how `kind` reads: a mouse layout's kinds are
// hid_mouse_kind_t, a pad layout's are hid_pad_kind_t, and the two overlap
// numerically on purpose -- a pad axis kind IS its wire channel. inj_map_build.c
// is the only consumer that cares, and it branches on the class first.

#ifndef HURRA_MCXN947_HID_LAYOUT_H
#define HURRA_MCXN947_HID_LAYOUT_H

#include <stdint.h>

// Mouse: four axes plus up to four button runs. Pad: seven channels plus
// button runs. 16 is also the cap on MAP_ENTRY frames per upload, which bounds
// the CRC the session computes in interrupt context (16 x 26 = 416 bytes).
#define HID_LAYOUT_MAX_FIELDS 16u
#define HID_MOUSE_MAX_FIELDS HID_LAYOUT_MAX_FIELDS

typedef enum {
    HID_DEVICE_CLASS_NONE = 0,
    HID_DEVICE_CLASS_MOUSE = 1,
    HID_DEVICE_CLASS_PAD = 2,
} hid_device_class_t;

typedef enum {
    HID_MOUSE_X = 0,
    HID_MOUSE_Y,
    HID_MOUSE_WHEEL,
    HID_MOUSE_PAN,
    HID_MOUSE_BUTTONS,
} hid_mouse_kind_t;

// A pad axis kind equals its MAP_ENTRY channel (injection_wire.h
// INJ_MAP_ENTRY_CHANNEL_*); inj_map_build.c pins the HID_PAD_KIND side and
// usb_console.c the kmcmd side, since this header must not include the wire contract.
typedef enum {
    HID_PAD_KIND_LX = 0,
    HID_PAD_KIND_LY = 1,
    HID_PAD_KIND_RX = 2,
    HID_PAD_KIND_RY = 3,
    HID_PAD_KIND_LT = 4,
    HID_PAD_KIND_RT = 5,
    HID_PAD_KIND_HAT = 6,
    HID_PAD_KIND_BUTTONS = 7,
} hid_pad_kind_t;

#define HID_LAYOUT_AXIS_BIT(kind) ((uint8_t)(1u << (unsigned)(kind)))
#define HID_MOUSE_AXIS_BIT(kind) HID_LAYOUT_AXIS_BIT(kind)

typedef struct {
    uint8_t kind;              // hid_mouse_kind_t or hid_pad_kind_t, per device_class
    uint8_t null_state;        // 1 when the Input item declared Null State (hat: 8 is "centred")
    uint16_t usage_page;
    uint16_t usage;            // BUTTONS: the first button number (>= 1)
    uint16_t bit_offset;       // in the report as sent, ID byte included
    uint8_t bit_width;         // BUTTONS: how many consecutive buttons
    int32_t logical_minimum;
    int32_t logical_maximum;
} hid_layout_field_t;
typedef hid_layout_field_t hid_mouse_field_t;

typedef struct {
    uint8_t report_id;          // 0 = no report IDs
    uint8_t report_length;      // bytes as declared, ID byte included
    uint8_t min_report_length;  // bytes needed to reach every mapped field
    uint8_t field_count;
    uint8_t axes;               // HID_LAYOUT_AXIS_BIT() of each axis kind present
    uint8_t device_class;       // hid_device_class_t; sits in the padding before fields
    hid_layout_field_t fields[HID_LAYOUT_MAX_FIELDS];  // axes in kind order, then button runs
} hid_layout_t;
typedef hid_layout_t hid_mouse_layout_t;

// device_class took a padding byte: the header is still 8 bytes and the struct
// is exactly the header plus the field array.
_Static_assert(sizeof(hid_layout_field_t) == 20u, "hid_layout_field_t is 20 bytes");
_Static_assert(sizeof(hid_layout_t) == 8u + HID_LAYOUT_MAX_FIELDS * sizeof(hid_layout_field_t),
               "device_class must live in the layout header's padding");

#endif  // HURRA_MCXN947_HID_LAYOUT_H
