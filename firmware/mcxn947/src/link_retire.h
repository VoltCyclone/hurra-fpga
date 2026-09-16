// RX retirement: turning completed eDMA receive banks into validated,
// sequence-classified frames.
//
// Migration step 3 of docs/MCXN947_CONTROLLER.md section 9, and the "portable
// transport half" that step names. Nothing in this module touches MMIO, so it
// host-compiles and test/link_retire_test.c drives every path with fabricated
// slots. The hardware that calls it -- the eDMA0 channel 1 major-loop ISR --
// lives behind `#if defined(MCXN947)` in link.c.
//
// The reference implementation is firmware/ch32h417/src/ch32_link.c's
// retirement half, which has run on the outgoing MCU. The validation order,
// the IDLE-is-not-sequenced rule and the drain/act/advance dispositions are
// carried over unchanged; what is new here is link_retire_active_bank(), which
// exists because the two parts arm their DMA differently.
//
// --- Why the bank has to be derived and cannot be assumed -------------------
//
// The CH32 runs its DMA in DMA_Mode_Normal: a channel disables itself at
// transfer complete and the ISR re-points it, so software *chooses* the bank
// and always knows which one is live. The MCXN947 ring is scatter-gather and
// self-loading (link.c): the engine reloads the next descriptor on its own and
// never tells the CPU which bank that was. A software `bank ^= 1` here would
// be a second, independent model of the hardware's position that nothing keeps
// in step -- and after an ERR051588 recovery, or a single missed completion,
// it would be wrong with no symptom other than corrupt frames.
//
// It is not a theoretical concern. Measured on the bench at step 2: a read of
// the bank the DMA currently owns returns a TORN slot -- bank 0 read clean
// while bank 1 read `68 00 74 00 ... f0 ...` mid-fill at CITER=6. Design doc
// section 4's rule is "only ever touch the bank the DMA is *not* pointing at",
// and the only thing that knows where it is pointing is the live TCD.
//
// So the ISR reads TCD_DADDR and asks this module which bank that address is
// inside; everything from the retirement cursor up to (not including) that
// bank is safe and is retired. A torn read would fail its CRC immediately, so
// `bad_crc` staying at zero over millions of slots is the running proof that
// the derivation is right.

#ifndef HURRA_MCXN947_LINK_RETIRE_H
#define HURRA_MCXN947_LINK_RETIRE_H

#include <stdbool.h>
#include <stdint.h>

#include "injection_wire.h"
#include "spi_frame.h"

// Deliverable frames waiting for a consumer. Power of two; the enqueue test is
// `next = (head + 1) & LINK_RETIRE_RING_MASK`. Eight, as on the CH32: at this
// step nothing consumes the ring at all (the FPGA transmits only IDLE, which
// is never deliverable), so its only job is to exist and be correct before
// step 8 gives it a reader.
#define LINK_RETIRE_RING_SIZE 8u
#define LINK_RETIRE_RING_MASK (LINK_RETIRE_RING_SIZE - 1u)

// Cumulative and monotonic; only link_retire_reset() clears them. The first
// five mirror the FPGA's own counters by name so the two ends can be compared
// directly over JTAG -- `slots` against `spi_slots`, `bad_sof` against
// `spi_bad_sof`, and so on. They are NOT the same measurement: the FPGA counts
// what it received from us, these count what we received from it. Both being
// flat is two independent statements about the same wire.
typedef struct {
    uint32_t slots;        // RX banks retired, all outcomes. The MCU-side 8 kHz.
    uint32_t idle;         // Well-formed IDLE keepalives: a slot, never delivered.
    uint32_t deliverable;  // SPI_FRAME_OK frames that acted and advanced the window.
    uint32_t bad_sof;      // SPI_FRAME_ERR_SOF.
    uint32_t bad_crc;      // SPI_FRAME_ERR_CRC -- also what a torn bank read looks like.
    uint32_t bad_length;   // SPI_FRAME_ERR_LEN.
    uint32_t bad_type;     // SPI_FRAME_ERR_TYPE.
    uint32_t duplicate;    // delta == 0: drain, do not act, do not advance.
    uint32_t stale;        // delta 0x80..0xFF: drain, never move the window back.
    uint32_t sequence_gap; // delta 0x02..0x7F: act and advance, but slots were lost.
    uint32_t ring_drop;    // Deliverable frame dropped because the RX ring was full.
} link_retire_counters_t;

// --- Bank safety ------------------------------------------------------------

