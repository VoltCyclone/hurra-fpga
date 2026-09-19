// See inj_command.h. Pure, MMIO-free; host-compiled by inj_command_test.c.

#include "inj_command.h"

#include <string.h>

uint32_t inj_crc32(const uint8_t *data, uint32_t length)
{
    uint32_t crc = INJ_CRC32_INIT;

    for (uint32_t index = 0u; index < length; ++index) {
        crc ^= (uint32_t)data[index];
        for (uint8_t bit = 0u; bit < 8u; ++bit) {
            // Branchless reflected reduction: -(crc & 1) is all-ones when the
            // low bit is set, all-zero otherwise, so the poly is XORed in only
            // on a 1 bit. INJ_CRC32_POLY is the contract's reflected 0xEDB88320.
            uint32_t mask = (uint32_t)0u - (crc & 1u);
            crc = (crc >> 1) ^ (INJ_CRC32_POLY & mask);
        }
    }

    return crc ^ INJ_CRC32_XOROUT;
}

// The packed payload structs are static-asserted in the generated header to be
// exactly INJ_FRAME_PAYLOAD_SIZE bytes with the contract's field offsets, so
// their raw bytes ARE the wire payload on a little-endian target. spi_frame_pack
// re-checks type and length, computes the CRC-16 and lays down SOF/type/seq/len.

spi_frame_result_t inj_build_map_begin(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                       const inj_map_begin_payload_t *payload)
{
    return spi_frame_pack(slot, INJ_TYPE_MAP_BEGIN, frame_sequence, (const uint8_t *)payload,
                          INJ_FRAME_PAYLOAD_SIZE);
}

spi_frame_result_t inj_build_map_entry(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                       const inj_map_entry_payload_t *payload)
{
    return spi_frame_pack(slot, INJ_TYPE_MAP_ENTRY, frame_sequence, (const uint8_t *)payload,
                          INJ_FRAME_PAYLOAD_SIZE);
}

spi_frame_result_t inj_build_map_commit(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                        const inj_map_commit_payload_t *payload)
{
    return spi_frame_pack(slot, INJ_TYPE_MAP_COMMIT, frame_sequence, (const uint8_t *)payload,
                          INJ_FRAME_PAYLOAD_SIZE);
}

spi_frame_result_t inj_build_relative(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                      const inj_relative_payload_t *payload)
{
    return spi_frame_pack(slot, INJ_TYPE_RELATIVE, frame_sequence, (const uint8_t *)payload,
                          INJ_FRAME_PAYLOAD_SIZE);
}

// BUTTON_STATE and PHYSICAL_MASK have near-identical layouts and adjacent type
// codes, and both carry a u64 button field. Pointing either builder at the
// other's type would still pack, still CRC and still round-trip cleanly, and
// the FPGA would act on the wrong field with nothing to notice it by -- there
// is no COMMAND_ACK. That is what inj_command_test asserts about these two, and
// it is the only reason they are worth separate tests at all.

spi_frame_result_t inj_build_button_state(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                          const inj_button_state_payload_t *payload)
{
    return spi_frame_pack(slot, INJ_TYPE_BUTTON_STATE, frame_sequence, (const uint8_t *)payload,
                          INJ_FRAME_PAYLOAD_SIZE);
}

spi_frame_result_t inj_build_physical_mask(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                           const inj_physical_mask_payload_t *payload)
{
    return spi_frame_pack(slot, INJ_TYPE_PHYSICAL_MASK, frame_sequence, (const uint8_t *)payload,
                          INJ_FRAME_PAYLOAD_SIZE);
}
