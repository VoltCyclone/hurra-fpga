// See hid_descriptor_set.h.

#include "hid_descriptor_set.h"

#include <string.h>

void hid_descriptor_set_init(hid_descriptor_set_t *set)
{
    memset(set, 0, sizeof(*set));
    for (uint8_t i = 0u; i < INJ_MAX_INTERFACES; ++i) {
        hid_descriptor_reassembly_init(&set->slot[i], i);
    }
}

// The slot holding `iface`, claiming the lowest free one for a new number.
static uint8_t slot_for(hid_descriptor_set_t *set, uint8_t iface)
{
    for (uint8_t i = 0u; i < INJ_MAX_INTERFACES; ++i) {
        if ((set->used_mask & (1u << i)) != 0u && set->slot_interface[i] == iface) {
            return i;
        }
    }
    for (uint8_t i = 0u; i < INJ_MAX_INTERFACES; ++i) {
        if ((set->used_mask & (1u << i)) == 0u) {
            hid_descriptor_reassembly_retarget(&set->slot[i], iface);
            set->slot_interface[i] = iface;
            set->used_mask = (uint8_t)(set->used_mask | (1u << i));
            return i;
        }
    }
    return HID_DESCRIPTOR_SET_NO_SLOT;
}

hid_descriptor_set_push_t hid_descriptor_set_push(hid_descriptor_set_t *set,
                                                  const inj_descriptor_fragment_payload_t *fragment)
{
    hid_descriptor_set_push_t r;
    memset(&r, 0, sizeof(r));

    if (!set->have_generation || fragment->descriptor_generation != set->generation) {
        for (uint8_t i = 0u; i < INJ_MAX_INTERFACES; ++i) {
            hid_descriptor_reassembly_retarget(&set->slot[i], set->slot_interface[i]);
        }
        set->used_mask = 0u;
        set->generation = fragment->descriptor_generation;
        set->have_generation = true;
        set->dirty_mask = 0u;
        r.generation_changed = true;
    }

    r.slot = slot_for(set, fragment->interface_number);
    if (r.slot == HID_DESCRIPTOR_SET_NO_SLOT) {
        r.result = HID_DESCRIPTOR_FRAGMENT_IGNORED_INTERFACE;
        return r;
    }

    hid_descriptor_reassembly_t *slot = &set->slot[r.slot];
    const bool was_complete = hid_descriptor_reassembly_complete(slot);
    r.result = hid_descriptor_reassembly_push(slot, fragment);
    // Only the edge counts: the reassembler answers COMPLETE to every duplicate
    // of a complete descriptor, and the FPGA re-exports on every link-up.
    r.newly_complete = !was_complete && hid_descriptor_reassembly_complete(slot);
    if (r.newly_complete) {
        set->dirty_mask = (uint8_t)(set->dirty_mask | (1u << r.slot));
    }
    return r;
}

bool hid_descriptor_set_take_dirty(hid_descriptor_set_t *set, uint8_t *slot)
{
    const uint8_t dirty = set->dirty_mask;
    for (uint8_t i = 0u; i < INJ_MAX_INTERFACES; ++i) {
        if ((dirty & (1u << i)) != 0u) {
            set->dirty_mask = (uint8_t)(dirty & ~(1u << i));
            *slot = i;
            return true;
        }
    }
    return false;
}

static bool slot_claimed(const hid_descriptor_set_t *set, uint8_t slot)
{
    return slot < INJ_MAX_INTERFACES && (set->used_mask & (1u << slot)) != 0u;
}

bool hid_descriptor_set_snapshot(const hid_descriptor_set_t *set, uint8_t slot,
                                 hid_descriptor_snapshot_t *out)
{
    if (!slot_claimed(set, slot)) {
        return false;
    }
    const hid_descriptor_reassembly_t *r = &set->slot[slot];
    if (!hid_descriptor_reassembly_complete(r)) {
        return false;
    }
    out->slot = slot;
    out->interface_number = set->slot_interface[slot];
    out->generation = hid_descriptor_reassembly_generation(r);
    out->revision = r->revision;
    out->bytes = hid_descriptor_reassembly_data(r);
    out->length = (uint16_t)hid_descriptor_reassembly_length(r);
    return true;
}

bool hid_descriptor_set_snapshot_valid(const hid_descriptor_set_t *set,
                                       const hid_descriptor_snapshot_t *snapshot)
{
    // A slot re-claimed for another number was retargeted, which bumps its
    // revision too; the tag check says so outright.
    if (!slot_claimed(set, snapshot->slot) ||
        set->slot_interface[snapshot->slot] != snapshot->interface_number) {
        return false;
    }
    const hid_descriptor_reassembly_t *r = &set->slot[snapshot->slot];
    return hid_descriptor_reassembly_complete(r) && r->revision == snapshot->revision &&
           hid_descriptor_reassembly_generation(r) == snapshot->generation;
}

uint8_t hid_descriptor_set_complete_mask(const hid_descriptor_set_t *set)
{
    uint8_t mask = 0u;
    for (uint8_t i = 0u; i < INJ_MAX_INTERFACES; ++i) {
        if (slot_claimed(set, i) && hid_descriptor_reassembly_complete(&set->slot[i])) {
            mask = (uint8_t)(mask | (1u << i));
        }
    }
    return mask;
}

void hid_descriptor_set_redirty(hid_descriptor_set_t *set, uint8_t mask)
{
    set->dirty_mask = (uint8_t)(set->dirty_mask | (mask & hid_descriptor_set_complete_mask(set)));
}
