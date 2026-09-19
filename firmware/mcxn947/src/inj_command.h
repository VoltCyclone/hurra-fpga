// Injection-command frame builders: the MCU's TX side of the report-injection
// wire contract. Where link_retire.c/spi_frame.c cover receiving and framing,
// this covers *emitting* the controller-initiated messages -- the map upload
// (MAP_BEGIN / MAP_ENTRY / MAP_COMMIT) and the live RELATIVE motion command.
//
// Everything here is portable and MMIO-free, so it host-compiles for
// inj_command_test.c. Each builder takes a generated packed payload struct
// (include/injection_wire.h, byte-offset- and size-asserted against the JSON
// contract) and hands it to spi_frame_pack(), so the wire layout has exactly
// one source of truth and cannot drift from the FPGA or the Python reference.
//
// Endianness: the generated payload structs store multi-byte fields in native
// order and the wire is little-endian, so these builders are correct only on a
// little-endian target. The MCXN947 (Cortex-M33) and the x86_64/arm64 host that
// runs the tests are all little-endian; the generated header already commits to
// this by using packed structs of native integers.

#ifndef INJ_COMMAND_H
#define INJ_COMMAND_H

#include <stdint.h>

#include "injection_wire.h"
#include "spi_frame.h"

// Reflected IEEE CRC-32 (poly 0xEDB88320, init/xorout 0xFFFFFFFF) -- i.e.
// zlib.crc32, which is what the FPGA's map_store validates MAP_ENTRY bytes
// against and what the Python reference uses. Table-free; the constants come
// from the generated contract macros so they cannot drift from the JSON.
uint32_t inj_crc32(const uint8_t *data, uint32_t length);

// Build one command frame into `slot`. `frame_sequence` is the link-level frame
// sequence (slot byte 2) the FPGA classifies for its RX window -- NOT the
// command_sequence carried inside the RELATIVE payload. Each returns the
// spi_frame_pack() result; SPI_FRAME_OK on success.
spi_frame_result_t inj_build_map_begin(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                       const inj_map_begin_payload_t *payload);
spi_frame_result_t inj_build_map_entry(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                       const inj_map_entry_payload_t *payload);
spi_frame_result_t inj_build_map_commit(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                        const inj_map_commit_payload_t *payload);
// Injected button state, and which of the REAL device's buttons are suppressed
// on the way to the PC. Note PHYSICAL_MASK is buttons-only: there is no motion
// mask anywhere in the contract, which is why absolute positioning and axis
// locks are not expressible on this link (see kmcmd.h).
spi_frame_result_t inj_build_button_state(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                          const inj_button_state_payload_t *payload);
spi_frame_result_t inj_build_physical_mask(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                           const inj_physical_mask_payload_t *payload);

spi_frame_result_t inj_build_relative(uint8_t slot[INJ_FRAME_SIZE], uint8_t frame_sequence,
                                      const inj_relative_payload_t *payload);

#endif  // INJ_COMMAND_H
