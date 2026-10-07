// Host test for link.c's portable half.
//
// It compiles src/link.c WITHOUT -DMCXN947, so it also proves the guard
// polarity: every MMIO access in that file is inside `#if defined(MCXN947)`,
// and nothing outside the guard reaches for a vendor header.
//
// What is actually under test is step 2's safety argument. The step raises
// `mcu_ready`, which makes the FPGA validate every slot the MCU returns, and
// the only thing standing between that and an active injection map is that the
// slot is IDLE. link_slot_is_inert_idle() is that claim as a predicate; these
// cases are the claim as evidence, in both directions:
//
//   * the slot we actually transmit is inert, and
//   * everything that is NOT that slot is rejected -- including a perfectly
//     well-formed deliverable frame, which is the case that matters.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "injection_wire.h"
#include "link.h"
#include "spi_frame.h"

int main(void)
{
    // The slot this step clocks out forever, byte for byte. SOF, type IDLE,
    // sequence 0, length 0, 26 zero payload bytes, then CRC-16/CCITT-FALSE
    // 0x36FE stored low byte first. Hard-coded rather than recomputed: a
    // golden catches a codec change, a recomputation would follow it.
    static const uint8_t idle_golden[INJ_FRAME_SIZE] = {
        [0] = 0x68u,
        [30] = 0xFEu,
        [31] = 0x36u,
    };
    uint8_t slot[INJ_FRAME_SIZE];
    uint8_t cached[INJ_FRAME_SIZE];
    uint8_t probe[INJ_FRAME_SIZE];

    // What link_init() seeds into both TX banks.
    memset(slot, 0xA5u, sizeof(slot));
    link_build_idle_slot(slot);
    assert(memcmp(slot, idle_golden, sizeof(idle_golden)) == 0);
    assert(link_slot_is_inert_idle(slot));

    // The steady-state ISR copies a slot packed once at boot. This expectation
    // stays independent of that cache and its builder: a wrong-but-consistent
    // IDLE would otherwise be drained by the FPGA as DUPLICATE with every
    // spi_bad_* counter flat.
    memset(cached, 0x5Au, sizeof(cached));
    link_idle_slot_cache_init();
    link_copy_cached_idle_slot(cached);
    assert(memcmp(cached, idle_golden, sizeof(idle_golden)) == 0);
    assert(link_slot_is_inert_idle(cached));

    // Design doc section 4: "both TX buffers must be seeded with a complete
    // valid IDLE frame before ERQ is set (zeroed buffers fail SOF)". An
    // unseeded bank is the single most likely boot mistake in this step, and
    // on the wire it is a spi_bad_sof storm.
    memset(probe, 0, sizeof(probe));
    assert(!link_slot_is_inert_idle(probe));

    // Each header fault, in the order the FPGA buckets them. Every one of
    // these on the wire moves a counter the step-2 gate watches, so the
    // predicate must reject all four.
    memcpy(probe, idle_golden, sizeof(probe));
    probe[0] = 0x69u;  // -> spi_bad_sof
    assert(!link_slot_is_inert_idle(probe));

    memcpy(probe, idle_golden, sizeof(probe));
    probe[1] = 0x7Fu;  // unassigned type -> spi_bad_type
    assert(!link_slot_is_inert_idle(probe));

    memcpy(probe, idle_golden, sizeof(probe));
    probe[3] = (uint8_t)INJ_FRAME_PAYLOAD_SIZE;  // IDLE ships 0 -> spi_bad_length
    assert(!link_slot_is_inert_idle(probe));

    memcpy(probe, idle_golden, sizeof(probe));
    probe[30] ^= 0xFFu;  // -> spi_bad_crc
    assert(!link_slot_is_inert_idle(probe));

    // The case the whole step rests on: a frame that passes every single
    // validation the FPGA performs is STILL rejected here, because it is
    // deliverable. `valid_return` ends with `& (rx_type != INJ_TYPE_IDLE)`, so
    // this slot would set rx_valid, reach the injection plane, and -- for a map
    // message -- be acted on. "Inert" has to mean "cannot be delivered", not
    // merely "cannot be faulted", or the predicate would be worthless.
    assert(spi_frame_pack(probe, INJ_TYPE_RELATIVE, 7u, idle_golden,
                          INJ_FRAME_PAYLOAD_SIZE) == SPI_FRAME_OK);
    assert(!link_slot_is_inert_idle(probe));
    // ... and it really is well formed, so the rejection above came from the
    // type and not from some incidental fault.
    assert(spi_frame_unpack(probe, NULL, NULL, NULL, NULL) == SPI_FRAME_OK);

    // A map-bearing type specifically. INJ_TYPE_MAP_COMMIT is the message that
    // would activate a field map; step 2 must be structurally incapable of
    // emitting one, and it is, because nothing ever calls spi_frame_pack() with
    // a type other than INJ_TYPE_IDLE.
    assert(spi_frame_pack(probe, INJ_TYPE_MAP_COMMIT, 1u, idle_golden,
                          INJ_FRAME_PAYLOAD_SIZE) == SPI_FRAME_OK);
    assert(!link_slot_is_inert_idle(probe));

    // Ring bookkeeping. Two banks, so next() is an alternation and applying it
    // twice is the identity.
    assert(LINK_SLOT_BANKS == 2u);
    assert(link_next_bank(0u) == 1u);
    assert(link_next_bank(1u) == 0u);
    for (uint8_t bank = 0u; bank < LINK_SLOT_BANKS; ++bank) {
        assert(link_next_bank(link_next_bank(bank)) == bank);
        assert(link_next_bank(bank) != bank);
    }

    // The DMA geometry the ring is built from. These are _Static_assert-ed in
    // link.h as well; restated here so a reader of the test sees the numbers
    // the TCDs actually carry.
    assert(LINK_DMA_MINOR_BYTES == 4u);
    assert(LINK_DMA_MAJOR_ITER == 8u);
    assert(LINK_SPI_FRAMESZ == 255u);

    // Normalized -32768..32767 onto a field's logical range: ends land on the
    // ends, the centre on the upper middle, rounding is to nearest, and the
    // result never leaves the range. For an unsigned 16-bit field the value is
    // returned as the FIELD'S BIT PATTERN (the FPGA writes only bit_width bits).
    assert(link_scale_absolute(-32768, 0, 255) == 0);
    assert(link_scale_absolute(32767, 0, 255) == 255);
    assert(link_scale_absolute(0, 0, 255) == 128);
    assert(link_scale_absolute(-1, 0, 255) == 127);
    assert(link_scale_absolute(16384, 0, 255) == 191);  // 191.25 rounds down
    assert(link_scale_absolute(-16384, 0, 255) == 64);
    assert(link_scale_absolute(-32768, -32768, 32767) == -32768);  // identity
    assert(link_scale_absolute(32767, -32768, 32767) == 32767);
    assert(link_scale_absolute(1234, -32768, 32767) == 1234);
    assert(link_scale_absolute(-5, -32768, 32767) == -5);
    assert(link_scale_absolute(32767, 0, 4095) == 4095);
    assert(link_scale_absolute(-32768, 0, 4095) == 0);
    assert(link_scale_absolute(0, 0, 4095) == 2048);
    assert((uint16_t)link_scale_absolute(32767, 0, 65535) == 65535u);
    assert((uint16_t)link_scale_absolute(0, 0, 65535) == 32768u);
    assert(link_scale_absolute(-32768, 0, 65535) == 0);
    assert(link_scale_absolute(-32768, -127, 127) == -127);
    assert(link_scale_absolute(32767, -127, 127) == 127);
    assert(link_scale_absolute(0, -127, 127) == 0);
    assert(link_scale_absolute(100, 5, 5) == 5);  // degenerate range: the minimum
    int32_t last = -1;
    for (int32_t n = -32768; n <= 32767; ++n) {
        const int32_t v = link_scale_absolute((int16_t)n, 0, 255);
        assert(v >= 0 && v <= 255 && v >= last);  // monotonic, in range
        last = v;
    }
    assert(LINK_PAD_CHANNELS == 7u);

    printf("link_test: ok\n");
    return 0;
}
