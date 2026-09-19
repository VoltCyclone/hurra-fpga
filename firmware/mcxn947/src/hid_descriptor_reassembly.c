#include "hid_descriptor_reassembly.h"

#include <string.h>

static void discard_descriptor(hid_descriptor_reassembly_t *reassembly)
{
    memset(reassembly->received, 0, sizeof(reassembly->received));
    reassembly->generation = 0u;
    reassembly->total = 0u;
    reassembly->received_count = 0u;
    reassembly->active = false;
}

static bool byte_received(const hid_descriptor_reassembly_t *reassembly,
                          size_t index)
{
    const size_t byte_index = index / 8u;
    const uint8_t bit_index = (uint8_t)(index % 8u);
    return (reassembly->received[byte_index] &
            (uint8_t)(UINT8_C(1) << bit_index)) != 0u;
}

static void mark_received(hid_descriptor_reassembly_t *reassembly,
                          size_t index)
{
    const size_t byte_index = index / 8u;
    const uint8_t bit_index = (uint8_t)(index % 8u);
    reassembly->received[byte_index] |=
        (uint8_t)(UINT8_C(1) << bit_index);
}

void hid_descriptor_reassembly_init(hid_descriptor_reassembly_t *reassembly,
                                    uint8_t target_interface)
{
    if (reassembly == NULL) {
        return;
    }
    reassembly->target_interface = target_interface;
    discard_descriptor(reassembly);
}

void hid_descriptor_reassembly_retarget(hid_descriptor_reassembly_t *reassembly,
                                        uint8_t target_interface)
{
    if (reassembly == NULL) {
        return;
    }
    reassembly->target_interface = target_interface;
    discard_descriptor(reassembly);
}

hid_descriptor_fragment_result_t hid_descriptor_reassembly_push(
    hid_descriptor_reassembly_t *reassembly,
    const inj_descriptor_fragment_payload_t *fragment)
{
    if (reassembly == NULL || fragment == NULL) {
        return HID_DESCRIPTOR_FRAGMENT_ERR_ARGUMENT;
    }
    if (fragment->interface_number != reassembly->target_interface) {
        return HID_DESCRIPTOR_FRAGMENT_IGNORED_INTERFACE;
    }

    if (reassembly->active &&
        fragment->descriptor_generation != reassembly->generation) {
        // A generation identifies the whole captured descriptor set. Keeping
        // even non-overlapping bytes across a change could fabricate a valid-
        // looking descriptor from two different devices.
        discard_descriptor(reassembly);
    }

    const size_t total = fragment->total;
    if (total == 0u || total > HID_DESCRIPTOR_REASSEMBLY_CAPACITY) {
        discard_descriptor(reassembly);
        return HID_DESCRIPTOR_FRAGMENT_ERR_TOTAL;
    }

    if (reassembly->active && fragment->total != reassembly->total) {
        discard_descriptor(reassembly);
        return HID_DESCRIPTOR_FRAGMENT_ERR_TOTAL;
    }

    const size_t offset = fragment->offset;
    if (offset >= total) {
        return HID_DESCRIPTOR_FRAGMENT_ERR_RANGE;
    }

    if (!reassembly->active) {
        reassembly->generation = fragment->descriptor_generation;
        reassembly->total = fragment->total;
        reassembly->received_count = 0u;
        reassembly->active = true;
    }

    // Subtraction establishes the bound without ever evaluating offset plus
    // an attacker-controlled length, so a near-UINT16_MAX offset cannot wrap.
    const size_t remaining = total - offset;
    const size_t fragment_length =
        remaining < sizeof(fragment->data) ? remaining : sizeof(fragment->data);

    // Reject the whole overlapping fragment before changing state. Otherwise
    // a conflicting retransmission could overwrite a prefix and still leave a
    // descriptor marked complete.
    for (size_t i = 0u; i < fragment_length; ++i) {
        const size_t index = offset + i;
        if (byte_received(reassembly, index) &&
            reassembly->data[index] != fragment->data[i]) {
            return HID_DESCRIPTOR_FRAGMENT_ERR_CONFLICT;
        }
    }

    for (size_t i = 0u; i < fragment_length; ++i) {
        const size_t index = offset + i;
        if (!byte_received(reassembly, index)) {
            reassembly->data[index] = fragment->data[i];
            mark_received(reassembly, index);
            reassembly->received_count++;
        }
    }

    return hid_descriptor_reassembly_complete(reassembly)
               ? HID_DESCRIPTOR_FRAGMENT_COMPLETE
               : HID_DESCRIPTOR_FRAGMENT_ACCEPTED;
}

bool hid_descriptor_reassembly_complete(
    const hid_descriptor_reassembly_t *reassembly)
{
    return reassembly != NULL && reassembly->active &&
           reassembly->received_count == reassembly->total;
}

const uint8_t *hid_descriptor_reassembly_data(
    const hid_descriptor_reassembly_t *reassembly)
{
    return reassembly != NULL && reassembly->active ? reassembly->data : NULL;
}

size_t hid_descriptor_reassembly_length(
    const hid_descriptor_reassembly_t *reassembly)
{
    return reassembly != NULL && reassembly->active ? reassembly->total : 0u;
}

uint16_t hid_descriptor_reassembly_generation(
    const hid_descriptor_reassembly_t *reassembly)
{
    return reassembly != NULL && reassembly->active ? reassembly->generation
                                                     : 0u;
}

uint8_t hid_descriptor_reassembly_target_interface(
    const hid_descriptor_reassembly_t *reassembly)
{
    return reassembly != NULL ? reassembly->target_interface : 0u;
}
