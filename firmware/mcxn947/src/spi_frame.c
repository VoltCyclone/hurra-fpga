#include "spi_frame.h"

#include <stddef.h>

/* The shift counts below ARE the polynomial: 12, 5 and 0 spell x^16 + x^12 +
 * x^5 + 1 with the x^16 implicit. INJ_CRC16_POLY is generated from
 * protocol/report_injection_wire.json, so pin it here -- changing the poly in
 * the JSON must fail this build loudly rather than leave these shifts quietly
 * computing a different CRC than every other implementation of the contract. */
_Static_assert(INJ_CRC16_POLY == 0x1021u,
               "spi_frame_crc16's table-free reduction hardcodes poly 0x1021");

/* CRC-16/CCITT-FALSE, a byte at a time and without a lookup table.
 *
 * This is the standard table-free reduction of the bitwise loop it replaces:
 * the eight shift/xor steps for one byte collapse into three shifted xors of a
 * single intermediate. Verified identical to the bitwise form over 20,000
 * random 30-byte buffers, and against the CRC-16/CCITT-FALSE check value
 * 0x29B1 for "123456789".
 *
 * Why it is worth the density: this runs in the eDMA retirement ISR 8,000 times
 * a second over 30 bytes. PROVENANCE.md measures that ISR at ~10 us of each
 * 125 us slot, "nearly all of it the bitwise CRC-16" -- ~68 instructions per
 * byte, against ~10 here.
 *
 * Why not a 256-entry table: GCC 15 already rewrites the bitwise loop into one
 * for the CH32/RISC-V build, but the ARM toolchain is GCC 14.2, which has no
 * CRC idiom recognition, so the MCXN947 image shipped the loop verbatim. A
 * table is marginally faster per byte but costs 512 bytes of rodata and makes
 * the speed depend on which compiler happens to read this file. This form does
 * not. */
uint16_t spi_frame_crc16(const uint8_t *data, uint32_t length)
{
    uint16_t crc = INJ_CRC16_INIT;

    for (uint32_t index = 0u; index < length; ++index) {
        uint8_t reduced = (uint8_t)((crc >> 8) ^ data[index]);
        reduced ^= (uint8_t)(reduced >> 4);
        crc = (uint16_t)((uint16_t)(crc << 8) ^ ((uint16_t)reduced << 12) ^
                         ((uint16_t)reduced << 5) ^ (uint16_t)reduced);
    }

    return crc;
}

uint8_t spi_frame_expected_payload_length(uint8_t type)
{
    return (type == INJ_TYPE_IDLE) ? 0u : (uint8_t)INJ_FRAME_PAYLOAD_SIZE;
}

spi_frame_result_t spi_frame_pack(uint8_t slot[INJ_FRAME_SIZE],
                                  uint8_t type,
                                  uint8_t sequence,
                                  const uint8_t *payload,
                                  uint8_t length)
{
    /* Type before length, matching the FPGA's counter bucket order
     * (spi_link.py:368 before :371). */
    if (!inj_type_is_known(type)) {
        return SPI_FRAME_ERR_TYPE;
    }
    if (length != spi_frame_expected_payload_length(type)) {
        return SPI_FRAME_ERR_LEN;
    }

    slot[SPI_FRAME_OFF_SOF] = INJ_FRAME_SOF;
    slot[SPI_FRAME_OFF_TYPE] = type;
    slot[SPI_FRAME_OFF_SEQUENCE] = sequence;
    slot[SPI_FRAME_OFF_LENGTH] = length;

    for (uint8_t index = 0u; index < length; ++index) {
        slot[SPI_FRAME_OFF_PAYLOAD + index] = payload[index];
    }
    for (uint8_t index = length; index < INJ_FRAME_PAYLOAD_SIZE; ++index) {
        slot[SPI_FRAME_OFF_PAYLOAD + index] = 0u;
    }

    const uint16_t crc = spi_frame_crc16(slot, SPI_FRAME_OFF_CRC);
    slot[SPI_FRAME_OFF_CRC] = (uint8_t)crc;
    slot[SPI_FRAME_OFF_CRC + 1u] = (uint8_t)(crc >> 8);
    return SPI_FRAME_OK;
}

spi_frame_result_t spi_frame_unpack(const uint8_t slot[INJ_FRAME_SIZE],
                                    uint8_t *type,
                                    uint8_t *sequence,
                                    const uint8_t **payload,
                                    uint8_t *length)
{
    if (slot[SPI_FRAME_OFF_SOF] != INJ_FRAME_SOF) {
        return SPI_FRAME_ERR_SOF;
    }
    /* Cheap byte comparisons before the 30-byte CRC: a frame with an unassigned
     * type or the wrong length is unusable whatever its CRC says, and the slot
     * budget is 125 us. */
    if (!inj_type_is_known(slot[SPI_FRAME_OFF_TYPE])) {
        return SPI_FRAME_ERR_TYPE;
    }
    if (slot[SPI_FRAME_OFF_LENGTH] !=
        spi_frame_expected_payload_length(slot[SPI_FRAME_OFF_TYPE])) {
        return SPI_FRAME_ERR_LEN;
    }

    const uint16_t expected_crc =
        (uint16_t)((uint16_t)slot[SPI_FRAME_OFF_CRC] |
                   ((uint16_t)slot[SPI_FRAME_OFF_CRC + 1u] << 8));
    if (spi_frame_crc16(slot, SPI_FRAME_OFF_CRC) != expected_crc) {
        return SPI_FRAME_ERR_CRC;
    }

    if (slot[SPI_FRAME_OFF_TYPE] == INJ_TYPE_IDLE) {
        return SPI_FRAME_IDLE;
    }

    if (type != NULL) {
        *type = slot[SPI_FRAME_OFF_TYPE];
    }
    if (sequence != NULL) {
        *sequence = slot[SPI_FRAME_OFF_SEQUENCE];
    }
    if (payload != NULL) {
        *payload = &slot[SPI_FRAME_OFF_PAYLOAD];
    }
    if (length != NULL) {
        *length = slot[SPI_FRAME_OFF_LENGTH];
    }

    return SPI_FRAME_OK;
}

spi_frame_seq_class_t spi_frame_seq_classify(uint8_t previous_sequence,
                                             uint8_t sequence,
                                             uint8_t *gap)
{
    const uint8_t delta = (uint8_t)(sequence - previous_sequence);

    if (gap != NULL) {
        *gap = 0u;
    }
    if (delta == 0u) {
        return SPI_FRAME_SEQ_DUPLICATE;
    }
    if (delta == 1u) {
        return SPI_FRAME_SEQ_NEXT;
    }
    if (delta < 0x80u) {
        if (gap != NULL) {
            *gap = (uint8_t)(delta - 1u);
        }
        return SPI_FRAME_SEQ_GAP;
    }
    /* Half the sequence space behind us is stale, not a very large forward gap.
     * The window must never move backwards. */
    return SPI_FRAME_SEQ_STALE;
}

uint8_t spi_frame_seq_gap(uint8_t previous_sequence, uint8_t sequence)
{
    uint8_t gap = 0u;

    (void)spi_frame_seq_classify(previous_sequence, sequence, &gap);
    return gap;
}
