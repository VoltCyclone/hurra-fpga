// Host test for src/link_retire.c -- step 3's "portable transport half".
//
// Two things are under test and they are independent:
//
//   1. link_retire_active_bank(), the derivation that keeps the CPU off the
//      bank the DMA owns. This is the one piece of new logic in step 3 and the
//      bench has already shown what getting it wrong looks like: reading a
//      live bank returns a torn slot.
//
//   2. link_retire_slot(), which is the CH32's proven retirement path ported
//      unchanged. Every bucket is exercised, because on the wire each one is a
//      different fault and folding two together would misattribute it.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "injection_wire.h"
#include "link_retire.h"
#include "spi_frame.h"

#define BASE 0x20001000u
#define STRIDE INJ_FRAME_SIZE
#define BANKS 2u

static uint8_t active_at(uint32_t address)
{
    uint8_t active = 0xEEu;
    assert(link_retire_active_bank(address, BASE, STRIDE, BANKS, &active));
    return active;
}

static void expect_rejected(uint32_t address)
{
    uint8_t active = 0xEEu;
    assert(!link_retire_active_bank(address, BASE, STRIDE, BANKS, &active));
    assert(active == 0xEEu);  // untouched on failure
}

static void test_active_bank(void)
{
    // Inside each bank, including both ends.
    assert(active_at(BASE) == 0u);
    assert(active_at(BASE + 4u) == 0u);
    assert(active_at(BASE + STRIDE - 1u) == 0u);
    assert(active_at(BASE + STRIDE) == 1u);
    assert(active_at(BASE + STRIDE + 4u) == 1u);
    assert(active_at(BASE + (2u * STRIDE) - 1u) == 1u);

    // The wrap. One past the end of the last bank is the window between the
    // final major-loop completion and the scatter-gather reload becoming
    // visible; the ring reloads to bank 0, so that is the answer. Reporting it
    // out of range instead would make the ISR retire nothing on every second
    // completion and the link would quietly drop half its slots.
    assert(active_at(BASE + (BANKS * STRIDE)) == 0u);

    // Out of range in both directions is a hard fault, not a guess.
    expect_rejected(BASE - 1u);
    expect_rejected(0u);
    expect_rejected(BASE + (BANKS * STRIDE) + 1u);

    // Degenerate arguments are refused rather than dividing by zero.
    uint8_t active = 0xEEu;
    assert(!link_retire_active_bank(BASE, BASE, STRIDE, 0u, &active));
    assert(!link_retire_active_bank(BASE, BASE, 0u, BANKS, &active));
    assert(!link_retire_active_bank(BASE, BASE, STRIDE, BANKS, NULL));

    // The rule section 4 states, restated as the property the ISR relies on:
    // whatever the address, the bank it reports is never one the caller may
    // read, and every other bank is.
    for (uint32_t offset = 0u; offset <= BANKS * STRIDE; ++offset) {
        uint8_t live = active_at(BASE + offset);
        assert(live < BANKS);
    }

    // A wider ring, so the arithmetic is not accidentally two-bank-specific.
    uint8_t wide = 0xEEu;
    assert(link_retire_active_bank(BASE + (3u * STRIDE) + 7u, BASE, STRIDE, 4u, &wide));
    assert(wide == 3u);
    assert(link_retire_active_bank(BASE + (4u * STRIDE), BASE, STRIDE, 4u, &wide));
    assert(wide == 0u);
}

static void build_idle(uint8_t slot[INJ_FRAME_SIZE])
{
    assert(spi_frame_pack(slot, INJ_TYPE_IDLE, 0u, NULL, 0u) == SPI_FRAME_OK);
}

static void build_deliverable(uint8_t slot[INJ_FRAME_SIZE], uint8_t sequence)
{
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    memset(payload, (int)sequence, sizeof(payload));
    assert(spi_frame_pack(slot, INJ_TYPE_MAP_COMMIT, sequence, payload,
                          INJ_FRAME_PAYLOAD_SIZE) == SPI_FRAME_OK);
}

