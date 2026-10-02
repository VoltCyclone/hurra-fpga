// Host test for src/hid_descriptor_set.c -- descriptor slots keyed by interface number.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_descriptor_set.h"

#define FRAGMENT_BYTES 18u

static inj_descriptor_fragment_payload_t fragment(uint16_t generation, uint8_t interface_number,
                                                  const uint8_t *descriptor, uint16_t total,
                                                  uint16_t offset)
{
    inj_descriptor_fragment_payload_t f;
    memset(&f, 0, sizeof(f));
    f.descriptor_generation = generation;
    f.interface_number = interface_number;
    f.offset = offset;
    f.total = total;
    const uint16_t remaining = (uint16_t)(total - offset);
    memcpy(f.data, &descriptor[offset], remaining < FRAGMENT_BYTES ? remaining : FRAGMENT_BYTES);
    return f;
}

static const uint8_t DESCRIPTOR_A[30] = {
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE,
    0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xBB, 0xBC, 0xBD,
};
static const uint8_t DESCRIPTOR_B[20] = {
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19,
    0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23,
};

static hid_descriptor_set_push_t push(hid_descriptor_set_t *set, uint16_t generation,
                                      uint8_t interface_number, const uint8_t *d, uint16_t total,
                                      uint16_t offset)
{
    const inj_descriptor_fragment_payload_t f = fragment(generation, interface_number, d, total,
                                                         offset);
    return hid_descriptor_set_push(set, &f);
}

// Two interfaces, fragments interleaved and out of order: each completes on
// its own, each exactly once, and each lands in its own slot -- claimed in
// arrival order, tagged with its interface number.
static void test_interfaces_complete_independently(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);

    hid_descriptor_set_push_t r = push(&set, 1u, 3u, DESCRIPTOR_A, 30u, 18u);
    assert(!r.newly_complete && r.slot == 0u);
    r = push(&set, 1u, 0u, DESCRIPTOR_B, 20u, 18u);
    assert(!r.newly_complete && r.slot == 1u);
    r = push(&set, 1u, 3u, DESCRIPTOR_A, 30u, 0u);
    assert(r.newly_complete && r.result == HID_DESCRIPTOR_FRAGMENT_COMPLETE && r.slot == 0u);
    assert(set.dirty_mask == 1u);
    r = push(&set, 1u, 0u, DESCRIPTOR_B, 20u, 0u);
    assert(r.newly_complete && r.slot == 1u);
    assert(set.dirty_mask == 3u);

    hid_descriptor_snapshot_t snap;
    assert(hid_descriptor_set_snapshot(&set, 0u, &snap));
    assert(snap.length == 30u && memcmp(snap.bytes, DESCRIPTOR_A, 30u) == 0);
    assert(snap.slot == 0u && snap.interface_number == 3u && snap.generation == 1u);
    assert(hid_descriptor_set_snapshot(&set, 1u, &snap));
    assert(snap.length == 20u && memcmp(snap.bytes, DESCRIPTOR_B, 20u) == 0);
    assert(snap.slot == 1u && snap.interface_number == 0u);
    assert(!hid_descriptor_set_snapshot(&set, 2u, &snap));  // never claimed
}

// A legal four-interface device need not number them 0..3.
static void test_any_four_interface_numbers_fit(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    static const uint8_t NUMBERS[4] = {0u, 1u, 2u, 5u};
    for (uint8_t i = 0u; i < 4u; ++i) {
        (void)push(&set, 1u, NUMBERS[i], DESCRIPTOR_B, 20u, 0u);
        const hid_descriptor_set_push_t r = push(&set, 1u, NUMBERS[i], DESCRIPTOR_B, 20u, 18u);
        assert(r.newly_complete && r.slot == i);
    }
    assert(hid_descriptor_set_complete_mask(&set) == 0x0Fu);
    for (uint8_t i = 0u; i < 4u; ++i) {
        hid_descriptor_snapshot_t snap;
        assert(hid_descriptor_set_snapshot(&set, i, &snap));
        assert(snap.interface_number == NUMBERS[i]);
    }
}

