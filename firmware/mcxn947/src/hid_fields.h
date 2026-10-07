// HID report-descriptor field walker: the item state machine above hid_item.
//
// Walks one interface's report descriptor and emits one hid_field_t per run of
// INPUT elements -- where each lands in the report as sent, how wide it is,
// what it means, and which top-level Application collection it belongs to.
// Output and Feature items are parsed (they carry globals) but never emitted
// and never move a bit cursor: a keyboard's LED output report or a DS4's many
// feature reports must not shift where its input fields are.
//
// This is a field MODEL, not a policy. Deciding which fields are injectable is
// hid_mouse_layout.c's job. Portable and MMIO-free; host-tested by
// hid_fields_test.c.

#ifndef HURRA_MCXN947_HID_FIELDS_H
#define HURRA_MCXN947_HID_FIELDS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Bounds on one walk. A descriptor that exceeds one is refused (LIMIT) rather
// than half-understood -- except the usage list, whose overflow only loses the
// names of trailing elements, which are then reported as usage 0.
#define HID_FIELDS_MAX_USAGES 16u
#define HID_FIELDS_MAX_COLLECTION_DEPTH 8u
#define HID_FIELDS_MAX_REPORT_IDS 16u
#define HID_FIELDS_MAX_GLOBAL_STACK 4u

// Input item data bits (HID 1.11 section 6.2.2.5) that the layer above reads.
#define HID_FIELD_CONSTANT 0x01u  // 0 = Data
#define HID_FIELD_VARIABLE 0x02u  // 0 = Array
#define HID_FIELD_RELATIVE 0x04u  // 0 = Absolute
#define HID_FIELD_NULL_STATE 0x40u  // 0 = No Null position

typedef struct {
    uint8_t report_id;         // 0 = the descriptor declares no report IDs
    uint16_t bit_offset;       // in the report AS SENT: an ID'd report's byte 0
                               // is the ID, so its fields start at bit 8
    uint8_t bit_size;          // per element
    uint16_t count;            // elements in this run
    uint32_t flags;            // the Input item's data bits
    uint16_t usage_page;       // of element 0
    uint16_t usage;            // of element 0
    bool usage_is_range;       // element i is usage + i; otherwise all share usage
    int32_t logical_minimum;
    int32_t logical_maximum;   // unsigned unless logical_minimum < 0 (HID 1.11 6.2.2.7)
    uint16_t app_usage_page;   // enclosing top-level Application collection;
    uint16_t app_usage;        // both 0 when outside one
} hid_field_t;

typedef enum {
    HID_FIELDS_OK = 0,
    HID_FIELDS_TRUNCATED,   // an item runs past the end of the descriptor
    HID_FIELDS_UNBALANCED,  // End Collection without a Collection, or unclosed at the end
    HID_FIELDS_LIMIT,       // a bound above was exceeded
    HID_FIELDS_BAD_ID,      // Report ID 0, which HID 1.11 reserves
    HID_FIELDS_STOPPED,     // the callback asked to stop
} hid_fields_status_t;

// Return false to stop the walk early.
typedef bool (*hid_field_fn)(void *context, const hid_field_t *field);

typedef struct {
    int32_t logical_minimum;
    uint32_t logical_maximum_raw;  // interpreted against the minimum's sign at use
    uint8_t logical_maximum_size;
    uint16_t usage_page;
    uint32_t report_size;
    uint32_t report_count;
    uint8_t report_id;
} hid_fields_globals_t;

typedef struct {
    uint8_t id;
    uint32_t bits;  // Input bits so far, including the ID byte for a nonzero ID
} hid_fields_cursor_t;

// A local usage as declared. A 4-byte item names its own page in bits 31..16
// -- vendor pages included, so no bit of `value` is free to mark the other
// kind. A 1- or 2-byte item names only a usage ID, whose page is the Usage Page
// in effect at the MAIN item (as Linux's hid-core resolves it).
typedef struct {
    uint32_t value;
    bool is_short;
} hid_fields_usage_t;

// About 400 bytes. Callers keep it static: core0's whole stack is 2 KiB.
typedef struct {
    hid_fields_globals_t globals;
    hid_fields_globals_t global_stack[HID_FIELDS_MAX_GLOBAL_STACK];
    uint8_t global_depth;

    hid_fields_usage_t usages[HID_FIELDS_MAX_USAGES];
    uint8_t usage_count;
    bool usage_overflow;
    hid_fields_usage_t usage_minimum;
    bool have_usage_minimum;
    hid_fields_usage_t usage_maximum;
    bool have_usage_maximum;

    uint8_t collection_depth;
    uint16_t app_usage_page;
    uint16_t app_usage;

    hid_fields_cursor_t cursors[HID_FIELDS_MAX_REPORT_IDS];
    uint8_t cursor_count;
} hid_fields_walker_t;

// Walk `descriptor`, calling `emit` for each Input run in descriptor order.
// `emit` may be NULL to only measure. The walker's state stays readable after
// the walk for hid_fields_input_bits().
hid_fields_status_t hid_fields_walk(hid_fields_walker_t *walker, const uint8_t *descriptor,
                                    size_t length, hid_field_fn emit, void *context);

// Total Input bits of one report after a walk, the ID byte included for a
// nonzero ID -- so (bits + 7) / 8 is the report's length on the wire. 0 when
// the descriptor declares no Input for that ID.
uint32_t hid_fields_input_bits(const hid_fields_walker_t *walker, uint8_t report_id);

#endif  // HURRA_MCXN947_HID_FIELDS_H
