// HID report-descriptor item tokenizer (HID 1.11 section 6.2.2.2-6.2.2.3).
//
// The bottom layer of the descriptor stack: it splits bytes into items and
// decodes their data, and holds no state between calls. Everything that gives
// items meaning -- globals, locals, collections, bit positions -- lives in
// hid_fields.c above it. Portable and MMIO-free; host-tested by hid_item_test.c.

#ifndef HURRA_MCXN947_HID_ITEM_H
#define HURRA_MCXN947_HID_ITEM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    HID_ITEM_OK = 0,
    HID_ITEM_END,        // *pos == length: nothing left to read
    HID_ITEM_TRUNCATED,  // the item's declared size runs past the end
} hid_item_status_t;

#define HID_ITEM_TYPE_MAIN 0u
#define HID_ITEM_TYPE_GLOBAL 1u
#define HID_ITEM_TYPE_LOCAL 2u
#define HID_ITEM_TYPE_RESERVED 3u

typedef struct {
    uint8_t type;     // HID_ITEM_TYPE_*
    uint8_t tag;      // bTag; for a long item, bLongItemTag
    uint8_t size;     // data bytes: 0, 1, 2 or 4 for a short item
    bool is_long;     // long items carry no value the stack above interprets
    uint32_t value;   // data, little-endian, zero-extended
    int32_t svalue;   // the same data sign-extended from its own width
} hid_item_t;

// Read the item at *pos and advance *pos past it. On TRUNCATED nothing is
// decoded and *pos is left at the length, so a caller looping until !OK always
// terminates.
hid_item_status_t hid_item_next(const uint8_t *descriptor, size_t length, size_t *pos,
                                hid_item_t *item);

#endif  // HURRA_MCXN947_HID_ITEM_H
