// CPU0 -> CPU1 shared SRAM. Step 6 preserves the four-word step-5 prefix and
// appends the seqlock snapshot from design doc section 5.

#ifndef HURRA_MCXN947_SHARED_WINDOW_H
#define HURRA_MCXN947_SHARED_WINDOW_H

#include <stdint.h>

#include "link_snapshot.h"

#define SHARED_WINDOW_MAGIC 0x48555231u /* "HUR1" */

// Capacity for one interface's HID report descriptor. This is the generated
// contract's INJ_MAX_DESCRIPTOR_BYTES_PER_INTERFACE, spelled as a literal for
// the same reason link_snapshot.h names INJ_LINK_STATUS_FLAG_* in a comment and
// does not include the header: this is the IPC layer, and neither it nor its
// host test should have to know the wire contract exists. link.c includes both
// and carries a _Static_assert that the two agree, so they cannot drift.
//
// Sized so a descriptor the FPGA would accept is always held IN FULL. A
// truncated descriptor decodes into plausible-looking nonsense, which is worse
// on a diagnostic panel than showing nothing at all.
//
// This is 2 KB of the 8 KB shared region, and it is free: the region is carved
// out of m_data by RPMSG_SHMEM_SIZE whether or not anything uses it, so
// spending more of it costs no RAM either core could otherwise have had.
#define SHARED_DESCRIPTOR_CAPACITY 2048u

// One completed HID report descriptor, published by CPU0 and read by CPU1.
//
// `sequence` is the same seqlock discipline link_snapshot uses -- even is
// stable, odd means a publish is in progress -- and it is separate from the
// snapshot's because the two are published on completely different schedules:
// the snapshot every slot, this once per enumeration.
//
// length == 0 means nothing has been published yet, which is the state CPU1
// starts in and returns to on a re-enumeration.
typedef struct {
    uint32_t sequence;
    uint16_t generation;  // the descriptor_generation these bytes belong to
    uint16_t length;      // valid bytes in `data`; 0 = nothing published
    uint8_t interface_number;
    uint8_t reserved[3];
    uint8_t data[SHARED_DESCRIPTOR_CAPACITY];
} shared_descriptor_t;

// Room for the two wires a framing fault can implicate. Spelled as a literal
// rather than including link_fault_classify.h, for the same reason
// SHARED_DESCRIPTOR_CAPACITY is spelled as one: this is the IPC layer, and
// neither it nor its host test should have to know the classifier exists.
// link.c includes both and carries a _Static_assert that the two agree.
#define SHARED_FAULT_MAX_SUSPECTS 2u

// The link fault classifier's latest verdict. `verdict` holds a
// link_fault_verdict_t and each `suspect` a link_pin_t, both narrowed to a
// byte: this block is a cross-core ABI, and an enum's width is a compiler's
// choice rather than a contract.
//
// Published on the 50 ms framing cadence, not per slot, so it carries its own
// sequence rather than riding the snapshot's 8 kHz seqlock.
//
// sequence == 0 means nothing has been classified yet -- the state CPU1 starts
// in -- and is deliberately distinct from a published verdict of OK.
typedef struct {
    uint32_t sequence;
    uint8_t verdict;
    uint8_t suspect_count;
    uint8_t suspect[SHARED_FAULT_MAX_SUSPECTS];
} shared_link_fault_t;

typedef struct {
    uint32_t magic;
    uint32_t cpu1_boot_count;
    uint32_t cpu1_heartbeat;
    // Section 5 makes a stale LPCAC read cheap and visible on a display. This
    // echo makes it numeric before the panel exists: CPU1 writes only what it
    // actually read, CPU0 observes it but never waits on it or treats it as a
    // command, exactly like the heartbeat word beside it.
    uint32_t cpu1_seen_slot_counter;

    // Frames CPU1 has completed, and what its panel is doing. Same category
    // again: CPU1 data that CPU0 observes and never waits on.
    //
    // Section 9 step 7's gate is "20 Hz repaint with link counters still flat",
    // which is a RATE, and a rate cannot be read off a panel by looking at it.
    // Without this the only evidence the display works is a human saying it
    // looks right, which is exactly what step 6 replaced for the LPCAC
    // question. `stats` prints frames/s next to the link counters so both
    // halves of that gate are one measurement.
    uint32_t cpu1_frames;
    uint32_t cpu1_display_flags;
    uint32_t cpu1_blits;
    uint32_t cpu1_blit_rejects;

    // The CPU0 -> CPU1 payload proper. Kept last so every CPU1 -> CPU0
    // observation word above it keeps its offset when the snapshot grows.
    link_snapshot_t snapshot;

    // The captured device's HID report descriptor, for CPU1 to decode and
    // display. It does NOT go in `snapshot`: that struct is a 32-byte seqlock
    // published at 8 kHz from the retirement interrupt, and a 2 KB payload has
    // no business in a per-slot publish. This one is written once per
    // enumeration and carries its own sequence.
    shared_descriptor_t descriptor;

    // Appended AFTER the descriptor block so that every offset above it --
    // the step-7 prefix, the snapshot and the descriptor -- is unchanged by
    // its arrival. Same append-only discipline the debug register maps use,
    // and for the same reason: a reader built against the old layout must
    // keep working rather than silently read the wrong field.
    shared_link_fault_t fault;
} shared_window_t;

// Bit 0 -- the panel initialised. Clear means display_init() failed, and the
// distinction matters: CPU1 deliberately keeps its heartbeat, echo and LED
// running with a dead panel, so a blank screen with a live heartbeat is
// otherwise indistinguishable from a panel that is simply not plugged in.
#define SHARED_DISPLAY_FLAG_READY 0x00000001u
// Bit 1 -- a blit was rejected or the transport reported an error since boot.
#define SHARED_DISPLAY_FLAG_FAULT 0x00000002u

// 64 bytes of step-7 window plus one 2060-byte descriptor block. The exact
// number is asserted rather than rounded because SHARED_WINDOW_SIZE in the
// Makefile is passed to both the merge tool and check_mcxn947_images.py, which
// assert g_shared_window's size and placement in BOTH images -- so this
// constant and that one cannot drift apart without the build saying so.
_Static_assert(sizeof(shared_descriptor_t) == 2060u,
               "descriptor block must be 2060 bytes");
_Static_assert(sizeof(shared_link_fault_t) == 8u,
               "link fault block must be 8 bytes");
_Static_assert(sizeof(shared_window_t) == 2132u,
               "shared window must be 2132 bytes (64 + 2060 + 8)");

extern volatile shared_window_t g_shared_window;

// The linker section is NOLOAD, so startup on neither core initializes it.
// CPU0 calls this after mcu_ready is raised and before CPU1 is released.
void shared_window_reset(void);

#endif  // HURRA_MCXN947_SHARED_WINDOW_H