// Numbers are tags, not indices: 7 alone, and 255, which is not the no-slot
// sentinel even though both are 0xFF.
static void test_high_interface_numbers_claim_a_slot(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    hid_descriptor_set_push_t r = push(&set, 1u, 7u, DESCRIPTOR_B, 20u, 0u);
    assert(r.slot == 0u && r.result != HID_DESCRIPTOR_FRAGMENT_IGNORED_INTERFACE);
    r = push(&set, 1u, 255u, DESCRIPTOR_B, 20u, 0u);
    assert(r.slot == 1u && r.result != HID_DESCRIPTOR_FRAGMENT_IGNORED_INTERFACE);
    r = push(&set, 1u, 255u, DESCRIPTOR_B, 20u, 18u);
    assert(r.newly_complete && r.slot == 1u);
    hid_descriptor_snapshot_t snap;
    assert(hid_descriptor_set_snapshot(&set, 1u, &snap) && snap.interface_number == 255u);
}

// The FPGA exports at most four interfaces, so a fifth number is a stale or
// rogue stream: ignored, and it disturbs none of the four.
static void test_a_fifth_interface_number_is_ignored(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    for (uint8_t i = 0u; i < 4u; ++i) {
        (void)push(&set, 1u, (uint8_t)(10u + i), DESCRIPTOR_B, 20u, 0u);
        (void)push(&set, 1u, (uint8_t)(10u + i), DESCRIPTOR_B, 20u, 18u);
    }
    const hid_descriptor_set_push_t r = push(&set, 1u, 20u, DESCRIPTOR_A, 30u, 0u);
    assert(r.result == HID_DESCRIPTOR_FRAGMENT_IGNORED_INTERFACE && !r.newly_complete);
    assert(r.slot == HID_DESCRIPTOR_SET_NO_SLOT);
    assert(hid_descriptor_set_complete_mask(&set) == 0x0Fu);
}

// A re-plugged device may bring different numbers: every slot is free again,
// claimed afresh in arrival order, and a snapshot from before is void even
// where a slot index is reused for another number.
static void test_a_new_generation_reclaims_slots(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    static const uint8_t OLD[4] = {0u, 1u, 2u, 5u};
    for (uint8_t i = 0u; i < 4u; ++i) {
        (void)push(&set, 1u, OLD[i], DESCRIPTOR_B, 20u, 0u);
        (void)push(&set, 1u, OLD[i], DESCRIPTOR_B, 20u, 18u);
    }
    hid_descriptor_snapshot_t old;
    assert(hid_descriptor_set_snapshot(&set, 0u, &old) && old.interface_number == 0u);

    hid_descriptor_set_push_t r = push(&set, 2u, 5u, DESCRIPTOR_B, 20u, 0u);
    assert(r.generation_changed && r.slot == 0u);
    r = push(&set, 2u, 5u, DESCRIPTOR_B, 20u, 18u);
    assert(r.newly_complete);
    r = push(&set, 2u, 7u, DESCRIPTOR_A, 30u, 0u);
    assert(r.slot == 1u);
    assert(hid_descriptor_set_complete_mask(&set) == 1u);
    assert(!hid_descriptor_set_snapshot_valid(&set, &old));
    hid_descriptor_snapshot_t now;
    assert(hid_descriptor_set_snapshot(&set, 0u, &now) && now.interface_number == 5u);
}

// The FPGA re-exports every descriptor on each link-up with the SAME
// generation. That must not look like a new completion, or every link bounce
// would recompile and re-offer.
static void test_duplicate_export_is_not_a_new_completion(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    (void)push(&set, 1u, 0u, DESCRIPTOR_B, 20u, 0u);
    assert(push(&set, 1u, 0u, DESCRIPTOR_B, 20u, 18u).newly_complete);
    uint8_t slot = 0xFFu;
    assert(hid_descriptor_set_take_dirty(&set, &slot) && slot == 0u);

    hid_descriptor_set_push_t r = push(&set, 1u, 0u, DESCRIPTOR_B, 20u, 0u);
    assert(r.result == HID_DESCRIPTOR_FRAGMENT_COMPLETE && !r.newly_complete);
    r = push(&set, 1u, 0u, DESCRIPTOR_B, 20u, 18u);
    assert(!r.newly_complete);
    assert(set.dirty_mask == 0u);
}

// A new device must not inherit the old one's interfaces: interface 2 was
// complete under generation 1 and has no business being complete under 2.
static void test_new_generation_discards_every_slot(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    (void)push(&set, 1u, 2u, DESCRIPTOR_B, 20u, 0u);
    (void)push(&set, 1u, 2u, DESCRIPTOR_B, 20u, 18u);
    hid_descriptor_snapshot_t snap;
    assert(hid_descriptor_set_snapshot(&set, 0u, &snap));

    const hid_descriptor_set_push_t r = push(&set, 2u, 0u, DESCRIPTOR_A, 30u, 0u);
    assert(r.generation_changed);
    assert(!hid_descriptor_set_snapshot(&set, 0u, &snap));  // slot 0 is interface 0's now
    assert(set.dirty_mask == 0u);  // a pending dirty bit of the old device is void too
    assert(!push(&set, 2u, 0u, DESCRIPTOR_A, 30u, 18u).generation_changed);
}

