#include "spi_frame.h"

#include <stddef.h>

uint16_t spi_frame_crc16(const uint8_t *data, uint32_t length)
{
    uint16_t crc = INJ_CRC16_INIT;

    for (uint32_t index = 0u; index < length; ++index) {
        crc ^= (uint16_t)((uint16_t)data[index] << 8);
        for (uint8_t bit = 0u; bit < 8u; ++bit) {
            if ((crc & 0x8000u) != 0u) {
                const uint16_t shifted = (uint16_t)(crc << 1);
                crc = (uint16_t)(shifted ^ (uint16_t)INJ_CRC16_POLY);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
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
