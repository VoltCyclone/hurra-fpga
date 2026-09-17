// CPU1 reset release. Arithmetic stays portable; only the trailing target
// implementation may touch SYSCON.

#ifndef HURRA_MCXN947_CORE1_RELEASE_H
#define HURRA_MCXN947_CORE1_RELEASE_H

#include <stdbool.h>
#include <stdint.h>

#define CORE1_SYSCON_BASE 0x50000000u
#define CORE1_CPUCTRL_OFFSET 0x00000800u
#define CORE1_CPBOOT_OFFSET 0x00000804u
#define CORE1_CPUCTRL_KEY (0x0000C0C4u << 16)
#define CORE1_CPUCTRL_CLK_ENA (1u << 3)
#define CORE1_CPUCTRL_RESET_ENA (1u << 5)

// CPU1's flash window, and the RAM its stack must live in. Both come from
// MCXN947_cm33_core1_flash.ld: m_interrupts ORIGIN + m_text LENGTH, and
// m_data ORIGIN + LENGTH.
#define CORE1_FLASH_START 0x000C0000u
#define CORE1_FLASH_END 0x00100000u
#define CORE1_RAM_START 0x2004E000u
#define CORE1_RAM_END 0x20068000u

uint32_t core1_cpuctrl_assert_reset(uint32_t cpuctrl_now);
uint32_t core1_cpuctrl_release(uint32_t cpuctrl_now);

// Does the first vector pair at CORE1_FLASH_START look like a real image?
//
// MEASURED, and the reason this predicate exists at all. With the core1 region
// erased, both words read 0xFFFFFFFF; releasing CPU1 onto that makes it fault
// on its first instruction fetch, escalate to LOCKUP, and reset the WHOLE CHIP
// -- CPU0's live link with it. Design doc section 4 claims an unflashed CPU1
// leaves the link running; on this part, releasing it unconditionally is what
// makes that claim false. Section 9 step 5 configuration (a) boot-looped until
// this check existed.
//
// Portable and host-tested. This is a validity check on a flash image, NOT a
// handshake with CPU1: it reads two words of flash and never waits for
// anything, so section 8's objection to MCMgr does not apply.
bool core1_image_valid(uint32_t initial_msp, uint32_t reset_vector);

typedef enum {
    CORE1_RELEASED = 0,
    CORE1_SKIPPED_NO_IMAGE = 1,
    CORE1_HELD_IN_RESET = 2,
} core1_release_status_t;

// What the last core1_release() call did. Portable state so the console can
// report it without reaching into guarded code: a skipped release must be
// VISIBLE. A silent skip is the same failure as a silent release -- a working
// link and a black screen with nothing anywhere saying why.
core1_release_status_t core1_release_last_status(void);

#if defined(MCXN947)
core1_release_status_t core1_release(void);

// Put CPU1 back into reset and leave it there. CPU0 owns this reset line -- it
// is the same register it used to let CPU1 go -- so this is not a command TO
// CPU1 and does not breach section 3's one-way, data-only IPC rule. Nothing
// here waits for CPU1, which is the property that matters.
//
// It exists because section 9 step 5 configuration (c) needs CPU1 stopped
// mid-execution on demand, and LinkServer's gdbserver could not do that
// repeatably on this board. Stopping CPU1 from CPU0 is deterministic, needs no
// debugger, and is a harsher test than a debugger halt: reset is asynchronous
// and can land mid-store to the shared window.
void core1_halt(void);
#endif

#endif  // HURRA_MCXN947_CORE1_RELEASE_H
