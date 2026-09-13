#ifndef WS2812_H
#define WS2812_H

#include <stdint.h>

/* WS2812 output over the CH32H417 PIOC (PF13/IO1). This is the low-level drive
 * only -- PIOC bringup and a non-blocking GRB push; the status layer owns color
 * and animation and computes the frame this sends. Both entry points are
 * MMIO-only and defined solely for the target build. */

/* Minimum interval between pushes (~60 Hz cap). The status layer owns the
 * throttle clock; this is the shared floor. */
#define WS2812_MIN_INTERVAL_MS 16u

/* Bring up PIOC on PF13/IO1 and load the WS2812 waveform program. V5F-only;
 * call once before the service loop. */
void ws2812_init(void);

/* Push one 3-byte GRB frame. Non-blocking: drops the frame if PIOC has not
 * finished the previous one, never spins. */
void ws2812_send_grb(const uint8_t grb[3]);

#endif
