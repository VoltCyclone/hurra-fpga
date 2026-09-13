#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "ch32_link.h"
#include "injection_wire.h"
#include "spi_frame.h"

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,            \
                    #condition);                                               \
            return 1;                                                          \
        }                                                                      \
    } while (0)

/* Re-CRC a slot edited in place so a hand-built frame is rejected by the rule
 * under test, not by a stale checksum. */
static void fix_crc(uint8_t slot[INJ_FRAME_SIZE])
{
    const uint16_t crc = spi_frame_crc16(slot, INJ_FRAME_SIZE - 2u);

    slot[INJ_FRAME_SIZE - 2u] = (uint8_t)crc;
    slot[INJ_FRAME_SIZE - 1u] = (uint8_t)(crc >> 8);
}

/* Build a deliverable RELATIVE slot with a 26-byte payload of `tag` bytes. */
static void build_relative(uint8_t slot[INJ_FRAME_SIZE], uint8_t sequence,
                           uint8_t tag)
{
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];

    memset(payload, tag, sizeof(payload));
    (void)spi_frame_pack(slot, INJ_TYPE_RELATIVE, sequence, payload,
                         INJ_FRAME_PAYLOAD_SIZE);
}

int main(void)
{
    uint8_t slot[INJ_FRAME_SIZE];
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    inj_frame_t frame;
    const ch32_link_counters_t *counters = ch32_link_counters();

    memset(payload, 0x5Au, sizeof(payload));

    /* --- Duplicate sequence executes zero times. --- */
    ch32_link_reset();
    build_relative(slot, 5u, 0x11u);
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_receive(&frame));
    CHECK(frame.type == INJ_TYPE_RELATIVE);
    CHECK(frame.sequence == 5u);
    CHECK(frame.length == INJ_FRAME_PAYLOAD_SIZE);
    CHECK(frame.payload[0] == 0x11u);
    CHECK(!ch32_link_receive(&frame));

    build_relative(slot, 5u, 0x22u);
    ch32_link_retire_slot(slot);
    CHECK(!ch32_link_receive(&frame));
    CHECK(counters->duplicate == 1u);
    CHECK(counters->slots == 2u);

    /* --- A forward gap is delivered and increments sequence_gap. --- */
    ch32_link_reset();
    build_relative(slot, 5u, 0x11u);
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_receive(&frame));
    build_relative(slot, 8u, 0x33u);
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_receive(&frame));
    CHECK(frame.sequence == 8u);
    CHECK(counters->sequence_gap == 1u);
    CHECK(counters->duplicate == 0u);

    /* --- A bad CRC never reaches ch32_link_receive. --- */
    ch32_link_reset();
    build_relative(slot, 5u, 0x11u);
    slot[4] ^= 0x01u; /* Corrupt a payload byte; CRC no longer matches. */
    ch32_link_retire_slot(slot);
    CHECK(!ch32_link_receive(&frame));
    CHECK(counters->bad_crc == 1u);
    CHECK(counters->slots == 1u);

    /* --- SOF and length faults land in their own counters. --- */
    ch32_link_reset();
    build_relative(slot, 5u, 0x11u);
    slot[0] ^= 0x01u; /* SOF checked first, before CRC. */
    ch32_link_retire_slot(slot);
    CHECK(!ch32_link_receive(&frame));
    CHECK(counters->bad_sof == 1u);

    build_relative(slot, 5u, 0x11u);
    slot[3] = (uint8_t)(INJ_FRAME_PAYLOAD_SIZE + 1u); /* Length before CRC. */
    ch32_link_retire_slot(slot);
    CHECK(!ch32_link_receive(&frame));
    CHECK(counters->bad_length == 1u);

    /* --- An unassigned type lands in bad_type, not bad_crc/bad_length. --- */
    ch32_link_reset();
    build_relative(slot, 1u, 0x11u);
    slot[1] = 0x7Fu; /* Not one of the 15 assigned types. */
    fix_crc(slot);
    ch32_link_retire_slot(slot);
    CHECK(!ch32_link_receive(&frame));
    CHECK(counters->bad_type == 1u);
    CHECK(counters->bad_crc == 0u);
    CHECK(counters->bad_length == 0u);

    /* --- A stale (backward) sequence is dropped and counted, and does not move
     *     the receive window backward. --- */
    ch32_link_reset();
    build_relative(slot, 5u, 0x11u);
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_receive(&frame));
    build_relative(slot, 3u, 0x44u); /* delta = (3 - 5) & 0xFF = 0xFE -> STALE. */
    ch32_link_retire_slot(slot);
    CHECK(!ch32_link_receive(&frame));
    CHECK(counters->stale == 1u);
    build_relative(slot, 6u, 0x55u); /* Window still at 5, so 6 is NEXT. */
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_receive(&frame));
    CHECK(frame.sequence == 6u);
    CHECK(counters->sequence_gap == 0u);

    /* --- An IDLE keepalive counts as a slot but is not sequenced or delivered,
     *     so it cannot desync the classifier. --- */
    ch32_link_reset();
    build_relative(slot, 5u, 0x11u);
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_receive(&frame));
    CHECK(spi_frame_pack(slot, INJ_TYPE_IDLE, 0u, NULL, 0u) == SPI_FRAME_OK);
    ch32_link_retire_slot(slot);
    CHECK(!ch32_link_receive(&frame));
    CHECK(counters->slots == 2u); /* One RELATIVE + one IDLE, both counted. */
    build_relative(slot, 6u, 0x66u); /* delta 1 despite the intervening IDLE. */
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_receive(&frame));
    CHECK(frame.sequence == 6u);
    CHECK(counters->sequence_gap == 0u);
    CHECK(counters->duplicate == 0u);
    CHECK(counters->stale == 0u);

    /* --- A full RX ring rejects without overwriting the buffered frames. --- */
    ch32_link_reset();
    for (uint8_t index = 0u; index < CH32_LINK_RING_MASK; ++index) {
        build_relative(slot, (uint8_t)(index + 1u), (uint8_t)(0xA0u + index));
        ch32_link_retire_slot(slot);
    }
    /* Ring holds CH32_LINK_RING_MASK (7) frames; the next deliverable is dropped. */
    build_relative(slot, (uint8_t)(CH32_LINK_RING_MASK + 1u), 0xBBu);
    ch32_link_retire_slot(slot);
    for (uint8_t index = 0u; index < CH32_LINK_RING_MASK; ++index) {
        CHECK(ch32_link_receive(&frame));
        CHECK(frame.sequence == (uint8_t)(index + 1u));
        CHECK(frame.payload[0] == (uint8_t)(0xA0u + index));
    }
    CHECK(!ch32_link_receive(&frame)); /* The overflow frame never appears. */

    /* --- A full TX ring rejects submit without overwriting. --- */
    ch32_link_reset();
    for (uint8_t index = 0u; index < CH32_LINK_RING_MASK; ++index) {
        CHECK(ch32_link_submit(INJ_TYPE_RELATIVE, payload,
                               INJ_FRAME_PAYLOAD_SIZE));
    }
    CHECK(!ch32_link_submit(INJ_TYPE_RELATIVE, payload, INJ_FRAME_PAYLOAD_SIZE));
    for (uint8_t index = 0u; index < CH32_LINK_RING_MASK; ++index) {
        uint8_t type = 0u;
        ch32_link_fill_tx_slot(slot);
        CHECK(spi_frame_unpack(slot, &type, NULL, NULL, NULL) == SPI_FRAME_OK);
        CHECK(type == INJ_TYPE_RELATIVE);
    }
    ch32_link_fill_tx_slot(slot); /* Queue drained -> IDLE keepalive. */
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_IDLE);

    /* --- submit rejects an unassigned type or a wrong length. --- */
    ch32_link_reset();
    CHECK(!ch32_link_submit(0x7Fu, payload, INJ_FRAME_PAYLOAD_SIZE));
    CHECK(!ch32_link_submit(INJ_TYPE_RELATIVE, payload, 20u));
    CHECK(ch32_link_submit(INJ_TYPE_IDLE, NULL, 0u));

    /* --- Clearing drops queued commands and returns an IDLE TX slot. --- */
    ch32_link_reset();
    CHECK(ch32_link_submit(INJ_TYPE_RELATIVE, payload, INJ_FRAME_PAYLOAD_SIZE));
    CHECK(ch32_link_submit(INJ_TYPE_RELATIVE, payload, INJ_FRAME_PAYLOAD_SIZE));
    build_relative(slot, 1u, 0x11u);
    ch32_link_retire_slot(slot); /* Also leave something on the RX ring. */
    ch32_link_clear_queues();
    ch32_link_fill_tx_slot(slot);
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_IDLE);
    CHECK(!ch32_link_receive(&frame)); /* RX queue dropped too. */

    /* --- Health follows slot progress within the timeout window. --- */
    ch32_link_reset();
    CHECK(ch32_link_healthy(0u)); /* Bootstrap grace on the first observation. */
    CHECK(!ch32_link_healthy(CH32_LINK_HEALTH_TIMEOUT_MS + 1u)); /* Stalled. */
    build_relative(slot, 1u, 0x11u);
    ch32_link_retire_slot(slot);
    CHECK(ch32_link_healthy(CH32_LINK_HEALTH_TIMEOUT_MS + 2u)); /* Progressed. */

    puts("ch32_link_logic_test: all passed");
    return 0;
}
