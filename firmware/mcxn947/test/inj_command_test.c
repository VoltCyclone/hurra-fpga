// Host test for src/inj_command.c -- the MCU's injection-command TX builders.
//
// Two independent things under test:
//   1. inj_crc32(): must be zlib.crc32 exactly, because the FPGA validates the
//      accumulated MAP_ENTRY bytes against the entries_crc32 the MCU stamps in
//      MAP_BEGIN/MAP_COMMIT. A one-bit disagreement rejects every map.
//   2. The four builders: each must emit a well-formed frame that round-trips
//      through spi_frame_unpack() (proving SOF/type/len/CRC-16) and whose
//      payload bytes land at the contract's little-endian offsets.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "inj_command.h"
#include "injection_wire.h"
#include "spi_frame.h"

static void test_crc32_matches_zlib(void)
{
    // The canonical CRC-32 check value: crc32("123456789") == 0xCBF43926.
    const uint8_t check[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
    assert(inj_crc32(check, sizeof(check)) == 0xCBF43926u);

    // Empty input is the bare init^xorout == 0.
    assert(inj_crc32(NULL, 0u) == 0u);

    // A single zero byte -- an independent fixed point vs zlib.crc32(b"\x00").
    const uint8_t zero = 0u;
    assert(inj_crc32(&zero, 1u) == 0xD202EF8Du);
}

static void test_relative_round_trips_with_fields_at_offsets(void)
{
    inj_relative_payload_t rel;
    memset(&rel, 0, sizeof(rel));
    rel.lease_generation = 1u;
    rel.map_generation = 1u;
    rel.command_sequence = 0x1234u;
    rel.interface_number = 0u;
    rel.endpoint_number = 1u;
    rel.report_id = 0u;
    rel.flags = INJ_RELATIVE_FLAG_X;
    rel.x = (int16_t)-6;  // 0xFFFA little-endian

    uint8_t slot[INJ_FRAME_SIZE];
    assert(inj_build_relative(slot, 7u, &rel) == SPI_FRAME_OK);

    // Header, by hand: SOF, type, frame sequence, length.
    assert(slot[SPI_FRAME_OFF_SOF] == INJ_FRAME_SOF);
    assert(slot[SPI_FRAME_OFF_TYPE] == INJ_TYPE_RELATIVE);
    assert(slot[SPI_FRAME_OFF_SEQUENCE] == 7u);
    assert(slot[SPI_FRAME_OFF_LENGTH] == INJ_FRAME_PAYLOAD_SIZE);

    // Payload fields at their contract offsets, little-endian.
    const uint8_t *p = &slot[SPI_FRAME_OFF_PAYLOAD];
    assert(p[INJ_RELATIVE_MAP_GENERATION_OFFSET] == 1u);
    assert(p[INJ_RELATIVE_COMMAND_SEQUENCE_OFFSET] == 0x34u);
    assert(p[INJ_RELATIVE_COMMAND_SEQUENCE_OFFSET + 1u] == 0x12u);
    assert(p[INJ_RELATIVE_ENDPOINT_NUMBER_OFFSET] == 1u);
    assert(p[INJ_RELATIVE_FLAGS_OFFSET] == INJ_RELATIVE_FLAG_X);
    assert(p[INJ_RELATIVE_X_OFFSET] == 0xFAu);
    assert(p[INJ_RELATIVE_X_OFFSET + 1u] == 0xFFu);

    // The whole frame must decode: this is the CRC-16 and framing check.
    uint8_t type = 0u;
    uint8_t sequence = 0u;
    const uint8_t *payload = NULL;
    uint8_t length = 0u;
    assert(spi_frame_unpack(slot, &type, &sequence, &payload, &length) == SPI_FRAME_OK);
    assert(type == INJ_TYPE_RELATIVE);
    assert(sequence == 7u);
    assert(length == INJ_FRAME_PAYLOAD_SIZE);
    assert(memcmp(payload, &rel, sizeof(rel)) == 0);
}

static void test_map_frames_round_trip(void)
{
    inj_map_begin_payload_t begin;
    memset(&begin, 0, sizeof(begin));
    begin.descriptor_generation = 3u;
    begin.map_generation = 1u;
    begin.entry_count = 1u;
    begin.layout_count = 1u;
    begin.entries_crc32 = 0xDEADBEEFu;

    inj_map_entry_payload_t entry;
    memset(&entry, 0, sizeof(entry));
    entry.descriptor_generation = 3u;
    entry.map_generation = 1u;
    entry.endpoint_number = 1u;
    entry.usage_page = 0x01u;
    entry.usage = 0x30u;
    entry.bit_offset = 8u;
    entry.bit_width = 8u;
    entry.flags = INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X;
    entry.logical_minimum = -127;
    entry.logical_maximum = 127;
    entry.report_length = 4u;

    inj_map_commit_payload_t commit;
    memset(&commit, 0, sizeof(commit));
    commit.descriptor_generation = 3u;
    commit.map_generation = 1u;
    commit.entry_count = 1u;
    commit.layout_count = 1u;
    commit.entries_crc32 = 0xDEADBEEFu;

    uint8_t slot[INJ_FRAME_SIZE];
    uint8_t type = 0u;
    const uint8_t *payload = NULL;
    uint8_t length = 0u;

    assert(inj_build_map_begin(slot, 1u, &begin) == SPI_FRAME_OK);
    assert(spi_frame_unpack(slot, &type, NULL, &payload, &length) == SPI_FRAME_OK);
    assert(type == INJ_TYPE_MAP_BEGIN && length == INJ_FRAME_PAYLOAD_SIZE);
    assert(memcmp(payload, &begin, sizeof(begin)) == 0);
    // entries_crc32 landed little-endian at its offset.
    assert(payload[INJ_MAP_BEGIN_ENTRIES_CRC32_OFFSET] == 0xEFu);
    assert(payload[INJ_MAP_BEGIN_ENTRIES_CRC32_OFFSET + 3u] == 0xDEu);

    assert(inj_build_map_entry(slot, 2u, &entry) == SPI_FRAME_OK);
    assert(spi_frame_unpack(slot, &type, NULL, &payload, &length) == SPI_FRAME_OK);
    assert(type == INJ_TYPE_MAP_ENTRY);
    assert(memcmp(payload, &entry, sizeof(entry)) == 0);
    assert(payload[INJ_MAP_ENTRY_BIT_OFFSET_OFFSET] == 8u);

    assert(inj_build_map_commit(slot, 3u, &commit) == SPI_FRAME_OK);
    assert(spi_frame_unpack(slot, &type, NULL, &payload, &length) == SPI_FRAME_OK);
    assert(type == INJ_TYPE_MAP_COMMIT);
    assert(memcmp(payload, &commit, sizeof(commit)) == 0);
}

int main(void)
{
    test_crc32_matches_zlib();
    test_relative_round_trips_with_fields_at_offsets();
    test_map_frames_round_trip();

    printf("inj_command_test: ok\n");
    return 0;
}
