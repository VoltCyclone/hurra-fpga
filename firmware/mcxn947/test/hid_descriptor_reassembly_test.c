#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "hid_descriptor_reassembly.h"

static inj_descriptor_fragment_payload_t fragment(uint16_t generation,
                                                  uint8_t interface_number,
                                                  uint16_t offset,
                                                  uint16_t total,
                                                  uint8_t seed)
{
    inj_descriptor_fragment_payload_t frag = {
        .descriptor_generation = generation,
        .interface_number = interface_number,
        .offset = offset,
        .total = total,
    };
    for (size_t i = 0u; i < sizeof(frag.data); ++i) {
        frag.data[i] = (uint8_t)(seed + (uint8_t)i);
    }
    return frag;
}

static void test_out_of_order_fragments_complete_without_splicing_gaps(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 2u);

    const inj_descriptor_fragment_payload_t middle = fragment(7u, 2u, 18u, 40u, 18u);
    const inj_descriptor_fragment_payload_t first = fragment(7u, 2u, 0u, 40u, 0u);
    const inj_descriptor_fragment_payload_t last = fragment(7u, 2u, 36u, 40u, 36u);

    assert(hid_descriptor_reassembly_push(&reassembly, &middle) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_push(&reassembly, &first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_push(&reassembly, &last) ==
           HID_DESCRIPTOR_FRAGMENT_COMPLETE);
    assert(hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_length(&reassembly) == 40u);
    assert(hid_descriptor_reassembly_generation(&reassembly) == 7u);

    const uint8_t *const bytes = hid_descriptor_reassembly_data(&reassembly);
    assert(bytes != NULL);
    for (size_t i = 0u; i < 40u; ++i) {
        assert(bytes[i] == (uint8_t)i);
    }
}