static void test_take_dirty_returns_the_lowest_and_clears_it(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    set.dirty_mask = (uint8_t)((1u << 1u) | (1u << 3u));
    uint8_t slot = 0xFFu;
    assert(hid_descriptor_set_take_dirty(&set, &slot) && slot == 1u);
    assert(hid_descriptor_set_take_dirty(&set, &slot) && slot == 3u);
    assert(!hid_descriptor_set_take_dirty(&set, &slot));
}

// The foreground reads a snapshot's bytes unmasked. Anything that could have
// changed them -- a discard, a new generation -- must invalidate it; a harmless
// duplicate or a rejected conflicting fragment must not.
static void test_snapshot_validity(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    (void)push(&set, 1u, 0u, DESCRIPTOR_A, 30u, 0u);
    (void)push(&set, 1u, 0u, DESCRIPTOR_A, 30u, 18u);
    hid_descriptor_snapshot_t snap;
    assert(hid_descriptor_set_snapshot(&set, 0u, &snap));
    assert(hid_descriptor_set_snapshot_valid(&set, &snap));

    (void)push(&set, 1u, 0u, DESCRIPTOR_A, 30u, 0u);  // duplicate
    assert(hid_descriptor_set_snapshot_valid(&set, &snap));

    uint8_t conflicting[30];
    memcpy(conflicting, DESCRIPTOR_A, sizeof(conflicting));
    conflicting[2] ^= 0xFFu;
    assert(push(&set, 1u, 0u, conflicting, 30u, 0u).result ==
           HID_DESCRIPTOR_FRAGMENT_ERR_CONFLICT);
    assert(hid_descriptor_set_snapshot_valid(&set, &snap));

    (void)push(&set, 1u, 0u, DESCRIPTOR_A, 29u, 0u);  // total changed -> discard
    assert(!hid_descriptor_set_snapshot_valid(&set, &snap));

    // Re-completing the same bytes after a discard is a NEW revision: the
    // snapshot taken before the discard stays invalid.
    (void)push(&set, 1u, 0u, DESCRIPTOR_A, 30u, 0u);
    (void)push(&set, 1u, 0u, DESCRIPTOR_A, 30u, 18u);
    assert(!hid_descriptor_set_snapshot_valid(&set, &snap));
}

// What the set actually holds, for link.c to reconcile the session against,
// and a way to ask again for any complete slot the session never judged.
static void test_complete_mask_and_redirty(void)
{
    static hid_descriptor_set_t set;
    hid_descriptor_set_init(&set);
    assert(hid_descriptor_set_complete_mask(&set) == 0u);
    (void)push(&set, 1u, 2u, DESCRIPTOR_B, 20u, 0u);  // slot 0
    (void)push(&set, 1u, 2u, DESCRIPTOR_B, 20u, 18u);
    (void)push(&set, 1u, 0u, DESCRIPTOR_A, 30u, 0u);  // slot 1, partial
    assert(hid_descriptor_set_complete_mask(&set) == 1u);

    uint8_t slot = 0xFFu;
    assert(hid_descriptor_set_take_dirty(&set, &slot) && slot == 0u);
    assert(!hid_descriptor_set_take_dirty(&set, &slot));
    // Only complete slots can be re-marked: re-asking for a partial one would
    // just fail its snapshot every pass.
    hid_descriptor_set_redirty(&set, 0x0Fu);
    assert(set.dirty_mask == 1u);
}

int main(void)
{
    test_interfaces_complete_independently();
    test_any_four_interface_numbers_fit();
    test_high_interface_numbers_claim_a_slot();
    test_a_fifth_interface_number_is_ignored();
    test_a_new_generation_reclaims_slots();
    test_duplicate_export_is_not_a_new_completion();
    test_new_generation_discards_every_slot();
    test_take_dirty_returns_the_lowest_and_clears_it();
    test_snapshot_validity();
    test_complete_mask_and_redirty();
    printf("hid_descriptor_set_test: ok\n");
    return 0;
}
