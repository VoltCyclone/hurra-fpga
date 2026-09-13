// V3F supervisor image.
//
// The V3F owns the global clock tree. SYSCLK, HPRE and FPRE are chip-wide RCC
// settings that set BOTH cores' clocks, so SystemInit() runs here and only
// here; the V5F calls only the updater on itself (system_ch32h417.c:258-266
// branches on NVIC_GetCurrentCoreID(), and each core links its own system.o
// into its own address space).
#include "ch32h417_port.h"
#include "trap_witness.h"

int main(void)
{
    trap_witness_clear();

    // Programs the PLL and switches SYSCLK. Until this runs the part is on the
    // hand-rolled 14*HSI/5 = 70 MHz block in core/startup_v3f.S's .load
    // section, not the configured 400/100 MHz profile.
    SystemInit();
    // Reads the resulting RCC registers back into SystemClock / HCLKClock /
    // SystemCoreClock. Nothing else writes HCLKClock, and Delay_Init()
    // (vendor/wch/Debug/debug.c:27) divides by it, so before this call every
    // vendor Delay_Us / Delay_Ms would return immediately.
    SystemAndCoreClockUpdate();

    // Release the V5F. It is hardware-locked at power-on and nothing else in
    // this firmware unlocks it, so until this line the application core -- and
    // with it every driver, the SPI link, and the whole Phase 2 control plane
    // -- has never executed an instruction.
    //
    // One store does both jobs: NVIC_WakeUp_V5F masks the address with
    // ~0x3FF before writing WAKEIP[1] (0xE000E724, RM 4.7.5.52), and bit 0 of
    // that register is SHUTDOWN1, the C1 sleep-lock cancel. Clearing it while
    // programming C1's boot PC unlocks the core and aims it in one write.
    //
    // ORDERING INVARIANT: everything the V5F may observe must be initialised
    // BEFORE this line -- the clock tree and the trap witness above, and every
    // shared ring or buffer Phase 2 adds. New setup goes above this call,
    // never below it. Waking before the clock is programmed would start the
    // V5F at 70 MHz and let it latch a SystemCoreClock that V3F is about to
    // change underneath it.
    NVIC_WakeUp_V5F(Core_V5F_StartAddr);

    for (;;) {
        __WFI();
    }
}
