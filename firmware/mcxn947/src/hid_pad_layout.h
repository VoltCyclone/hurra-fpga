// Pad-layout policy: which fields of a game pad's report descriptor injection
// may SET.
//
// Walks the descriptor with hid_fields and keeps what sits inside a Generic
// Desktop Joystick (0x04), Game Pad (0x05) or Multi-axis Controller (0x08)
// application collection: absolute variable axes, a hat switch, and runs of
// buttons. The channel table is fixed: X->LX, Y->LY, Z->RX, Rz->RY (the DS4
// convention), Rx->LT, Ry->RT, Hat Switch->HAT; Simulation Accelerator (0xC4)
// and Brake (0xC5) fill LT/RT only when the report has no Rx/Ry. The layout's
// report is the first one carrying both X and Y.
//
// A pad whose fields exceed HID_LAYOUT_MAX_FIELDS is UNSUPPORTED, never
// truncated: a channel silently dropped with an OK status would be a stick the
// MCU thinks it holds and the FPGA never writes.
//
// Portable and MMIO-free; host-tested by hid_pad_layout_test.c. Not reentrant:
// its walker and workspace are its own statics (hid_mouse_layout.c's are
// file-local), ~790 bytes, because core0's stack is 2 KiB.

#ifndef HURRA_MCXN947_HID_PAD_LAYOUT_H
#define HURRA_MCXN947_HID_PAD_LAYOUT_H

#include <stddef.h>
#include <stdint.h>

#include "hid_layout.h"

typedef enum {
    HID_PAD_OK = 0,
    HID_PAD_NOT_PAD,      // no pad application collection at all
    HID_PAD_NO_AXES,      // a pad, but no absolute X and Y in one report
    HID_PAD_UNSUPPORTED,  // a pad, beyond what the link or the layout can carry
    HID_PAD_MALFORMED,    // the descriptor itself does not parse
} hid_pad_status_t;

// Compile one interface's report descriptor. On anything but OK, *out is
// zeroed. Foreground only.
hid_pad_status_t hid_pad_compile(const uint8_t *descriptor, size_t length, hid_layout_t *out);

#endif  // HURRA_MCXN947_HID_PAD_LAYOUT_H