static void test_idle_is_a_slot_but_never_a_frame(void)
{
    uint8_t slot[INJ_FRAME_SIZE];
    inj_frame_t frame;

    link_retire_reset();
    build_idle(slot);
    for (uint32_t n = 0u; n < 1000u; ++n) {
        link_retire_slot(slot);
    }

    const link_retire_counters_t *c = link_retire_counters();
    // This is exactly what the bench sees at this step, and the shape of it is
    // the acceptance criterion: slots advancing at the wire rate, every one of
    // them an IDLE, nothing delivered and no error bucket moving.
    assert(c->slots == 1000u);
    assert(c->idle == 1000u);
    assert(c->deliverable == 0u);
    assert(c->bad_sof == 0u && c->bad_crc == 0u && c->bad_length == 0u &&
           c->bad_type == 0u);
    // An IDLE keepalive carries a hardwired sequence 0. If it were fed to the
    // classifier, the second one and every one after it would read DUPLICATE
    // and a healthy link would look like a replay storm.
    assert(c->duplicate == 0u && c->stale == 0u && c->sequence_gap == 0u);
    assert(!link_retire_receive(&frame));

    // The last-slot snapshot is the on-target evidence that real wire bytes
    // reached this module: an untouched bank reads as zeros, and zeros are not
    // a valid IDLE frame.
    assert(memcmp(link_retire_last_slot(), slot, INJ_FRAME_SIZE) == 0);
}

static void test_every_malformed_bucket(void)
{
    uint8_t golden[INJ_FRAME_SIZE];
    uint8_t slot[INJ_FRAME_SIZE];

    build_idle(golden);

    link_retire_reset();

    memcpy(slot, golden, sizeof(slot));
    slot[0] = 0x69u;
    link_retire_slot(slot);
    assert(link_retire_counters()->bad_sof == 1u);

    memcpy(slot, golden, sizeof(slot));
    slot[1] = 0x7Fu;  // not one of the 15 assigned types
    link_retire_slot(slot);
    assert(link_retire_counters()->bad_type == 1u);

    memcpy(slot, golden, sizeof(slot));
    slot[3] = (uint8_t)INJ_FRAME_PAYLOAD_SIZE;  // IDLE ships length 0, exactly
    link_retire_slot(slot);
    assert(link_retire_counters()->bad_length == 1u);

    memcpy(slot, golden, sizeof(slot));
    slot[30] ^= 0xFFu;
    link_retire_slot(slot);
    assert(link_retire_counters()->bad_crc == 1u);

    // A torn read of a live bank is the fault this whole module is arranged to
    // prevent, and this is what it looks like when it happens anyway: a header
    // from the new slot over a body from the old one. It lands in bad_crc,
    // which is why bad_crc flat on the bench is the running proof that
    // link_retire_active_bank() is picking the right bank.
    memcpy(slot, golden, sizeof(slot));
    memset(&slot[12], 0xA5u, 8u);
    link_retire_slot(slot);
    assert(link_retire_counters()->bad_crc == 2u);

    const link_retire_counters_t *c = link_retire_counters();
    assert(c->slots == 5u);
    assert(c->idle == 0u && c->deliverable == 0u);
}

