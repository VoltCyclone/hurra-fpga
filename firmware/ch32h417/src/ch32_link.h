#ifndef CH32_LINK_H
#define CH32_LINK_H

#include <stdbool.h>
#include <stdint.h>

#include "injection_wire.h"
#include "spi_frame.h"

/* Board-to-board SPI link retirement, queueing, and health.
 *
 * The FPGA is the SPI master and clocks one 32-byte full-duplex slot every
 * 125 us. This module owns the two directions:
 *
 *   RX  completed DMA slot -> ch32_link_retire_slot() -> validate, classify
 *       sequence, and enqueue deliverable frames -> ch32_link_receive().
 *   TX  ch32_link_submit() -> queue a frame -> ch32_link_fill_tx_slot() packs
 *       the next slot the DMA engine clocks out, or an IDLE keepalive when the
 *       queue is empty.
 *
 * Everything except the SPI1/DMA/ISR glue is MMIO-free so the retirement logic
 * host-compiles and is driven directly by test/ch32_link_logic_test.c with
 * fabricated completed slots. The hardware half lives behind
 * `#if defined(CH32H417)` in ch32_link.c and is only built for the target.
 */

/* Link diagnostics. Cumulative and monotonic; only ch32_link_reset() clears
 * them. Extends the roadmap plan's seven-field draft by two members forced by
 * the frozen codec, which the draft predates:
 *
 *   bad_type   spi_frame_unpack() gained SPI_FRAME_ERR_TYPE (a well-CRC'd frame
 *              whose type byte is not one of the 15 assigned). Folding it into
 *              bad_crc or bad_length would misattribute a distinct fault.
 *   stale      spi_frame_seq_classify() distinguishes STALE (delta 0x80..0xFF)
 *              from DUPLICATE (delta 0). Dropping a stale frame with no counter
 *              would make a replay storm invisible.
 *
 * A third addition, dma_rearm, is not codec-driven: it is the ISR-side liveness
 * the health-gated watchdog needs. `slots` advances in the foreground (retire),
 * so it proves the poll loop is alive; dma_rearm advances only when the RX DMA
 * ISR re-arms after a completion, so it proves the interrupt half is alive. The
 * two catch different wedges -- a stalled foreground vs. a dead DMA/ISR -- which
 * is exactly why the watchdog scores HEALTH_SPI_PROGRESS and HEALTH_DMA_REARM
 * separately.
 */
typedef struct {
    uint32_t slots;        /* Every 32-byte slot retired from DMA, all outcomes. */
    uint32_t bad_sof;      /* SPI_FRAME_ERR_SOF. */
    uint32_t bad_crc;      /* SPI_FRAME_ERR_CRC. */
    uint32_t bad_length;   /* SPI_FRAME_ERR_LEN. */
    uint32_t bad_type;     /* SPI_FRAME_ERR_TYPE. */
    uint32_t sequence_gap; /* Deliverable frames arriving with a forward gap. */
    uint32_t duplicate;    /* Exact replay of the last sequence (delta 0). */
    uint32_t stale;        /* Backward/out-of-window sequence (delta 0x80..0xFF). */
    uint32_t dma_overrun;  /* A second DMA completion before the prior retired. */
    uint32_t dma_rearm;    /* RX DMA re-armed after a completion (ISR liveness). */
} ch32_link_counters_t;

/* Number of inj_frame_t slots each ring holds. Power of two; the enqueue test is
 * `next = (head + 1) & CH32_LINK_RING_MASK`. */
#define CH32_LINK_RING_SIZE 8u
#define CH32_LINK_RING_MASK (CH32_LINK_RING_SIZE - 1u)

/* Health window. The link exchanges a slot every 125 us, so real progress is
 * sub-millisecond; this is the watchdog's tolerance for a stalled link before
 * ch32_link_healthy() reports unhealthy. */
#define CH32_LINK_HEALTH_TIMEOUT_MS 100u

/* --- Hardware entry points (target only; no-ops are never linked on host). --- */

/* Bring up SPI1 slave mode-0 on PA4..PA7, arm both DMA directions, and release
 * MCU_READY once armed. Calls ch32_link_reset() first. */
void ch32_link_init(void);

/* Foreground service: retire any DMA-completed RX slot, prepare the next TX
 * slot, and flag a second completion before retirement as an overrun. */
void ch32_link_poll(void);

/* --- Pure transport (host-testable). --- */

/* Reset all rings, counters, and sequence/TX state. init() calls this; tests
 * call it to isolate cases. */
void ch32_link_reset(void);

/* Retire one completed 32-byte RX slot: bump `slots`, validate via the codec,
 * and for a deliverable (OK) frame classify its sequence and enqueue it on the
 * RX ring. IDLE keepalives count as a slot but are not sequenced or delivered.
 * The SPI DMA path calls this from ch32_link_poll(); tests call it directly. */
void ch32_link_retire_slot(const uint8_t slot[INJ_FRAME_SIZE]);

/* Pack the next slot to clock out: the oldest queued TX frame, or an IDLE
 * keepalive when the queue is empty. Never fails; always writes 32 bytes. */
void ch32_link_fill_tx_slot(uint8_t slot[INJ_FRAME_SIZE]);

/* Pop the oldest deliverable RX frame. Returns false and leaves *out untouched
 * when the RX ring is empty. */
bool ch32_link_receive(inj_frame_t *out);

/* Queue a frame for transmission. `length` is the exact per-type wire length
 * (0 for IDLE, INJ_FRAME_PAYLOAD_SIZE otherwise). Returns false without
 * enqueuing if the type is unassigned, the length is wrong, or the TX ring is
 * full. The transport assigns the on-wire sequence at fill time. */
bool ch32_link_submit(uint8_t type, const void *payload, uint8_t length);

/* True while the link has retired a fresh slot within CH32_LINK_HEALTH_TIMEOUT_MS
 * of `now_ms`. Self-contained: infers progress from the `slots` counter, so no
 * separate timestamp plumbing is needed. */
bool ch32_link_healthy(uint32_t now_ms);

/* Drop every queued RX and TX frame and reset RX sequence tracking, as required
 * on lease release. Leaves cumulative counters untouched. */
void ch32_link_clear_queues(void);

/* Read-only view of the diagnostics. */
const ch32_link_counters_t *ch32_link_counters(void);

#endif
