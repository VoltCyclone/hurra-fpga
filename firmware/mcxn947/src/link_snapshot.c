// Portable seqlock shared by both images. There is one algorithm here; only
// LINK_SNAPSHOT_BARRIER() differs because MCXN947 needs an architectural data
// memory barrier while the host test needs a compiler barrier.

#include "link_snapshot.h"

#include <stddef.h>

#if defined(MCXN947)
// CMSIS spells this __DMB(), but including the device/CMSIS header here would
// make a pointer-only module vendor-dependent. This is the same GCC expansion
// CMSIS supplies, kept local so the required barrier spelling remains clear.
#ifndef __DMB
#define __DMB() __asm__ volatile("dmb 0xF" ::: "memory")
#endif
#define LINK_SNAPSHOT_BARRIER() __DMB()
#else
#define LINK_SNAPSHOT_BARRIER() __atomic_signal_fence(__ATOMIC_SEQ_CST)
#endif

#if defined(LINK_SNAPSHOT_TESTING)
// Test-only instrumentation makes the promised "reader at every writer
// offset" interleaving deterministic. These calls compile out of both target
// images; the stores and barriers under test are otherwise the same code.
void link_snapshot_test_read_attempt(uint32_t attempt);
void link_snapshot_test_publish_offset(size_t offset,
                                       const volatile link_snapshot_t *src);
#define LINK_SNAPSHOT_READ_HOOK(attempt) link_snapshot_test_read_attempt(attempt)
#define LINK_SNAPSHOT_PUBLISH_HOOK(offset, src) \
    link_snapshot_test_publish_offset((offset), (src))
#else
#define LINK_SNAPSHOT_READ_HOOK(attempt) ((void)(attempt))
#define LINK_SNAPSHOT_PUBLISH_HOOK(offset, src) \
    ((void)(offset), (void)(src))
#endif

#define LINK_SNAPSHOT_STORE(dst, src, field)                 \
    do {                                                      \
        (dst)->field = (src)->field;                          \
        LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, field), (dst)); \
    } while (0)

void link_snapshot_publish(volatile link_snapshot_t *dst,
                           const link_snapshot_t *src)
{
    const uint32_t odd_sequence = dst->seq + 1u;
    const uint32_t even_sequence = odd_sequence + 1u;

    dst->seq = odd_sequence;
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, seq), dst);
    LINK_SNAPSHOT_BARRIER();

    LINK_SNAPSHOT_STORE(dst, src, slot_counter);
    LINK_SNAPSHOT_STORE(dst, src, native_report_count);
    LINK_SNAPSHOT_STORE(dst, src, usb_frame);
    LINK_SNAPSHOT_STORE(dst, src, usb_subframe);
    LINK_SNAPSHOT_STORE(dst, src, link_flags);
    LINK_SNAPSHOT_STORE(dst, src, descriptor_generation);
    LINK_SNAPSHOT_STORE(dst, src, map_generation);
    LINK_SNAPSHOT_STORE(dst, src, fault_flags);
    LINK_SNAPSHOT_STORE(dst, src, last_rx_sequence);
    dst->_pad[0] = src->_pad[0];
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, _pad) + 0u, dst);
    dst->_pad[1] = src->_pad[1];
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, _pad) + 1u, dst);
    dst->_pad[2] = src->_pad[2];
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, _pad) + 2u, dst);
    dst->_pad[3] = src->_pad[3];
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, _pad) + 3u, dst);
    dst->_pad[4] = src->_pad[4];
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, _pad) + 4u, dst);
    dst->_pad[5] = src->_pad[5];
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, _pad) + 5u, dst);
    dst->_pad[6] = src->_pad[6];
    LINK_SNAPSHOT_PUBLISH_HOOK(offsetof(link_snapshot_t, _pad) + 6u, dst);

    LINK_SNAPSHOT_BARRIER();
    dst->seq = even_sequence;
}

bool link_snapshot_read(const volatile link_snapshot_t *src,
                        link_snapshot_t *out)
{
    for (uint32_t attempt = 0u; attempt < LINK_SNAPSHOT_READ_ATTEMPTS; ++attempt) {
        LINK_SNAPSHOT_READ_HOOK(attempt);
        const uint32_t s0 = src->seq;
        if ((s0 & 1u) != 0u) {
            continue;
        }

        LINK_SNAPSHOT_BARRIER();
        link_snapshot_t candidate;
        candidate.slot_counter = src->slot_counter;
        candidate.native_report_count = src->native_report_count;
        candidate.usb_frame = src->usb_frame;
        candidate.usb_subframe = src->usb_subframe;
        candidate.link_flags = src->link_flags;
        candidate.descriptor_generation = src->descriptor_generation;
        candidate.map_generation = src->map_generation;
        candidate.fault_flags = src->fault_flags;
        candidate.last_rx_sequence = src->last_rx_sequence;
        for (uint32_t i = 0u; i < sizeof(candidate._pad); ++i) {
            candidate._pad[i] = src->_pad[i];
        }
        LINK_SNAPSHOT_BARRIER();

        const uint32_t s1 = src->seq;
        if (s0 == s1 && (s1 & 1u) == 0u) {
            candidate.seq = s1;
            *out = candidate;
            return true;
        }
    }

    return false;
}