static void test_sequence_dispositions(void)
{
    uint8_t slot[INJ_FRAME_SIZE];
    inj_frame_t frame;

    link_retire_reset();

    // The first deliverable frame of a session is accepted whatever its
    // sequence -- there is no window yet to judge it against.
    build_deliverable(slot, 200u);
    link_retire_slot(slot);
    assert(link_retire_counters()->deliverable == 1u);
    assert(link_retire_receive(&frame));
    assert(frame.type == INJ_TYPE_MAP_COMMIT);
    assert(frame.sequence == 200u);
    assert(frame.length == INJ_FRAME_PAYLOAD_SIZE);
    assert(frame.payload[0] == 200u);

    // delta 1: act and advance.
    build_deliverable(slot, 201u);
    link_retire_slot(slot);
    assert(link_retire_counters()->deliverable == 2u);

    // delta 0: drain, do not act, do not advance.
    link_retire_slot(slot);
    assert(link_retire_counters()->duplicate == 1u);
    assert(link_retire_counters()->deliverable == 2u);

    // delta 5: act and advance, but say that four went missing.
    build_deliverable(slot, 206u);
    link_retire_slot(slot);
    assert(link_retire_counters()->sequence_gap == 1u);
    assert(link_retire_counters()->deliverable == 3u);

    // delta 0x80..0xFF: behind us. Drain; the window never moves backward.
    build_deliverable(slot, 100u);
    link_retire_slot(slot);
    assert(link_retire_counters()->stale == 1u);
    assert(link_retire_counters()->deliverable == 3u);

    // And the window really did stay at 206: the next in-order frame is NEXT.
    build_deliverable(slot, 207u);
    link_retire_slot(slot);
    assert(link_retire_counters()->deliverable == 4u);
    assert(link_retire_counters()->stale == 1u);

    // The exact classifier boundary, both sides of it.
    link_retire_reset();
    build_deliverable(slot, 0u);
    link_retire_slot(slot);
    build_deliverable(slot, 0x7Fu);  // delta 0x7F -- still forward
    link_retire_slot(slot);
    assert(link_retire_counters()->sequence_gap == 1u);
    link_retire_reset();
    build_deliverable(slot, 0u);
    link_retire_slot(slot);
    build_deliverable(slot, 0x80u);  // delta 0x80 -- stale
    link_retire_slot(slot);
    assert(link_retire_counters()->stale == 1u);
}

static void test_ring_drops_rather_than_overwrites(void)
{
    uint8_t slot[INJ_FRAME_SIZE];
    inj_frame_t frame;

    link_retire_reset();
    // LINK_RETIRE_RING_SIZE - 1 usable entries: head == tail means empty, so
    // the last slot is reserved to keep the two states apart.
    for (uint8_t n = 0u; n < (uint8_t)LINK_RETIRE_RING_SIZE + 4u; ++n) {
        build_deliverable(slot, (uint8_t)(n + 1u));
        link_retire_slot(slot);
    }

    const link_retire_counters_t *c = link_retire_counters();
    assert(c->deliverable == (uint32_t)LINK_RETIRE_RING_SIZE + 4u);
    assert(c->ring_drop == 5u);

    // The frames that survived are the OLDEST ones, in order -- a full ring
    // drops the newcomer rather than overwriting something unread.
    for (uint8_t n = 0u; n < (uint8_t)LINK_RETIRE_RING_MASK; ++n) {
        assert(link_retire_receive(&frame));
        assert(frame.sequence == (uint8_t)(n + 1u));
    }
    assert(!link_retire_receive(&frame));
}

static void test_reset_clears_everything(void)
{
    uint8_t slot[INJ_FRAME_SIZE];
    inj_frame_t frame;

    build_deliverable(slot, 9u);
    link_retire_slot(slot);
    link_retire_reset();

    const link_retire_counters_t *c = link_retire_counters();
    assert(c->slots == 0u && c->deliverable == 0u && c->idle == 0u);
    assert(!link_retire_receive(&frame));

    // The window is cleared too, so the next frame is a first frame again.
    build_deliverable(slot, 1u);
    link_retire_slot(slot);
    assert(link_retire_counters()->deliverable == 1u);
    assert(link_retire_counters()->stale == 0u);
}

// link_retire_slot() short-circuits an exact keepalive with a full-slot
// compare. This pins the reason it must stay a FULL compare.
//
// Corrupt a keepalive's body while leaving SOF, type and length intact: only
// the CRC can catch it, and it must land in bad_crc. Weaken the short-circuit
// to "the type byte says INJ_TYPE_IDLE, so skip the CRC" and this slot is
// counted as a healthy keepalive instead -- blinding bad_crc, the counter whose
// flatness on the bench is the running proof the link is clean, and which the
// ERR051588 recovery ladder reads to decide a slot was lost on the wire.
static void test_a_corrupted_keepalive_is_not_counted_as_idle(void)
{
    uint8_t slot[INJ_FRAME_SIZE];
    const link_retire_counters_t *c;

    link_retire_reset();
    build_idle(slot);
    slot[SPI_FRAME_OFF_PAYLOAD + 3u] ^= 0x01u;
    link_retire_slot(slot);

    c = link_retire_counters();
    assert(c->slots == 1u);
    assert(c->bad_crc == 1u);
    assert(c->idle == 0u);
    assert(c->deliverable == 0u);

    // ...and the pristine keepalive still takes the fast path, so the compare
    // is doing its job rather than simply never matching.
    link_retire_reset();
    build_idle(slot);
    link_retire_slot(slot);
    c = link_retire_counters();
    assert(c->idle == 1u && c->bad_crc == 0u);
}

