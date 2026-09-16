// CPU0 platform bring-up: core voltage mode, clock tree, and the SysTick tick.
//
// Declarations here are portable C -- only the *definitions* in platform.c are
// guarded by `#if defined(MCXN947)`. That keeps a caller (main_core0.c) free of
// the guard while every MMIO access stays behind it.

#ifndef HURRA_MCXN947_PLATFORM_H
#define HURRA_MCXN947_PLATFORM_H

#include <stdint.h>

// Core clock the part is taken to by platform_init(). Overdrive is load-bearing
// and not a power-optimisation knob: LPSPI6-9's 30 MHz slave TX ceiling (LP1)
// is quoted per drive mode, and Standard/Mid Drive would take the link below
// the 15 MHz SCK it has to carry from step 2 onwards.
//
// platform.c static-asserts this against the vendor's own
// BOARD_BOOTCLOCKPLL150M_CORE_CLOCK, so the two cannot drift.
#define PLATFORM_CORE_HZ 150000000u

// SysTick rate. 1 kHz keeps the reload inside SysTick's 24-bit LOAD at any
// core clock this part supports.
#define PLATFORM_TICK_HZ 1000u

// SysTick LOAD value for `core_hz / tick_hz`, or 0 when the division is not
// exact or the result does not fit SysTick's 24-bit reload. Returning 0 rather
// than truncating keeps a bad clock/tick pair a build-visible failure instead
// of a silently wrong tick rate. Portable; host-tested.
uint32_t platform_systick_reload(uint32_t core_hz, uint32_t tick_hz);

// SPC overdrive -> BOARD_BootClockPLL150M() -> flash wait states -> SysTick.
// Defined only for the target.
void platform_init(void);

#endif  // HURRA_MCXN947_PLATFORM_H
