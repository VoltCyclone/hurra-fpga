// Mouse layout -> MAP_ENTRY payloads, the wire form of an injection map.
//
// Pure translation: hid_mouse_layout.c decides WHAT is injectable, this only
// says it in the contract's terms (include/injection_wire.h) and stamps the
// addressing the session learned. Portable and MMIO-free; host-tested by
// inj_map_build_test.c.

#ifndef HURRA_MCXN947_INJ_MAP_BUILD_H
#define HURRA_MCXN947_INJ_MAP_BUILD_H

#include <stdint.h>

#include "hid_mouse_layout.h"
#include "injection_wire.h"

typedef struct {
    uint16_t descriptor_generation;
    uint16_t map_generation;
    uint8_t interface_number;
    uint8_t endpoint_number;
    // The FPGA binds a layout to a report only on an exact length match, so
    // this is the length the device is seen SENDING, which the session may
    // have adopted over the descriptor's figure (see inj_session.h).
    uint8_t report_length;
} inj_map_target_t;

// Write one entry per layout field, in layout order, and return the count.
uint8_t inj_map_build_entries(const hid_mouse_layout_t *layout, const inj_map_target_t *target,
                              inj_map_entry_payload_t out[HID_MOUSE_MAX_FIELDS]);

// entries_crc32 as MAP_BEGIN and MAP_COMMIT carry it: CRC-32 over the entries'
// payload bytes in order, which is what the FPGA's map store recomputes.
uint32_t inj_map_entries_crc32(const inj_map_entry_payload_t *entries, uint8_t count);

#endif  // HURRA_MCXN947_INJ_MAP_BUILD_H