// Which bank of a contiguous `banks x stride` array is the DMA writing, given
// the live destination address from its TCD?
//
// Returns false, leaving *active untouched, when `destination_address` is not
// inside the array at all. That is a hard fault -- a corrupted TCD, the wrong
// channel, or a descriptor that was never installed -- and the caller must
// retire nothing rather than guess.
//
// The one case that needs stating: at major-loop completion, and *before* the
// scatter-gather reload is visible, DADDR sits one byte past the end of the
// bank that just finished. For the last bank that is `base + banks * stride`,
// one past the whole array. That address is reported as bank 0, because bank 0
// is where the ring reloads to -- it is a wrap, not an overrun. Treating it as
// out of range instead would make every ISR entry after the last bank retire
// nothing, and the link would silently drop half its slots.
bool link_retire_active_bank(uint32_t destination_address,
                             uint32_t base,
                             uint32_t stride,
                             uint8_t banks,
                             uint8_t *active);

// --- Retirement -------------------------------------------------------------

// Clear every ring, counter and sequence-window bit. link_init() calls this
// before the DMA is armed; tests call it to isolate cases.
void link_retire_reset(void);

// Retire one completed 32-byte RX slot: bump `slots`, validate it with the
// shared codec, and for a deliverable frame classify its sequence and enqueue
// it. IDLE keepalives count as a slot and stop there -- they carry a hardwired
// sequence 0, and feeding that to the classifier would make a healthy link
// whose traffic is sparse read as almost entirely stale.
void link_retire_slot(const uint8_t slot[INJ_FRAME_SIZE]);

// Pop the oldest deliverable frame. False, with *out untouched, when empty.
bool link_retire_receive(inj_frame_t *out);

// Read-only view of the diagnostics.
const link_retire_counters_t *link_retire_counters(void);

// A verbatim copy of the last slot handed to link_retire_slot(), whatever its
// outcome. This is the evidence that retirement is reading real wire bytes
// rather than an untouched buffer: a bank nothing ever filled reads as zeros,
// and zeros are not what the FPGA's IDLE keepalive looks like.
//
// The caller must copy it out with the retirement interrupt masked. Printing it
// a byte at a time straight from this pointer is a race that MANUFACTURES
// EVIDENCE: a 32-byte hex dump takes ~30 ms at 115200 baud and the ISR rewrites
// the buffer 240 times in that window, so what reaches the terminal is a
// splice of hundreds of slots. That is exactly how a healthy link's keepalive
// acquires a scatter of stray bytes it never had on the wire, and it cost a
// wrong diagnosis on the bench before it was noticed.
const uint8_t *link_retire_last_slot(void);

// --- Framing health ---------------------------------------------------------
//
// The fault an LPSPI slave cannot report about itself.
//
// Measured on the bench at step 3: the slave's frame boundary is fixed at the
// moment CR[MEN] is set, and if that lands mid-frame the boundary is wrong by
// however many bits were left -- 69 of them on one boot -- FOREVER. It does not
// resynchronise on chip select, and it is boot-random: consecutive flashes of a
// byte-identical image came up 69 bits out and then perfectly aligned.
//
// Nothing in SR says so. There is no underrun, no overrun, no DMA error; every
// status register reads exactly as it does on a healthy link, because from the
// peripheral's point of view nothing has gone wrong -- it is shifting the bits
// it is given. The only evidence is semantic: every slot retired fails its SOF
// check, and the FPGA's spi_bad_sof runs at 1:1 with spi_slots.
//
// So framing health is a property of the RETIRED CONTENT, not of a register,
// which is why it lives in this module.
typedef struct {
    uint32_t slots;  // link_retire_counters()->slots
    uint32_t good;   // idle + deliverable: everything that parsed
} link_framing_sample_t;

// Slots that must have been retired in the interval before the verdict is
// trusted. At 8 kHz this is ~25 ms of wire. Below it, a sample pair straddling
// a recovery or a boot would be judged on a handful of slots.
#define LINK_FRAMING_MIN_SLOTS 200u

// Take a framing sample from the live counters.
void link_retire_framing_sample(link_framing_sample_t *out);

// True when the link is definitely mis-framed: at least LINK_FRAMING_MIN_SLOTS
// slots were retired between the two samples and NOT ONE of them parsed.
//
// The threshold is "none", not "most", deliberately. A link that is framed and
// merely noisy still lands well-formed slots between the bad ones -- the FPGA
// clocks 8,000 a second and its keepalive is constant -- so zero good slots
// across 200 is not a bad patch, it is a boundary in the wrong place. Anything
// weaker would tear down a working link over a burst of interference.
bool link_retire_framing_lost(const link_framing_sample_t *previous,
                              const link_framing_sample_t *current);

#endif  // HURRA_MCXN947_LINK_RETIRE_H
