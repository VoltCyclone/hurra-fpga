// Every captured interface's report descriptor, reassembled side by side.
//
// The FPGA exports each HID interface's descriptor ONCE per trigger, one after
// another (descriptor_export.py). A single reassembler that locks onto the first
// interface therefore loses the rest for good, and the mouse is often not the
// first -- so this holds one reassembler per interface: INJ_MAX_INTERFACES
// slots, each claimed by the first fragment of an interface number in a
// generation and tagged with it. Numbers are tags, not indices: a device may
// number its four interfaces 0, 1, 2, 5. Everything slot-indexed here (the
// dirty and complete masks) means slots; a snapshot carries the real number.
//
// Written from the retirement ISR, read by the foreground, which compiles a
// descriptor without masking the ISR for the ~1 ms that can take. The read is
// made safe by snapshot-and-validate: a complete slot's bytes can only change
// after a discard, and every discard bumps the slot's revision, so a snapshot
// whose generation and revision still match after the compile read stable
// bytes. Portable and MMIO-free; host-tested by hid_descriptor_set_test.c.

#ifndef HURRA_MCXN947_HID_DESCRIPTOR_SET_H
#define HURRA_MCXN947_HID_DESCRIPTOR_SET_H

#include <stdbool.h>
#include <stdint.h>

#include "hid_descriptor_reassembly.h"
#include "injection_wire.h"

// push()'s slot for a fragment no slot could take.
#define HID_DESCRIPTOR_SET_NO_SLOT UINT8_C(0xFF)

// The masks below are uint8_t.
_Static_assert(INJ_MAX_INTERFACES <= 8, "slot masks are 8 bits wide");

typedef struct {
    hid_descriptor_reassembly_t slot[INJ_MAX_INTERFACES];
    // The interface number slot i holds, where bit i of used_mask is set.
    uint8_t slot_interface[INJ_MAX_INTERFACES];
    uint8_t used_mask;
    uint16_t generation;
    bool have_generation;
    // Slots whose descriptor has newly completed and not yet been taken.
    // Set by the ISR, cleared by the foreground with the ISR masked.
    volatile uint8_t dirty_mask;
} hid_descriptor_set_t;

typedef struct {
    hid_descriptor_fragment_result_t result;
    // The slot the fragment's interface holds, or HID_DESCRIPTOR_SET_NO_SLOT
    // when every slot is held by another number (result IGNORED_INTERFACE).
    uint8_t slot;
    bool newly_complete;      // this fragment completed its interface's descriptor
    bool generation_changed;  // and discarded every slot of the previous device
} hid_descriptor_set_push_t;

typedef struct {
    uint8_t slot;
    uint8_t interface_number;
    uint16_t generation;
    uint16_t revision;
    const uint8_t *bytes;
    uint16_t length;
} hid_descriptor_snapshot_t;

void hid_descriptor_set_init(hid_descriptor_set_t *set);

// ISR side. A fragment of a new generation frees and discards EVERY slot
// first, not just its own: an interface the new device does not have must not
// stay "complete" with the old device's bytes, and the new device's numbers
// claim slots afresh.
hid_descriptor_set_push_t hid_descriptor_set_push(hid_descriptor_set_t *set,
                                                  const inj_descriptor_fragment_payload_t *fragment);

// Foreground side; call both with the ISR masked. take_dirty clears and returns
// the lowest dirty slot. snapshot fails unless that slot is claimed and complete.
bool hid_descriptor_set_take_dirty(hid_descriptor_set_t *set, uint8_t *slot);
bool hid_descriptor_set_snapshot(const hid_descriptor_set_t *set, uint8_t slot,
                                 hid_descriptor_snapshot_t *out);

// Bit i set when slot i holds a complete descriptor right now.
uint8_t hid_descriptor_set_complete_mask(const hid_descriptor_set_t *set);

// Mark again, for another compile, those slots in `mask` that are complete.
// Foreground, masked. For a verdict that was dropped (its snapshot went stale)
// on a slot the FPGA will not export again.
void hid_descriptor_set_redirty(hid_descriptor_set_t *set, uint8_t mask);

// Masked again after reading snapshot->bytes: true when nothing touched them.
// On false, drop the result -- the slot's next completion re-marks it dirty.
bool hid_descriptor_set_snapshot_valid(const hid_descriptor_set_t *set,
                                       const hid_descriptor_snapshot_t *snapshot);

#endif  // HURRA_MCXN947_HID_DESCRIPTOR_SET_H
