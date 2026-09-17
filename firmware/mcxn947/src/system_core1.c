// Deliberately smaller than the SDK's weak core1 SystemInit(). See
// PROVENANCE.md: chip-wide SYSCON/SPC/GDET/ITRC policy belongs to CPU0 and has
// already run before this image is released.

#include <stdint.h>

#include "fsl_device_registers.h"

uint32_t SystemCoreClock = DEFAULT_SYSTEM_CLOCK;

void SystemInit(void)
{
    // CP0/CP1 are core-private PowerQuad access controls. CPU1 needs no other
    // architectural or peripheral state in migration step 5.
    SCB->CPACR |= 0xFu;

    extern void *__Vectors;
    SCB->VTOR = (uint32_t)(uintptr_t)&__Vectors;
}
