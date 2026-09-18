// CPU1's presentation-only status LED: portable slot-counter policy plus a
// target-only blue GPIO implementation in status_led.c.

#ifndef HURRA_MCXN947_STATUS_LED_H
#define HURRA_MCXN947_STATUS_LED_H

#include <stdbool.h>
#include <stdint.h>

// Why this is a rhythm and not a fade.
//
// The *output* rate bounds this animation, not the slot rate. CPU1 calls
// status_led_hw_set() once per successful snapshot read, which is once per
// CORE1_SNAPSHOT_HEARTBEATS (36) heartbeats -- not once per loop iteration.
// PROVENANCE.md's step-5 bench table measured that loop at 720 heartbeats/s,
// so the LED is rewritten about 20 times a second and no more. Note that
// link_snapshot_read() succeeds on any stable seqlock read rather than only on
// fresh data, so the 20 Hz holds even while CPU0 publishes nothing new.
//
// 20 Hz is far below flicker fusion, so software PWM here would read as a
// strobe rather than as brightness. The achievable output is one bit every
// 50 ms: no intermediate levels exist to fade through. A smooth breathing fade
// needs the pin written some hundreds of times a second, which means hoisting
// the call out of the snapshot branch into the heartbeat loop -- a
// main_core1.c change, deliberately not made here.
//
// What one bit every 50 ms does buy is rhythm. This is a two-beat cardiac
// pulse: a long beat, a short gap, a shorter beat, then a long rest. Every
// segment is at least STATUS_LED_MIN_SEGMENT_SLOTS long, so each one is
// sampled at least three times at 20 Hz and still renders if that rate falls
// by 3x -- the two beats cannot alias into one, and neither can vanish.

// One animation cycle, in retired slots. Slots retire at 8000/s (125 us each;
// measured 7995.75/s on the bench), so this is 2.048 s -- about 29 double
// beats a minute. A power of two, and a divisor of 2^32, so the phase mask is
// exact and the rhythm does not stutter when slot_counter wraps.
#define STATUS_LED_CYCLE_SLOTS 16384u

// Segment boundaries, as offsets into one cycle.
#define STATUS_LED_BEAT1_END 2048u  // 0..2047:    256 ms on   (first beat)
#define STATUS_LED_GAP_END 3584u    // 2048..3583: 192 ms off  (between beats)
#define STATUS_LED_BEAT2_END 5120u  // 3584..5119: 192 ms on   (second beat)
                                    // 5120..:    1408 ms off  (rest)

// The shortest segment above, and so the sampling margin: an update interval
// up to this many slots (192 ms, i.e. a rate down to ~5.2 Hz) still renders
// every segment at least once.
#define STATUS_LED_MIN_SEGMENT_SLOTS 1536u

// Slots lit per cycle: 2048 + 1536. The rest dominates, so the LED is off for
// about 78% of the cycle. That matters for stalls: the policy is a pure
// function of slot_counter, so when slots stop advancing the LED holds one
// level and visibly stops animating instead of continuing to blink -- and a
// stall parks it dark far more often than at a misleading steady on.
#define STATUS_LED_ON_SLOTS_PER_CYCLE 3584u

bool status_led_for_slot_counter(uint32_t slot_counter);

void status_led_hw_init(void);
void status_led_hw_set(bool on);

#endif  // HURRA_MCXN947_STATUS_LED_H
