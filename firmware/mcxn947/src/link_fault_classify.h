// Turning link counters into a named suspect pin.
//
// The link is six wires and a frame-phase strobe. When it breaks, every one of
// them produces the same top-level symptom -- no frames reach the application --
// and the bench answer has so far been to reseat things until it works. This
// module is the part of that guesswork that can be made deterministic: given a
// window of retirement counters, which wire is it?
//
// NOTHING HERE TOUCHES MMIO, so it host-compiles and
// test/link_fault_classify_test.c drives every verdict with fabricated windows.
// link.c supplies the counter deltas and the last bank; it holds no opinion of
// its own about what they mean.
//
// --- What can and cannot be diagnosed ---------------------------------------
//
// The MCU can only diagnose wires it RECEIVES on. Of the link's six:
//
//   SCK  (J6-4, P3_21)     diagnosable -- no clock means no slots at all
//   MOSI (J6-6, P3_20)     diagnosable -- a bank that is all 00 or all ff
//   CS   (J6-3, P3_23)     diagnosable -- frames arrive, boundaries are wrong
//   GND  (J6-8)            diagnosable -- bits corrupt but framing intact
//
//   MISO (J6-5, P3_22)     NOT diagnosable -- the MCU drives it and cannot
//                          observe whether the FPGA receives anything
//   mcu_ready (J5-2, P5_7) NOT diagnosable -- likewise an output, and it floats
//                          HIGH when disconnected, so the FPGA reads a ready
//                          MCU either way
//
// Both blind spots are MCU->FPGA directions. Closing them needs the FPGA's own
// counters carried back in telemetry: INJ_TYPE_COUNTERS (0x06) is reserved in
// protocol/report_injection_wire.json for exactly that and is NOT implemented
// in the gateware. Until it is, a report that names no pin does not mean the
// link is sound -- it means the fault is not visible from this side.
//
// --- Why bad_crc is the discriminator ---------------------------------------
//
// spi_frame_unpack() checks SOF FIRST and returns early, so a slot that fails
// its SOF check never reaches the CRC. That makes the two counters read as a
// clean split rather than a correlation:
//
//   bad_sof high, bad_crc ZERO   -- the bytes are intact and the frame boundary
//                                   is in the wrong place. A framing fault.
//   bad_crc nonzero              -- the boundary is RIGHT (SOF matched) and the
//                                   payload is corrupt. An integrity fault.
//
// The trap this avoids: bad_crc == 0 reads like "no corruption" and is the
// opposite. On a fully mis-framed link the CRC check is unreachable, so a
// silent CRC counter is what a broken wire looks like, not a healthy one.

#ifndef HURRA_MCXN947_LINK_FAULT_CLASSIFY_H
#define HURRA_MCXN947_LINK_FAULT_CLASSIFY_H

#include <stdbool.h>
#include <stdint.h>

#include "injection_wire.h"

// The link wires this module can name. Ordered so that a lower enumerator is a
// more fundamental failure: no clock before no data before bad framing.
typedef enum {
    LINK_PIN_NONE = 0,
    LINK_PIN_SCK,
    LINK_PIN_MOSI,
    LINK_PIN_CS,
    LINK_PIN_USB_SYNC,
    LINK_PIN_GND,
    LINK_PIN_COUNT
} link_pin_t;

typedef enum {
    // Frames are parsing. Says nothing about the MCU->FPGA direction.
    LINK_FAULT_OK = 0,
    // No slots retired and no ISR entries across the whole window.
    LINK_FAULT_NO_CLOCK,
    // Slots retire, but every byte of the bank is 00 or every byte is ff.
    LINK_FAULT_NO_DATA,
    // Slots retire carrying varied bytes, and none of them parse.
    LINK_FAULT_MIS_FRAMED,
    // Frames parse far enough to reach the CRC, and fail it.
    LINK_FAULT_NOISY,
    // Slots retire, bytes are varied, nothing parses, and none of the sharper
    // predicates fit. Deliberately distinct from MIS_FRAMED: it means "broken,
    // and this module will not guess which wire".
    LINK_FAULT_STALE,
    LINK_FAULT_COUNT
} link_fault_verdict_t;

// A framing fault has two candidate wires and no way to separate them from
// counters alone, so a report carries up to two suspects.
#define LINK_FAULT_MAX_SUSPECTS 2u

// One window of evidence. Every count is a DELTA across the window, never a
// running total -- a cumulative counter would make a link that was broken an
// hour ago and is healthy now read as broken forever.
typedef struct {
    uint32_t slots;
    uint32_t idle;
    uint32_t deliverable;
    uint32_t bad_sof;
    uint32_t bad_crc;
    uint32_t bad_length;
    uint32_t bad_type;
    // eDMA retirement ISR entries. Separates "no clock" from "clocking, but
    // the ring is not retiring" -- without it both read as slots == 0.
    uint32_t isr_entries;
    // The last bank handed to link_retire_slot(), verbatim. Must be copied out
    // with the retirement IRQ masked; see link_retire_last_slot().
    uint8_t bank[INJ_FRAME_SIZE];
    bool bank_valid;
} link_fault_evidence_t;

typedef struct {
    link_fault_verdict_t verdict;
    link_pin_t suspect[LINK_FAULT_MAX_SUSPECTS];
    uint8_t suspect_count;
} link_fault_report_t;

// Slots that must have been retired in the window before any verdict other
// than NO_CLOCK is trusted. Matches LINK_FRAMING_MIN_SLOTS: below it, a window
// straddling a recovery is judged on a handful of slots.
#define LINK_FAULT_MIN_SLOTS 200u

// Classify one window. `out` is always fully written, including on a NULL or
// empty `evidence`, so a caller never renders a stale report.
void link_fault_classify(const link_fault_evidence_t *evidence,
                         link_fault_report_t *out);

// Short stable names for the display. Never NULL, including out of range.
const char *link_fault_verdict_name(link_fault_verdict_t verdict);

// "CS", "usb_sync", ...
const char *link_pin_name(link_pin_t pin);

// Where to physically look: "J6-3 P3_23". This is the half of the report that
// is actually actionable at the bench, and it is kept beside the pin name so
// the two cannot drift apart across a header and a display module.
const char *link_pin_location(link_pin_t pin);

#endif  // HURRA_MCXN947_LINK_FAULT_CLASSIFY_H