static void test_framing_health(void)
{
    uint8_t idle[INJ_FRAME_SIZE];
    uint8_t broken[INJ_FRAME_SIZE];
    link_framing_sample_t before;
    link_framing_sample_t after;

    build_idle(idle);
    // A slot from a mis-framed slave: the same wire bytes, rotated. Nothing
    // parses, and nothing in any status register would say so.
    memcpy(broken, idle, sizeof(broken));
    broken[0] = 0x00u;
    broken[6] = 0x07u;

    // Healthy: keepalives parse, so the window is good however long it is.
    link_retire_reset();
    link_retire_framing_sample(&before);
    for (uint32_t n = 0u; n < 4u * LINK_FRAMING_MIN_SLOTS; ++n) {
        link_retire_slot(idle);
    }
    link_retire_framing_sample(&after);
    assert(!link_retire_framing_lost(&before, &after));

    // Mis-framed: slots advance, nothing parses.
    link_retire_reset();
    link_retire_framing_sample(&before);
    for (uint32_t n = 0u; n < 4u * LINK_FRAMING_MIN_SLOTS; ++n) {
        link_retire_slot(broken);
    }
    link_retire_framing_sample(&after);
    assert(link_retire_framing_lost(&before, &after));

    // One good slot in the window is enough to veto the verdict. The threshold
    // is "none parsed", not "most failed", because a framed link that is merely
    // noisy still lands the FPGA's constant keepalive between the bad slots --
    // and tearing down a working link over a burst of interference is worse
    // than the interference.
    link_retire_reset();
    link_retire_framing_sample(&before);
    for (uint32_t n = 0u; n < 4u * LINK_FRAMING_MIN_SLOTS; ++n) {
        link_retire_slot(broken);
    }
    link_retire_slot(idle);
    link_retire_framing_sample(&after);
    assert(!link_retire_framing_lost(&before, &after));

    // Too few slots to judge on. A window straddling a boot or a recovery must
    // not convict the link on a handful of slots.
    link_retire_reset();
    link_retire_framing_sample(&before);
    for (uint32_t n = 0u; n < LINK_FRAMING_MIN_SLOTS - 1u; ++n) {
        link_retire_slot(broken);
    }
    link_retire_framing_sample(&after);
    assert(!link_retire_framing_lost(&before, &after));
    // ...and exactly at the threshold it does convict.
    link_retire_slot(broken);
    link_retire_framing_sample(&after);
    assert(link_retire_framing_lost(&before, &after));

    // A stalled link -- no slots at all -- is not a framing fault. It is a
    // different failure with a different response, and this predicate must not
    // claim it.
    link_retire_framing_sample(&before);
    link_retire_framing_sample(&after);
    assert(!link_retire_framing_lost(&before, &after));

    // Deliverable frames count as "parsed" just as keepalives do: the question
    // is whether the byte stream is framed, not what it carries.
    link_retire_reset();
    link_retire_framing_sample(&before);
    for (uint32_t n = 0u; n < 4u * LINK_FRAMING_MIN_SLOTS; ++n) {
        link_retire_slot(broken);
    }
    build_deliverable(idle, 1u);
    link_retire_slot(idle);
    link_retire_framing_sample(&after);
    assert(!link_retire_framing_lost(&before, &after));

    assert(!link_retire_framing_lost(NULL, &after));
    assert(!link_retire_framing_lost(&before, NULL));
}

int main(void)
{
    test_active_bank();
    test_idle_is_a_slot_but_never_a_frame();
    test_a_corrupted_keepalive_is_not_counted_as_idle();
    test_every_malformed_bucket();
    test_sequence_dispositions();
    test_ring_drops_rather_than_overwrites();
    test_reset_clears_everything();
    test_framing_health();

    printf("link_retire_test: ok\n");
    return 0;
}
