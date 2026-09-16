// CPU0 platform bring-up. Portable half first; every MMIO access lives in the
// single trailing `#if defined(MCXN947)` block, so a host build of this file
// sees no vendor header at all.

#include "platform.h"

uint32_t platform_systick_reload(uint32_t core_hz, uint32_t tick_hz)
{
    if (tick_hz == 0u || core_hz == 0u) {
        return 0u;
    }
    if ((core_hz % tick_hz) != 0u) {
        // An inexact division would make every timeout derived from the tick
        // wrong by a fixed ratio. Refuse rather than truncate.
        return 0u;
    }

    uint32_t cycles = core_hz / tick_hz;
    if (cycles < 2u || cycles > 0x01000000u) {
        // SysTick LOAD is 24 bits and counts `LOAD + 1` cycles, so 0x1000000
        // cycles is the largest representable period. The lower bound is not
        // cosmetic: ARMv8-M disables the counter when LOAD reads zero, so a
        // one-cycle period would silently stop the tick -- and zero is this
        // function's failure return, so it could not be reported anyway.
        return 0u;
    }
    return cycles - 1u;
}

#if defined(MCXN947)

#include "fsl_clock.h"
#include "fsl_device_registers.h"
#include "fsl_spc.h"

#include "clock_config.h"

// The clock profile is a vendor-generated constant and PLATFORM_CORE_HZ is
// ours. Everything downstream of the link -- the SysTick reload here, and from
// step 2 the LPSPI6 baud divider -- is derived from ours, so a regenerated
// clock_config.c that moves the profile must fail the build, not go unnoticed.
_Static_assert(PLATFORM_CORE_HZ == BOARD_BOOTCLOCKPLL150M_CORE_CLOCK,
               "PLATFORM_CORE_HZ disagrees with the vendor clock profile");

// Reimplements the SDK's BOARD_PowerMode_OD() (boards/frdmmcxn947/
// project_template/board.c) rather than vendoring board.c, which drags in the
// debug console, LPI2C and LP_FLEXCOMM for two register writes. Recorded as a
// deviation in PROVENANCE.md.
static void platform_power_mode_overdrive(void)
{
    spc_active_mode_dcdc_option_t dcdc = {
        .DCDCVoltage = kSPC_DCDC_OverdriveVoltage,
        .DCDCDriveStrength = kSPC_DCDC_NormalDriveStrength,
    };
    (void)SPC_SetActiveModeDCDCRegulatorConfig(SPC0, &dcdc);

    spc_sram_voltage_config_t sram = {
        .operateVoltage = kSPC_sramOperateAt1P2V,
        .requestVoltageUpdate = true,
    };
    (void)SPC_SetSRAMOperateVoltage(SPC0, &sram);
}

void platform_init(void)
{
    // Order is load-bearing and matches the design doc's boot ladder: the core
    // voltage must already be at overdrive before the PLL raises the core
    // clock to 150 MHz, and BOARD_BootClockPLL150M() itself programs the flash
    // wait states for kOD_Mode.
    platform_power_mode_overdrive();
    BOARD_BootClockPLL150M();

    uint32_t reload = platform_systick_reload(PLATFORM_CORE_HZ, PLATFORM_TICK_HZ);
    // platform_systick_reload() is a pure function of two compile-time
    // constants, so a zero here is a programming error, not a runtime
    // condition. Spin rather than run on an unknown tick rate.
    while (reload == 0u) {
    }

    SysTick->LOAD = reload;
    SysTick->VAL = 0u;
    SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk | SysTick_CTRL_TICKINT_Msk | SysTick_CTRL_ENABLE_Msk;
}

#endif  // MCXN947