static void test_gap_keeps_descriptor_incomplete(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 1u);
    const inj_descriptor_fragment_payload_t first = fragment(3u, 1u, 0u, 40u, 0u);
    const inj_descriptor_fragment_payload_t last = fragment(3u, 1u, 36u, 40u, 36u);

    assert(hid_descriptor_reassembly_push(&reassembly, &first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(hid_descriptor_reassembly_push(&reassembly, &last) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
}

static void test_duplicate_fragment_does_not_fake_progress(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 1u);
    const inj_descriptor_fragment_payload_t first = fragment(3u, 1u, 0u, 36u, 0u);

    assert(hid_descriptor_reassembly_push(&reassembly, &first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(hid_descriptor_reassembly_push(&reassembly, &first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
}

static void test_interleaved_other_interface_is_ignored(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 2u);
    const inj_descriptor_fragment_payload_t first = fragment(9u, 2u, 0u, 20u, 0u);
    const inj_descriptor_fragment_payload_t other = fragment(10u, 3u, 0u, 18u, 0xa0u);
    const inj_descriptor_fragment_payload_t last = fragment(9u, 2u, 18u, 20u, 18u);

    assert(hid_descriptor_reassembly_push(&reassembly, &first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(hid_descriptor_reassembly_push(&reassembly, &other) ==
           HID_DESCRIPTOR_FRAGMENT_IGNORED_INTERFACE);
    assert(hid_descriptor_reassembly_push(&reassembly, &last) ==
           HID_DESCRIPTOR_FRAGMENT_COMPLETE);
    assert(hid_descriptor_reassembly_generation(&reassembly) == 9u);
    assert(hid_descriptor_reassembly_data(&reassembly)[19] == 19u);
}

static void test_generation_change_discards_partial_descriptor(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t old_first = fragment(1u, 0u, 0u, 36u, 0u);
    const inj_descriptor_fragment_payload_t new_last = fragment(2u, 0u, 18u, 36u, 0x80u);
    const inj_descriptor_fragment_payload_t new_first = fragment(2u, 0u, 0u, 36u, 0x40u);

    assert(hid_descriptor_reassembly_push(&reassembly, &old_first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(hid_descriptor_reassembly_push(&reassembly, &new_last) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_push(&reassembly, &new_first) ==
           HID_DESCRIPTOR_FRAGMENT_COMPLETE);
    assert(hid_descriptor_reassembly_generation(&reassembly) == 2u);
    assert(hid_descriptor_reassembly_data(&reassembly)[0] == 0x40u);
    assert(hid_descriptor_reassembly_data(&reassembly)[18] == 0x80u);
}

static void test_retarget_discards_partial_descriptor(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t old = fragment(1u, 0u, 0u, 36u, 0u);
    const inj_descriptor_fragment_payload_t selected = fragment(1u, 3u, 0u, 18u, 0x20u);

    assert(hid_descriptor_reassembly_push(&reassembly, &old) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    hid_descriptor_reassembly_retarget(&reassembly, 3u);
    assert(hid_descriptor_reassembly_target_interface(&reassembly) == 3u);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_length(&reassembly) == 0u);
    assert(hid_descriptor_reassembly_push(&reassembly, &selected) ==
           HID_DESCRIPTOR_FRAGMENT_COMPLETE);
    assert(hid_descriptor_reassembly_data(&reassembly)[0] == 0x20u);
}

static void test_oversize_total_is_rejected_not_clamped(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t frag =
        fragment(1u, 0u, 0u, (uint16_t)(HID_DESCRIPTOR_REASSEMBLY_CAPACITY + 1u), 0u);

    assert(hid_descriptor_reassembly_push(&reassembly, &frag) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_TOTAL);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_length(&reassembly) == 0u);
    assert(hid_descriptor_reassembly_data(&reassembly) == NULL);
}

static void test_capacity_sized_descriptor_is_accepted_in_full(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);

    for (size_t offset = 0u; offset < HID_DESCRIPTOR_REASSEMBLY_CAPACITY;
         offset += 18u) {
        const inj_descriptor_fragment_payload_t frag =
            fragment(1u, 0u, (uint16_t)offset,
                     (uint16_t)HID_DESCRIPTOR_REASSEMBLY_CAPACITY,
                     (uint8_t)offset);
        const hid_descriptor_fragment_result_t result =
            hid_descriptor_reassembly_push(&reassembly, &frag);
        if (offset + 18u < HID_DESCRIPTOR_REASSEMBLY_CAPACITY) {
            assert(result == HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
        } else {
            assert(result == HID_DESCRIPTOR_FRAGMENT_COMPLETE);
        }
    }

    assert(hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_length(&reassembly) ==
           HID_DESCRIPTOR_REASSEMBLY_CAPACITY);
    for (size_t i = 0u; i < HID_DESCRIPTOR_REASSEMBLY_CAPACITY; ++i) {
        assert(hid_descriptor_reassembly_data(&reassembly)[i] == (uint8_t)i);
    }
}

static void test_zero_total_and_offset_past_total_are_rejected(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t zero = fragment(1u, 0u, 0u, 0u, 0u);
    const inj_descriptor_fragment_payload_t past = fragment(1u, 0u, 21u, 20u, 0u);

    assert(hid_descriptor_reassembly_push(&reassembly, &zero) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_TOTAL);
    assert(hid_descriptor_reassembly_push(&reassembly, &past) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_RANGE);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
}

static void test_offset_plus_payload_cannot_wrap(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t frag = fragment(1u, 0u, 0xfff8u, 20u, 0u);

    assert(hid_descriptor_reassembly_push(&reassembly, &frag) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_RANGE);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
}

static void test_generation_change_to_invalid_total_still_discards_old_data(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t old = fragment(1u, 0u, 0u, 18u, 0u);
    const inj_descriptor_fragment_payload_t invalid =
        fragment(2u, 0u, 0u, (uint16_t)(HID_DESCRIPTOR_REASSEMBLY_CAPACITY + 1u), 0u);

    assert(hid_descriptor_reassembly_push(&reassembly, &old) ==
           HID_DESCRIPTOR_FRAGMENT_COMPLETE);
    assert(hid_descriptor_reassembly_push(&reassembly, &invalid) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_TOTAL);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_data(&reassembly) == NULL);
}

static void test_conflicting_duplicate_is_rejected_without_overwrite(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t first = fragment(1u, 0u, 0u, 36u, 0u);
    inj_descriptor_fragment_payload_t conflict = first;
    conflict.data[5] = 0xffu;

    assert(hid_descriptor_reassembly_push(&reassembly, &first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(hid_descriptor_reassembly_push(&reassembly, &conflict) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_CONFLICT);
    assert(hid_descriptor_reassembly_data(&reassembly)[5] == 5u);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
}

static void test_same_generation_total_change_discards_partial_data(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);
    const inj_descriptor_fragment_payload_t first = fragment(1u, 0u, 0u, 36u, 0u);
    const inj_descriptor_fragment_payload_t changed = fragment(1u, 0u, 18u, 35u, 18u);

    assert(hid_descriptor_reassembly_push(&reassembly, &first) ==
           HID_DESCRIPTOR_FRAGMENT_ACCEPTED);
    assert(hid_descriptor_reassembly_push(&reassembly, &changed) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_TOTAL);
    assert(!hid_descriptor_reassembly_complete(&reassembly));
    assert(hid_descriptor_reassembly_length(&reassembly) == 0u);
}

static void test_null_arguments_are_total_functions(void)
{
    hid_descriptor_reassembly_t reassembly;
    hid_descriptor_reassembly_init(&reassembly, 0u);

    assert(hid_descriptor_reassembly_push(NULL, NULL) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_ARGUMENT);
    assert(hid_descriptor_reassembly_push(&reassembly, NULL) ==
           HID_DESCRIPTOR_FRAGMENT_ERR_ARGUMENT);
    assert(!hid_descriptor_reassembly_complete(NULL));
    assert(hid_descriptor_reassembly_data(NULL) == NULL);
    assert(hid_descriptor_reassembly_length(NULL) == 0u);
    assert(hid_descriptor_reassembly_generation(NULL) == 0u);
    assert(hid_descriptor_reassembly_target_interface(NULL) == 0u);
    hid_descriptor_reassembly_init(NULL, 0u);
    hid_descriptor_reassembly_retarget(NULL, 0u);
}

int main(void)
{
    test_out_of_order_fragments_complete_without_splicing_gaps();
    test_gap_keeps_descriptor_incomplete();
    test_duplicate_fragment_does_not_fake_progress();
    test_interleaved_other_interface_is_ignored();
    test_generation_change_discards_partial_descriptor();
    test_retarget_discards_partial_descriptor();
    test_oversize_total_is_rejected_not_clamped();
    test_capacity_sized_descriptor_is_accepted_in_full();
    test_zero_total_and_offset_past_total_are_rejected();
    test_offset_plus_payload_cannot_wrap();
    test_generation_change_to_invalid_total_still_discards_old_data();
    test_conflicting_duplicate_is_rejected_without_overwrite();
    test_same_generation_total_change_discards_partial_data();
    test_null_arguments_are_total_functions();
    puts("hid_descriptor_reassembly_test: all passed");
    return 0;
}
