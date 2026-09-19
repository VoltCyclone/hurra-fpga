// Portable, single-interface reassembly for HID report-descriptor telemetry.

#ifndef HURRA_MCXN947_HID_DESCRIPTOR_REASSEMBLY_H
#define HURRA_MCXN947_HID_DESCRIPTOR_REASSEMBLY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "injection_wire.h"

// The generated wire contract bounds one interface's descriptor at 2048
// bytes. That is deliberately larger than the roughly 50-byte boot-mouse
// minimum because shipping mice commonly add report IDs, consumer controls,
// and vendor collections. Matching the producer's bound means a conforming
// descriptor is never silently truncated. One bit per byte records gaps and
// duplicates without doubling the 2 KiB payload cost.
#define HID_DESCRIPTOR_REASSEMBLY_CAPACITY INJ_MAX_DESCRIPTOR_BYTES_PER_INTERFACE
#define HID_DESCRIPTOR_REASSEMBLY_BITMAP_BYTES \
    ((HID_DESCRIPTOR_REASSEMBLY_CAPACITY + 7u) / 8u)

typedef enum {
    HID_DESCRIPTOR_FRAGMENT_ACCEPTED = 0,
    HID_DESCRIPTOR_FRAGMENT_COMPLETE,
    HID_DESCRIPTOR_FRAGMENT_IGNORED_INTERFACE,
    HID_DESCRIPTOR_FRAGMENT_ERR_ARGUMENT,
    HID_DESCRIPTOR_FRAGMENT_ERR_TOTAL,
    HID_DESCRIPTOR_FRAGMENT_ERR_RANGE,
    HID_DESCRIPTOR_FRAGMENT_ERR_CONFLICT,
} hid_descriptor_fragment_result_t;

typedef struct {
    uint8_t data[HID_DESCRIPTOR_REASSEMBLY_CAPACITY];
    uint8_t received[HID_DESCRIPTOR_REASSEMBLY_BITMAP_BYTES];
    uint16_t generation;
    uint16_t total;
    uint16_t received_count;
    uint8_t target_interface;
    bool active;
} hid_descriptor_reassembly_t;

// One instance follows exactly one interface. Retargeting always discards the
// old bitmap and payload metadata; fragments for every other interface are
// ignored without disturbing the selected interface's partial descriptor.
void hid_descriptor_reassembly_init(hid_descriptor_reassembly_t *reassembly,
                                    uint8_t target_interface);
void hid_descriptor_reassembly_retarget(hid_descriptor_reassembly_t *reassembly,
                                        uint8_t target_interface);
hid_descriptor_fragment_result_t hid_descriptor_reassembly_push(
    hid_descriptor_reassembly_t *reassembly,
    const inj_descriptor_fragment_payload_t *fragment);

// Data is exposed only while a valid-sized stream is active. Decode it only
// after complete() is true; before then its gaps intentionally remain stale.
bool hid_descriptor_reassembly_complete(
    const hid_descriptor_reassembly_t *reassembly);
const uint8_t *hid_descriptor_reassembly_data(
    const hid_descriptor_reassembly_t *reassembly);
size_t hid_descriptor_reassembly_length(
    const hid_descriptor_reassembly_t *reassembly);
uint16_t hid_descriptor_reassembly_generation(
    const hid_descriptor_reassembly_t *reassembly);
uint8_t hid_descriptor_reassembly_target_interface(
    const hid_descriptor_reassembly_t *reassembly);

#endif  // HURRA_MCXN947_HID_DESCRIPTOR_REASSEMBLY_H
