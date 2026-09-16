// RX retirement. No MMIO anywhere in this file -- it is built for the host by
// `make test-link-retire` with exactly the same source the target links.

#include "link_retire.h"

#include <stddef.h>
#include <string.h>

static inj_frame_t s_ring[LINK_RETIRE_RING_SIZE];
static uint8_t s_head;  // Producer: link_retire_slot() writes here.
static uint8_t s_tail;  // Consumer: link_retire_receive() reads here.

static uint8_t s_last_sequence;  // Last accepted RX sequence.
static bool s_have_window;       // False until the first deliverable frame.

static link_retire_counters_t s_counters;
static uint8_t s_last_slot[INJ_FRAME_SIZE];

static bool ring_full(void)
{
    return (uint8_t)((s_head + 1u) & LINK_RETIRE_RING_MASK) == s_tail;
}

bool link_retire_active_bank(uint32_t destination_address,
                             uint32_t base,
                             uint32_t stride,
                             uint8_t banks,
                             uint8_t *active)
{
    if (active == NULL || banks == 0u || stride == 0u) {
        return false;
    }
    if (destination_address < base) {
        return false;
    }

    const uint32_t span = (uint32_t)banks * stride;
    const uint32_t offset = destination_address - base;
    if (offset > span) {
        return false;
    }
    if (offset == span) {
        // One past the end of the last bank: the major loop finished and the
        // scatter-gather reload has not been observed yet. The ring reloads to
        // bank 0, so that is where the engine is about to write.
        *active = 0u;
        return true;
    }

    *active = (uint8_t)(offset / stride);
    return true;
}

void link_retire_reset(void)
{
    s_head = 0u;
    s_tail = 0u;
    s_last_sequence = 0u;
    s_have_window = false;
    memset(&s_counters, 0, sizeof(s_counters));
    memset(s_last_slot, 0, sizeof(s_last_slot));
}

void link_retire_slot(const uint8_t slot[INJ_FRAME_SIZE])
{
    uint8_t type = 0u;
    uint8_t sequence = 0u;
    uint8_t length = 0u;
    const uint8_t *payload = NULL;
    spi_frame_seq_class_t disposition;
    inj_frame_t frame;

    s_counters.slots++;
    memcpy(s_last_slot, slot, INJ_FRAME_SIZE);

    switch (spi_frame_unpack(slot, &type, &sequence, &payload, &length)) {
    case SPI_FRAME_ERR_SOF:
        s_counters.bad_sof++;
        return;
    case SPI_FRAME_ERR_LEN:
        s_counters.bad_length++;
        return;
    case SPI_FRAME_ERR_CRC:
        s_counters.bad_crc++;
        return;
    case SPI_FRAME_ERR_TYPE:
        s_counters.bad_type++;
        return;
    case SPI_FRAME_IDLE:
        // The whole of what this step receives. Well formed, counted, and not
        // sequenced: the FPGA hardwires an IDLE keepalive's sequence to 0.
        s_counters.idle++;
        return;
    case SPI_FRAME_OK:
        break;
    }

    // The first deliverable frame of a session is accepted unconditionally;
    // only once a window exists does the classifier get a say.
    if (!s_have_window) {
        disposition = SPI_FRAME_SEQ_NEXT;
    } else {
        disposition = spi_frame_seq_classify(s_last_sequence, sequence, NULL);
    }

    switch (disposition) {
    case SPI_FRAME_SEQ_DUPLICATE:
        s_counters.duplicate++;
        return;  // Drain, do not act, do not advance.
    case SPI_FRAME_SEQ_STALE:
        s_counters.stale++;
        return;  // Drain, never move the window backward.
    case SPI_FRAME_SEQ_GAP:
        s_counters.sequence_gap++;
        break;  // Act and advance; the gap is a diagnostic, not a rejection.
    case SPI_FRAME_SEQ_NEXT:
        break;  // Act and advance.
    }

    s_last_sequence = sequence;
    s_have_window = true;
    s_counters.deliverable++;

    frame.type = type;
    frame.sequence = sequence;
    frame.length = length;
    memcpy(frame.payload, payload, INJ_FRAME_PAYLOAD_SIZE);

    // A full ring drops this frame rather than overwrite an unread one. The
    // window has already advanced, so the loss is bounded to one frame and the
    // next in-order frame still classifies as NEXT.
    if (ring_full()) {
        s_counters.ring_drop++;
        return;
    }
    s_ring[s_head] = frame;
    s_head = (uint8_t)((s_head + 1u) & LINK_RETIRE_RING_MASK);
}

bool link_retire_receive(inj_frame_t *out)
{
    if (out == NULL || s_head == s_tail) {
        return false;
    }
    *out = s_ring[s_tail];
    s_tail = (uint8_t)((s_tail + 1u) & LINK_RETIRE_RING_MASK);
    return true;
}

const link_retire_counters_t *link_retire_counters(void)
{
    return &s_counters;
}

const uint8_t *link_retire_last_slot(void)
{
    return s_last_slot;
}

void link_retire_framing_sample(link_framing_sample_t *out)
{
    if (out == NULL) {
        return;
    }
    out->slots = s_counters.slots;
    out->good = s_counters.idle + s_counters.deliverable;
}

bool link_retire_framing_lost(const link_framing_sample_t *previous,
                              const link_framing_sample_t *current)
{
    if (previous == NULL || current == NULL) {
        return false;
    }

    // Unsigned differences, so a counter wrap is handled without a special
    // case. Counters are monotonic between resets, and a reset zeroes both
    // members of the next sample, so the only ordering hazard is the caller
    // passing them backwards -- which produces a huge slot delta and a huge
    // good delta together, and so still reports healthy.
    const uint32_t slots = current->slots - previous->slots;
    const uint32_t good = current->good - previous->good;

    return slots >= LINK_FRAMING_MIN_SLOTS && good == 0u;
}
