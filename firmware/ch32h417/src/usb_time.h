#ifndef USB_TIME_H
#define USB_TIME_H

#include <stdbool.h>
#include <stdint.h>

/* USB-frame time base derived from the FPGA's USB_SYNC pulse.
 *
 * The FPGA drives one USB_SYNC edge per USB frame onto PC6/TIM8_CH1. This
 * module captures those edges into a free-running 32-bit tick base (a 16-bit
 * TIM8 extended with an overflow high word) and exposes a 16-bit USB frame
 * counter for scheduling injections against the host's own frame clock.
 *
 * As with the SPI link, the timing/sync logic is MMIO-free so it host-compiles
 * and is driven directly by test/usb_time_test.c with fabricated captures and
 * clock readings; the TIM8 capture/overflow ISRs live behind
 * `#if defined(CH32H417)` and only build for the target. */

typedef struct {
    uint16_t frame;        /* Frames seen since reset; increments per captured SOF. */
    uint32_t capture_ticks; /* Tick of the most recent SOF capture. */
    uint32_t period_ticks;  /* Filtered inter-SOF period, in tick units. */
    bool synchronized;      /* True once a period is locked and not yet lost. */
} usb_time_snapshot_t;

/* Consecutive missed SOFs that drop synchronization. Three frames with no edge
 * means the FPGA stopped driving USB_SYNC (link down or host detached). */
#define USB_TIME_SYNC_LOSS_FRAMES 3u

/* --- Hardware entry points (target only). --- */

/* Configure PC6/TIM8_CH1 rising-edge capture and the TIM8 overflow counter, and
 * route both IRQs to the V5F. Calls usb_time_reset() first. */
void usb_time_init(void);

/* Foreground service: sample the live 32-bit tick and check for SOF loss. */
void usb_time_poll(void);

/* --- Pure timing/sync core (host-testable). --- */

/* Reset the frame counter, period filter, and sync state. */
void usb_time_reset(void);

/* Record one captured SOF at `capture_ticks`. The capture ISR calls this; tests
 * call it directly. Advances the frame counter, updates the period filter, and
 * (re)acquires synchronization. A capture after a loss restarts the filter
 * rather than folding the outage gap into the period estimate. */
void usb_time_on_capture(uint32_t capture_ticks);

/* Check for SOF loss against the current tick `now_ticks`. poll() calls this
 * with the live counter; tests call it to advance simulated time. */
void usb_time_poll_at(uint32_t now_ticks);

/* Coherent read of the current frame/period/sync state. */
usb_time_snapshot_t usb_time_snapshot(void);

/* Wrap-aware "has frame `now` reached or passed `target`?" over the 16-bit USB
 * frame space (RFC 1982 serial arithmetic): true when `now` is 0..0x7FFF ahead
 * of `target`. Pure; used to decide whether a scheduled frame is due. */
bool usb_time_reached(uint16_t now, uint16_t target);

#endif
