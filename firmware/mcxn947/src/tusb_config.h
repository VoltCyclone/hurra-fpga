// TinyUSB configuration for the CPU0 CDC console -- migration step 4 of
// docs/MCXN947_CONTROLLER.md section 9.
//
// One device, one CDC interface, on the ChipIdea High Speed controller behind
// J11. UM12018: "only the HS USB controller and PHY interface is used and it
// is connected to the USB Type-C connector (J11)" -- USB0_FS is routed
// nowhere on this board, so the Full Speed controller is not merely unused,
// it is unusable, and dcd_ci_fs.c is not vendored.
//
// --- rhport, re-read rather than inherited ---------------------------------
//
// Design doc section 10 listed "TinyUSB rhport numbering -- taken from
// ci_hs_mcx.h, not re-read" as an open item. Re-read at this step, and the
// answer is 1, established three independent ways in the pinned tree:
//
//   * vendor/tinyusb/src/common/tusb_mcu.h, under TU_CHECK_MCU(OPT_MCU_MCXN9):
//     "USB0 is chipidea FS" / "USB1 is chipidea HS", then
//     `#define TUP_RHPORT_HIGHSPEED 1`.
//   * vendor/tinyusb/src/portable/chipidea/ci_hs/dcd_ci_hs.c selects
//     ci_hs_mcx.h under a comment reading "MCX N9 only port 1 use this
//     controller".
//   * hw/bsp/mcx/family.c in the same tree routes USB1_HS_IRQHandler to
//     `tusb_int_handler(1, true)`.
//
// The trap worth naming: ci_hs_mcx.h's own CI_HS_REG() *ignores* its port
// argument and always returns controller index 0, and so do
// CI_DCD_INT_ENABLE/DISABLE. So the DCD would physically work with rhport 0
// as well -- the number is not checked where it would fail loudly. What
// depends on getting it right is everything above the DCD: TUD_OPT_RHPORT is
// derived from which CFG_TUSB_RHPORTn_MODE is defined, tusb_int_handler()
// range-checks against TUP_USBIP_CONTROLLER_NUM (2), and the ISR's argument
// must match the port the stack was initialised on. Declaring port 1 keeps
// all of those consistent with the header's own comment.

#ifndef HURRA_MCXN947_TUSB_CONFIG_H
#define HURRA_MCXN947_TUSB_CONFIG_H

#define CFG_TUSB_MCU OPT_MCU_MCXN9
#define CFG_TUSB_OS OPT_OS_NONE

// No RTOS, so the stack runs from the foreground loop plus one ISR. This is
// the placement design doc section 3 argues for: CPU0's link has a 107.93 us
// *staging window*, not a latency deadline, because LPSPI6's 8 x 32-bit FIFO
// holds exactly one 32-byte slot -- so a bulk ISR and tud_task() sharing the
// core with link_poll() cost nothing the link can observe. Step 4's third
// gate is the measurement of that claim.
#define CFG_TUSB_RHPORT1_MODE (OPT_MODE_DEVICE | OPT_MODE_HIGH_SPEED)
#define CFG_TUD_ENABLED 1
#define CFG_TUH_ENABLED 0
#define CFG_TUD_MAX_SPEED OPT_MODE_HIGH_SPEED

// Debug output is off. TinyUSB's TU_LOG routes to printf, and this image links
// nosys.specs -- newlib's _write always fails, so a log call would be dead
// weight at best. The console reports its own state through `stats`.
#define CFG_TUSB_DEBUG 0

// The ChipIdea HS controller is a bus master reading endpoint queue heads and
// transfer descriptors straight out of SRAM. Cortex-M33 on this part has no
// core data cache (design doc section 5), so no maintenance is required; the
// alignment below is what the controller's own addressing needs, and
// dcd_ci_hs.c applies the 2048-byte alignment of the endpoint list itself.
#define CFG_TUD_MEM_SECTION
#define CFG_TUD_MEM_ALIGN TU_ATTR_ALIGNED(4)
#define CFG_TUD_MEM_DCACHE_ENABLE 0

#define CFG_TUD_ENDPOINT0_SIZE 64

#define CFG_TUD_CDC 1
#define CFG_TUD_MSC 0
#define CFG_TUD_HID 0
#define CFG_TUD_MIDI 0
#define CFG_TUD_VENDOR 0

// High Speed bulk endpoints are 512 bytes, so anything smaller would make the
// FIFO the limit rather than the wire and understate the load the third gate
// is supposed to apply. 2 KiB each way is four maximum-size packets of slack
// in both directions.
#define CFG_TUD_CDC_RX_BUFSIZE 2048
#define CFG_TUD_CDC_TX_BUFSIZE 2048
#define CFG_TUD_CDC_EP_BUFSIZE 512

#endif  // HURRA_MCXN947_TUSB_CONFIG_H
