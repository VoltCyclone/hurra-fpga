// One-way CPU0 -> CPU1 link state. The algorithm is portable and host-tested;
// link_snapshot.c changes only the spelling of its ordering barrier on target.

#ifndef HURRA_MCXN947_LINK_SNAPSHOT_H
#define HURRA_MCXN947_LINK_SNAPSHOT_H

#include <stdbool.h>
#include <stdint.h>

#define LINK_SNAPSHOT_READ_ATTEMPTS 4u

typedef struct {
    uint32_t seq;  // Even = stable, odd = write in progress.
    uint32_t slot_counter;
    uint32_t native_report_count;
    uint16_t usb_frame;
    uint16_t usb_subframe;
    uint16_t link_flags;  // INJ_LINK_STATUS_FLAG_* from injection_wire.h.
    uint16_t descriptor_generation;
    uint16_t map_generation;
    uint16_t fault_flags;
    uint8_t last_rx_sequence;

    // Section 5 says this contract is 32 bytes but shows _pad[3], whose fields
    // total only 28. Seven bytes preserves every named field and realizes the
    // stated 32-byte ABI without hiding the discrepancy in an alignment
    // attribute.
    uint8_t _pad[7];
} link_snapshot_t;

_Static_assert(sizeof(link_snapshot_t) == 32u, "link snapshot must be 32 bytes");

// Publish one complete generation. src->seq is deliberately ignored: only the
// destination's monotonically advancing sequence describes shared state.
void link_snapshot_publish(volatile link_snapshot_t *dst,
                           const link_snapshot_t *src);

// Try at most LINK_SNAPSHOT_READ_ATTEMPTS times. On failure, *out is untouched
// so a presentation caller naturally keeps rendering its last good copy.
bool link_snapshot_read(const volatile link_snapshot_t *src,
                        link_snapshot_t *out);

#endif  // HURRA_MCXN947_LINK_SNAPSHOT_H
