#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "link_snapshot.h"

static uint32_t g_read_attempts;
static uint32_t g_publish_hooks;
static size_t g_offsets[32];

void link_snapshot_test_read_attempt(uint32_t attempt)
{
    assert(attempt == g_read_attempts % LINK_SNAPSHOT_READ_ATTEMPTS);
    g_read_attempts++;
}

void link_snapshot_test_publish_offset(size_t offset,
                                       const volatile link_snapshot_t *src)
{
    link_snapshot_t unchanged = {
        .seq = 0x11111110u,
        .slot_counter = 0x22222222u,
        .native_report_count = 0x33333333u,
        .usb_frame = 0x4444u,
        .usb_subframe = 0x5555u,
        .link_flags = 0x6666u,
        .descriptor_generation = 0x7777u,
        .map_generation = 0x8888u,
        .fault_flags = 0x9999u,
        .last_rx_sequence = 0xAAu,
        ._pad = {0xB0u, 0xB1u, 0xB2u, 0xB3u, 0xB4u, 0xB5u, 0xB6u},
    };
    link_snapshot_t out = unchanged;

    assert(g_publish_hooks < (uint32_t)(sizeof(g_offsets) / sizeof(g_offsets[0])));
    g_offsets[g_publish_hooks] = offset;
    g_publish_hooks++;

    // Every hook runs while the writer's sequence is odd. A reader colliding
    // after any individual store must exhaust exactly four attempts and leave
    // the caller's last-good copy byte-for-byte untouched.
    const uint32_t attempts_before = g_read_attempts;
    assert(!link_snapshot_read(src, &out));
    assert(g_read_attempts - attempts_before == LINK_SNAPSHOT_READ_ATTEMPTS);
    assert(memcmp(&out, &unchanged, sizeof(out)) == 0);
}

static link_snapshot_t sample(uint32_t slots)
{
    return (link_snapshot_t){
        .seq = 0xDEADBEEFu,
        .slot_counter = slots,
        .native_report_count = 0x02030405u,
        .usb_frame = 0x0607u,
        .usb_subframe = 0x0809u,
        .link_flags = 0x0A0Bu,
        .descriptor_generation = 0x0C0Du,
        .map_generation = 0x0E0Fu,
        .fault_flags = 0x1011u,
        .last_rx_sequence = 0x12u,
        ._pad = {0x13u, 0x14u, 0x15u, 0x16u, 0x17u, 0x18u, 0x19u},
    };
}

static void test_stable_read_copies_one_complete_generation(void)
{
    volatile link_snapshot_t shared = sample(0x01020304u);
    shared.seq = 24u;
    link_snapshot_t out;

    g_read_attempts = 0u;
    assert(link_snapshot_read(&shared, &out));
    assert(g_read_attempts == 1u);
    assert(memcmp(&out, (const void *)&shared, sizeof(out)) == 0);
}

static void test_odd_sequence_is_bounded_and_preserves_last_good_copy(void)
{
    volatile link_snapshot_t shared = sample(0x11223344u);
    shared.seq = 7u;
    link_snapshot_t out = sample(0x55667788u);
    const link_snapshot_t before = out;

    g_read_attempts = 0u;
    assert(!link_snapshot_read(&shared, &out));
    assert(g_read_attempts == LINK_SNAPSHOT_READ_ATTEMPTS);
    assert(memcmp(&out, &before, sizeof(out)) == 0);
}

static void test_publish_interleaves_reader_after_every_store(void)
{
    volatile link_snapshot_t shared = sample(1u);
    shared.seq = 10u;
    const link_snapshot_t next = sample(0xA1B2C3D4u);
    const size_t expected_offsets[] = {
        offsetof(link_snapshot_t, seq),
        offsetof(link_snapshot_t, slot_counter),
        offsetof(link_snapshot_t, native_report_count),
        offsetof(link_snapshot_t, usb_frame),
        offsetof(link_snapshot_t, usb_subframe),
        offsetof(link_snapshot_t, link_flags),
        offsetof(link_snapshot_t, descriptor_generation),
        offsetof(link_snapshot_t, map_generation),
        offsetof(link_snapshot_t, fault_flags),
        offsetof(link_snapshot_t, last_rx_sequence),
        offsetof(link_snapshot_t, _pad) + 0u,
        offsetof(link_snapshot_t, _pad) + 1u,
        offsetof(link_snapshot_t, _pad) + 2u,
        offsetof(link_snapshot_t, _pad) + 3u,
        offsetof(link_snapshot_t, _pad) + 4u,
        offsetof(link_snapshot_t, _pad) + 5u,
        offsetof(link_snapshot_t, _pad) + 6u,
    };

    g_publish_hooks = 0u;
    g_read_attempts = 0u;
    link_snapshot_publish(&shared, &next);

    assert(g_publish_hooks ==
           (uint32_t)(sizeof(expected_offsets) / sizeof(expected_offsets[0])));
    assert(memcmp(g_offsets, expected_offsets, sizeof(expected_offsets)) == 0);
    assert(shared.seq == 12u);

    link_snapshot_t out;
    assert(link_snapshot_read(&shared, &out));
    assert(out.seq == 12u);
    assert(out.slot_counter == next.slot_counter);
    assert(out.native_report_count == next.native_report_count);
    assert(out.usb_frame == next.usb_frame);
    assert(out.usb_subframe == next.usb_subframe);
    assert(out.link_flags == next.link_flags);
    assert(out.descriptor_generation == next.descriptor_generation);
    assert(out.map_generation == next.map_generation);
    assert(out.fault_flags == next.fault_flags);
    assert(out.last_rx_sequence == next.last_rx_sequence);
    assert(memcmp(out._pad, next._pad, sizeof(out._pad)) == 0);
}

int main(void)
{
    assert(sizeof(link_snapshot_t) == 32u);
    test_stable_read_copies_one_complete_generation();
    test_odd_sequence_is_bounded_and_preserves_last_good_copy();
    test_publish_interleaves_reader_after_every_store();
    printf("link_snapshot_test: ok\n");
    return 0;
}
