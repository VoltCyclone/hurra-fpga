// Mouse-layout policy: which fields of a report descriptor injection may touch.
//
// Walks the descriptor with hid_fields and keeps only what sits inside a
// Generic Desktop / Mouse application collection (0x01/0x02): relative X, Y,
// Wheel and AC Pan, and runs of buttons. Anything that is not a mouse --
// keyboards, game pads, vendor collections -- yields NOT_MOUSE, which is the
// whole of the protection against a mouse move typing a key or nudging a stick.
//
// The result is a plain description of the report, with no wire types;
// inj_map_build.c turns it into MAP_ENTRY payloads. Portable and MMIO-free;
// host-tested by hid_mouse_layout_test.c.

#ifndef HURRA_MCXN947_HID_MOUSE_LAYOUT_H
#define HURRA_MCXN947_HID_MOUSE_LAYOUT_H

#include <stddef.h>
#include <stdint.h>

#include "hid_layout.h"

// The smallest logical range an injectable axis may have. The engine clamps an
// injected sum into the field's range, so a field that cannot hold a whole
// kmcmd step would quietly eat most of every move: kmcmd steps X/Y by up to
// KMCMD_STEP_MAX (pinned below this by hid_mouse_layout_test.c) and wheel and
// pan by exactly 1. +/-127 is the boot mouse's own range.
#define HID_MOUSE_XY_MINIMUM_SPAN 127
#define HID_MOUSE_WHEEL_MINIMUM_SPAN 1

#define HID_MOUSE_MAX_BUTTON_RUNS 4u

typedef enum {
    HID_MOUSE_OK = 0,
    HID_MOUSE_NOT_MOUSE,    // no mouse application collection at all
    HID_MOUSE_NO_AXES,      // a mouse, but no injectable relative X and Y in one report
    HID_MOUSE_UNSUPPORTED,  // injectable in principle, beyond what the link can carry
    HID_MOUSE_MALFORMED,    // the descriptor itself does not parse
} hid_mouse_status_t;

// Compile one interface's report descriptor. On anything but OK, *out is
// zeroed. Not reentrant: it uses a static walker and workspace (core0's stack
// is 2 KiB), and it runs only in the foreground.
hid_mouse_status_t hid_mouse_compile(const uint8_t *descriptor, size_t length,
                                     hid_layout_t *out);

#endif  // HURRA_MCXN947_HID_MOUSE_LAYOUT_H
