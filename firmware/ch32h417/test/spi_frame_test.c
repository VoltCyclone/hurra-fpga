#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "injection_wire.h"
#include "spi_frame.h"

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,           \
                    #condition);                                               \
            return 1;                                                          \
        }                                                                      \
    } while (0)

/* Re-CRC a slot edited in place, so that a hand-built malformed frame is
 * rejected by the rule under test rather than by its stale checksum. */
static void fix_crc(uint8_t slot[INJ_FRAME_SIZE])
{
    const uint16_t crc = spi_frame_crc16(slot, INJ_FRAME_SIZE - 2u);

    slot[INJ_FRAME_SIZE - 2u] = (uint8_t)crc;
    slot[INJ_FRAME_SIZE - 1u] = (uint8_t)(crc >> 8);
}

int main(void)
{
    static const uint8_t canonical_idle_golden[INJ_FRAME_SIZE] = {
        [0] = 0x68u,
        [30] = 0xFEu,
        [31] = 0x36u,
    };
    static const uint8_t idle_golden[INJ_FRAME_SIZE] = {
        [0] = 0x68u,
        [1] = 0x00u,
        [2] = 0x2Au,
        [3] = 0x00u,
        [30] = 0x53u,
        [31] = 0x99u,
    };
    uint8_t slot[INJ_FRAME_SIZE];

    CHECK(SPI_FRAME_IDLE != SPI_FRAME_OK);
    CHECK(spi_frame_pack(slot, INJ_TYPE_IDLE, 0u, NULL, 0u) == SPI_FRAME_OK);
    CHECK(memcmp(slot, canonical_idle_golden, sizeof(canonical_idle_golden)) ==
          0);

    uint8_t type = 0xAAu;
    uint8_t sequence = 0xAAu;
    uint8_t length = 0xAAu;
    const uint8_t *payload = slot;
    CHECK(spi_frame_unpack(slot, &type, &sequence, &payload, &length) ==
          SPI_FRAME_IDLE);
    CHECK(type == 0xAAu);
    CHECK(sequence == 0xAAu);
    CHECK(length == 0xAAu);
    CHECK(payload == slot);

    /* IDLE is identified by type, not by its normally hardwired sequence 0. */
    CHECK(spi_frame_pack(slot, INJ_TYPE_IDLE, 0x2Au, NULL, 0u) ==
          SPI_FRAME_OK);
    CHECK(memcmp(slot, idle_golden, sizeof(idle_golden)) == 0);
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_IDLE);

    static const uint8_t relative_payload[INJ_FRAME_PAYLOAD_SIZE] = {
        0x22u, 0x11u, 0x44u, 0x33u, 0x66u, 0x55u, 0x88u, 0x77u, 0x09u,
        0x0Au, 0x0Bu, 0x0Fu, 0xFEu, 0xFFu, 0x34u, 0x12u, 0xCCu, 0xEDu,
        0xFFu, 0x7Fu, 0xBCu, 0x9Au, 0x00u, 0x00u, 0x00u, 0x00u,
    };
    CHECK(spi_frame_pack(slot, INJ_TYPE_RELATIVE, 0x7Du, relative_payload,
                         sizeof(relative_payload)) == SPI_FRAME_OK);

    type = 0u;
    sequence = 0u;
    length = 0u;
    payload = NULL;
    CHECK(spi_frame_unpack(slot, &type, &sequence, &payload, &length) ==
          SPI_FRAME_OK);
    CHECK(type == INJ_TYPE_RELATIVE);
    CHECK(sequence == 0x7Du);
    CHECK(length == INJ_FRAME_PAYLOAD_SIZE);
    CHECK(memcmp(payload, relative_payload, sizeof(relative_payload)) == 0);

    slot[0] ^= 0x01u;
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_ERR_SOF);
    slot[0] ^= 0x01u;

    slot[3] = (uint8_t)(INJ_FRAME_PAYLOAD_SIZE + 1u);
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_ERR_LEN);
    slot[3] = INJ_FRAME_PAYLOAD_SIZE;

    slot[4] ^= 0x01u;
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_ERR_CRC);

    CHECK(spi_frame_seq_gap(10u, 10u) == 0u);
    CHECK(spi_frame_seq_gap(10u, 11u) == 0u);
    CHECK(spi_frame_seq_gap(10u, 14u) == 3u);
    CHECK(spi_frame_seq_gap(255u, 0u) == 0u);
    CHECK(spi_frame_seq_gap(254u, 1u) == 2u);

    /* The named regression: every delta in 0x80..0xFF is stale, and a stale
     * frame reports no gap. The old helper returned delta - 1 here, inflating
     * a replay by up to 254. All five goldens above sit in the agreeing region,
     * which is exactly why they never caught it. */
    CHECK(spi_frame_seq_gap(10u, 200u) == 0u);
    CHECK(spi_frame_seq_classify(10u, 200u, NULL) == SPI_FRAME_SEQ_STALE);

    /* Boundaries: first stale delta, last stale delta, and the forward wrap. */
    CHECK(spi_frame_seq_classify(0u, 128u, NULL) == SPI_FRAME_SEQ_STALE);
    CHECK(spi_frame_seq_classify(4u, 3u, NULL) == SPI_FRAME_SEQ_STALE);
    CHECK(spi_frame_seq_classify(0u, 127u, NULL) == SPI_FRAME_SEQ_GAP);
    CHECK(spi_frame_seq_classify(0xFFu, 0x00u, NULL) == SPI_FRAME_SEQ_NEXT);

    /* Exhaustive over every delta, from several starting points: the rule is a
     * pure function of delta, so the result must not depend on `previous`. */
    static const uint8_t previous_cases[] = {0u, 1u, 127u, 128u, 254u, 255u};
    for (size_t index = 0u; index < sizeof(previous_cases); ++index) {
        const uint8_t previous = previous_cases[index];
        for (unsigned delta = 0u; delta < 256u; ++delta) {
            const uint8_t received = (uint8_t)((unsigned)previous + delta);
            uint8_t gap = 0xAAu;
            const spi_frame_seq_class_t class_ =
                spi_frame_seq_classify(previous, received, &gap);

            if (delta == 0u) {
                CHECK(class_ == SPI_FRAME_SEQ_DUPLICATE);
                CHECK(gap == 0u);
            } else if (delta == 1u) {
                CHECK(class_ == SPI_FRAME_SEQ_NEXT);
                CHECK(gap == 0u);
            } else if (delta < 0x80u) {
                CHECK(class_ == SPI_FRAME_SEQ_GAP);
                CHECK(gap == (uint8_t)(delta - 1u));
            } else {
                CHECK(class_ == SPI_FRAME_SEQ_STALE);
                CHECK(gap == 0u);
            }

            /* The wrapper must agree with the classifier for every pair. */
            CHECK(spi_frame_seq_gap(previous, received) == gap);
            /* A NULL gap pointer must not be dereferenced. */
            CHECK(spi_frame_seq_classify(previous, received, NULL) == class_);
        }
    }

    /* Length is per-type EXACT, not a maximum. A short RELATIVE packs a
     * correctly-CRC'd frame the FPGA drops while incrementing bad_length_count,
     * a counter the MCU cannot read: the injection is lost with no local error,
     * and the transport sequence still advanced, so the next good frame arrives
     * as a GAP. pack() is the side that has to fail, and fail locally. */
    CHECK(spi_frame_pack(slot, INJ_TYPE_RELATIVE, 1u, relative_payload, 20u) ==
          SPI_FRAME_ERR_LEN);
    CHECK(spi_frame_pack(slot, INJ_TYPE_IDLE, 3u, relative_payload, 26u) ==
          SPI_FRAME_ERR_LEN);
    CHECK(spi_frame_pack(slot, INJ_TYPE_RELATIVE, 4u, relative_payload, 0u) ==
          SPI_FRAME_ERR_LEN);
    CHECK(spi_frame_pack(slot, 0x7Fu, 2u, relative_payload, 26u) ==
          SPI_FRAME_ERR_TYPE);

    static const uint8_t known_types[] = {
        INJ_TYPE_IDLE,       INJ_TYPE_LINK_STATUS,   INJ_TYPE_DESCRIPTOR_FRAGMENT,
        INJ_TYPE_REPORT_FRAGMENT, INJ_TYPE_MAP_STATUS, INJ_TYPE_COMMAND_ACK,
        INJ_TYPE_COUNTERS,   INJ_TYPE_MAP_BEGIN,     INJ_TYPE_MAP_ENTRY,
        INJ_TYPE_MAP_COMMIT, INJ_TYPE_RELATIVE,      INJ_TYPE_BUTTON_STATE,
        INJ_TYPE_PHYSICAL_MASK, INJ_TYPE_CLEAR,      INJ_TYPE_TELEMETRY_CONFIG,
    };
    CHECK(sizeof(known_types) == 15u);
    for (size_t known = 0u; known < sizeof(known_types); ++known) {
        const uint8_t candidate = known_types[known];

        CHECK(inj_type_is_known(candidate));
        CHECK(spi_frame_expected_payload_length(candidate) ==
              (candidate == INJ_TYPE_IDLE ? 0u : INJ_FRAME_PAYLOAD_SIZE));
    }

    /* Every byte value not in the contract's set must be unknown -- 241 of the
     * 256, checked by sweeping all of them and excluding the 15 above. */
    unsigned unassigned = 0u;
    for (unsigned candidate = 0u; candidate < 256u; ++candidate) {
        int assigned = 0;

        for (size_t known = 0u; known < sizeof(known_types); ++known) {
            if (known_types[known] == (uint8_t)candidate) {
                assigned = 1;
            }
        }
        if (!assigned) {
            CHECK(!inj_type_is_known((uint8_t)candidate));
            ++unassigned;
        }
    }
    CHECK(unassigned == 241u);

    /* The unpack side of the same two rules. Each slot carries a correct CRC,
     * so only the rule under test can reject it. */
    CHECK(spi_frame_pack(slot, INJ_TYPE_RELATIVE, 5u, relative_payload,
                         INJ_FRAME_PAYLOAD_SIZE) == SPI_FRAME_OK);
    slot[1] = 0x7Fu;
    fix_crc(slot);
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_ERR_TYPE);

    slot[1] = INJ_TYPE_IDLE;
    fix_crc(slot);
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_ERR_LEN);

    slot[1] = INJ_TYPE_RELATIVE;
    slot[3] = 20u;
    fix_crc(slot);
    CHECK(spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_ERR_LEN);

    puts("spi_frame_test: all passed");
    return 0;
}
