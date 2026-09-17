# MCXN947 Firmware Provenance

Target: **FRDM-MCXN947** (MCXN947VDF, dual Cortex-M33), replacing the CH32H417 on
the PMOD-A injection link. Design: `docs/MCXN947_CONTROLLER.md`.

**This tree implements migration step 6 of that document's section 9** — two
images: clock, blink, LPSPI6 as an SPI slave on LP_FLEXCOMM6 driven by a
self-loading eDMA0 scatter-gather ring that transmits a permanently IDLE slot,
retirement of every received slot through `src/link_retire.c`, ERR051588
detection and recovery through `src/link_recovery.c`, and a TinyUSB CDC console
on the ChipIdea High Speed controller behind J11. CPU1 is released last, reads
CPU0's seqlock snapshot at about 20 Hz, echoes the slot count it actually saw,
and drives only the blue LED from slot-counter bit 11; there is no display.
What is *absent* is recorded here too, because "we did not vendor it yet" and
"we decided not to vendor it" are different claims.

**Step 5 hardware acceptance is complete.** Section 9's three configurations
were measured on the bench on 2026-09-16, acceptance taken from the FPGA's own
registers over JTAG, each over a window rather than as a single reading:

| configuration | slots/s | gated FPGA counters | CPU1 |
|---|---|---|---|
| (a) core1 region erased, blank-check confirmed | 7999.97 | all +0 over 42.9 s | not released, reported |
| (b) both images flashed | 7995.75 | all +0 over 42.9 s | alive, 720 heartbeats/s |
| (c) both flashed, CPU1 held in reset mid-execution | 7999.92 | all +0 over 42.9 s | frozen, heartbeat delta 0 |

`spi_bad_sof`, `spi_bad_crc`, `spi_bad_length`, `spi_bad_type` and
`spi_queue_full` were flat in all three, `link_losses` did not move, and no
counter on the MCU's own console grew during any window. Configuration (a)
**failed on the first attempt** — see the boot-loop finding below — and passes
only because of `core1_image_valid()`.

Everything under `vendor/` is imported unmodified and is exempted from the
repository whitespace gate by `.gitattributes`. Do not reformat it.

---

## Upstream 1 — MCUXpresso SDK for FRDM-MCXN947

- **Version: 24.12.00**, revision `871`, dated 2025-01-14. Read from the
  package's own manifest, `FRDM-MCXN947_manifest_v3_15.xml`:
  `<ksdk id="MCUXpresso241200" version="24.12.00" revision="871 2025-01-14"/>`.
  The manifest's own `sdk repo commit` field reads literally `TODO`, so there is
  **no upstream git commit to record** — the version and revision above are the
  whole of the identification NXP ships.
- **Licence: BSD-3-Clause** (`vendor/mcux-sdk/COPYING-BSD-3`). CMSIS carries
  Apache-2.0 (`vendor/mcux-sdk/CMSIS/LICENSE.txt`).
- **Copied from:** `~/mcuxpresso/02/SDKPackages/SDK_24_12_00_FRDM-MCXN947-2`
  on the development machine. Not fetched; the package was already unpacked.

Imported unchanged:

| local path | upstream path |
|---|---|
| `vendor/mcux-sdk/CMSIS/Core/Include/` | `CMSIS/Core/Include/` |
| `vendor/mcux-sdk/CMSIS/LICENSE.txt` | `CMSIS/LICENSE.txt` |
| `vendor/mcux-sdk/COPYING-BSD-3` | `COPYING-BSD-3` |
| `vendor/mcux-sdk/devices/MCXN947/periph/` | `devices/MCXN947/periph/` (78 headers) |
| `vendor/mcux-sdk/devices/MCXN947/MCXN947_cm33_core0{,_COMMON,_features}.h` | same |
| `vendor/mcux-sdk/devices/MCXN947/MCXN947_cm33_core1{,_COMMON,_features}.h` | same |
| `vendor/mcux-sdk/devices/MCXN947/fsl_device_registers.h` | same |
| `vendor/mcux-sdk/devices/MCXN947/system_MCXN947_cm33_core0.{c,h}` | same |
| `vendor/mcux-sdk/devices/MCXN947/system_MCXN947_cm33_core1.{c,h}` | same (**reference only; deviation 12**) |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_{common,common_arm,reset,port}.h` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_{clock,spc,gpio}.{c,h}` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_reset.c` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_lpflexcomm.{c,h}` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_edma.{c,h}`, `fsl_edma_core.h`, `fsl_edma_soc.h` | `devices/MCXN947/drivers/` (`fsl_edma_soc.c` **removed at step 3** — see deviation 8) |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_lpspi.h` | `devices/MCXN947/drivers/` (**header only** — see deviation 4) |
| `vendor/mcux-sdk/boards/frdmmcxn947/project_template/{clock_config.c,clock_config.h,board.h}` | same |

`periph/` is taken whole, not pruned: `MCXN947_cm33_core0.h` includes all 78
`PERI_*.h` unconditionally, so removing one would mean editing a generated
header and forfeiting byte-exactness. It is ~4.8 MB of headers; that is the
price of the guarantee.

### Excluded from upstream 1, and why

- **`fsl_common.c`, `fsl_common_arm.c`.** Their *headers* are required
  (`fsl_common.h` includes `fsl_common_arm.h`, which includes `fsl_reset.h`),
  but nothing here calls `SDK_Malloc`, `SDK_DelayAtLeastUs` or
  `InstallIRQHandler`, and the image links without them. Both were copied in
  while step 2 was being built and then deleted again once the link proved it
  did not reference either — an unused vendored file is a file nobody has read.
  Add them back when a step first needs one.

  `fsl_reset.c` **is** now imported: `LP_FLEXCOMM_Init()` calls
  `RESET_ClearPeripheralReset()` to bring FlexComm6 out of reset.
- **`board.c`, `pin_mux.c`, `peripherals.c`** from `project_template`.
  `board.c` pulls in `fsl_debug_console.h`, `fsl_lpi2c.h` and
  `fsl_lpflexcomm.h` for what step 1 needs two register writes of; see the
  deviation below. `pin_mux.c` configures the camera, the display and the
  Arduino headers as well as the LEDs. `board.h` **is** imported, because it is
  the authoritative record of the FRDM LED pinout.
- **Every driver the design lists for a *later* step** — `fsl_ctimer`,
  `fsl_wwdt`, `fsl_inputmux*`, `fsl_flexio*`, `fsl_cache`, `fsl_mailbox`,
  `fsl_sema42`. An unused vendored driver is a file nobody has read; each
  arrives with the step that calls it. `fsl_edma*` and `fsl_lpflexcomm` left
  this list at step 2, which is the step that calls them.
- **`fsl_lpspi.c` and `fsl_lpspi_edma.c`** — excluded deliberately, not
  deferred; only `fsl_lpspi.h` is imported. See deviation 4.
- **`fsl_edma_soc.c`** — imported at step 2, **removed at step 3**. Only
  `fsl_edma_soc.h` remains. See deviation 8; it is a collision, not a
  preference.
- **`middleware/usb/phy/usb_phy.{c,h}` — REJECTED at step 4, not deferred.**
  Design doc section 7 lists it as "the only file from `middleware/usb/`". It
  cannot be that, and the dependency chain was measured rather than guessed:
  `usb_phy.c`'s first include is `usb.h`, which it needs for three enumerators
  (`kUSB_ControllerEhci0`, `kStatus_USB_Success`, `kStatus_USB_Error`), plus
  `kUSB_ControllerIp3516Hs0/1` and `kUSB_ControllerLpcIp3511Hs0/1` in a branch
  that is dead on this part. `middleware/usb/include/usb.h` in turn includes
  `usb_misc.h`, `usb_spec.h` and `fsl_os_abstraction.h` — 944 lines of the NXP
  USB stack's common headers and the OSA layer, both of which the same section
  excludes by name. So vendoring "one file" would in fact vendor four headers
  plus an OS abstraction, to obtain roughly forty lines of `USBPHY` register
  writes.

  Those writes are instead in `usb_console_hardware_init()` in
  `src/usb_console.c`. See deviation 9 for why that is defensible rather than
  a shortcut: two independent upstreams state the same sequence register for
  register.
- **`boot_multicore_slave.c`** — rejected at step 5 rather than imported; see
  deviation 13. The ST7796S / DBI / FlexIO display stack remains deferred to
  step 7.
- **`fsl_dbi_flexio_smartdma`** — rejected outright, not deferred. SmartDMA is a
  third bus master and admitting it would put an engine nobody has reasoned
  about against the link's arbitration budget (design doc section 7).
- **The NXP USB device stack, FreeRTOS, lwIP, mbedTLS, LVGL/emWin, MCMgr.**
  Rejected in the design; MCMgr specifically because
  `MCMGR_StartCore(kMCMGR_Start_Synchronous)` waits for CPU1, which is the one
  thing the safety invariant forbids on CPU0's boot path.

---

## Upstream 2 — NXP hal_nxp (mcux-sdk classic layout)

- **Commit `9dc7449014a7380355612453b31be479cb3a6833`** (2025-02-25,
  "hal_nxp: Include LP Flexcomm driver using the right Kconfig"). This is the
  `hal_nxp` revision pinned by the local Zephyr workspace's `west.yml`.
- **Licence: BSD-3-Clause** (headers of the imported files).
- **Copied from:**
  `~/zephyrproject/modules/hal/nxp/mcux/mcux-sdk/devices/MCXN947/gcc/`.

Imported unchanged into `vendor/hal_nxp/devices/MCXN947/gcc/`:

- `MCXN947_cm33_core0_flash.ld`
- `MCXN947_cm33_core1_flash.ld`
- `startup_MCXN947_cm33_core0.S`
- `startup_MCXN947_cm33_core1.S`

**Why a second upstream at all — verified, not assumed.** SDK 24.12.00 ships no
GNU linker script and no `.S` startup for this part. `find` over the whole
unpacked package returns `.ld` files only under `middleware/mcuboot_opensource`
and `middleware/tfm`, none for MCXN947, and `devices/MCXN947/` has no `gcc/`
directory at all — only `mcuxpresso/startup_mcxn947_cm33_core{0,1}.{c,cpp}`,
which are the MCUXpresso-IDE managed-linker startups. The design doc's section 7
claim is confirmed.

**The core1 pair is linked by migration step 5.** It forms a separate ELF at
0x000C0000; no embedded-blob section from the core0 linker script is used.

### Mixing the two upstreams is safe, and this is the check that says so

hal_nxp at this commit is a **different SDK vintage** from 24.12.00 — its
`MCXN947_cm33_core0.h` is a 4.6 MB monolith where 24.12.00's is a 3.4 KB shim
over `periph/`, and its `fsl_clock.{c,h}` differ by thousands of lines. So only
the four `gcc/` files are taken from it; every header and driver comes from the
SDK. The question that leaves open is whether the hal_nxp vector table still
agrees with 24.12.00's `IRQn_Type` enum, because a startup file from the wrong
vintage would put handlers in the wrong NVIC slots and nothing would say so.

Checked by diffing the hal_nxp `.S` vector block against SDK 24.12.00's own
`mcuxpresso/startup_mcxn947_cm33_core0.c` `g_pfnVectors[]`: **172 entries, same
order, no slot inserted or removed.** Four spot-checks of slot index against the
SDK's `IRQn_Type` all agree — `EDMA_0_CH0` 1, `CTIMER2` 34, `LP_FLEXCOMM6` 41,
`QDC0_COMPARE` 124.

Three naming divergences, all at slots this firmware does not use:

- `Reset_Handler` (hal_nxp) vs `ResetISR` (MCUXpresso managed-linker startup).
  The vendored `.ld` has `ENTRY(Reset_Handler)`, so the pair is self-consistent.
- Slots 123-130: hal_nxp names them `ENC0_*` / `ENC1_*`; 24.12.00 renamed the
  peripheral to QDC, so the NVIC enum reads `QDC0_COMPARE_IRQn` while the
  handler symbol to define is `ENC0_COMPARE_IRQHandler`. **Whoever first needs a
  QDC interrupt must define the `ENC*` name**, not the one the enum suggests.
- Slots 165-166: hal_nxp has `SM3_IRQHandler` / `TRNG0_IRQHandler`; 24.12.00 has
  them reserved.

---

## Upstream 3 — hathach/tinyusb

- **Tag `0.20.0`**, commit `3af1bec1a9161ee8dec29487831f7ac7ade9e189`,
  2025-11-20. A real release tag, as design doc section 7 requires — not the
  commit pin its fallback wording allows for.
- **Licence: MIT** (`vendor/tinyusb/LICENSE`). **This is the third licence in
  this tree**, alongside the SDK's BSD-3-Clause and CMSIS's Apache-2.0. Any
  distribution of this firmware now has to carry all three.
- **Copied from:** the local clone at `~/code/tinyusb`, read-only, with
  `git archive`. That clone's working tree was on `0.18.0-309-g2a364ca27` — a
  mid-development merge, not a release — and it was left exactly where it was.
  No checkout, no worktree, no fetch.

### Why 0.20.0

It is the newest release tag in that clone (0.19.0 is 2025-10-06, 0.20.0 is
2025-11-20) and the clone's own HEAD is an ancestor of it, so pinning forward
to it is a fast-forward rather than a jump onto an unrelated line. All three
tags carry `src/portable/chipidea/ci_hs/ci_hs_mcx.h`, so MCX HS support is not
what distinguishes them; taking the newest release is simply the fewest known
bugs. Nothing in this firmware depends on a 0.20.0-only API.

### The extraction, reproducibly

Run from anywhere; `$TUSB` is any clone that has the tag. It never writes to
the clone.

```sh
TUSB=~/code/tinyusb
cd firmware/mcxn947 && mkdir -p vendor/tinyusb
git -C "$TUSB" archive --format=tar 0.20.0 \
  LICENSE \
  src/tusb.c src/tusb.h src/tusb_option.h \
  src/common/tusb_common.h src/common/tusb_compiler.h src/common/tusb_debug.h \
  src/common/tusb_fifo.c src/common/tusb_fifo.h src/common/tusb_mcu.h \
  src/common/tusb_private.h src/common/tusb_types.h src/common/tusb_verify.h \
  src/device/dcd.h src/device/usbd.c src/device/usbd.h \
  src/device/usbd_control.c src/device/usbd_pvt.h \
  src/class/cdc/cdc.h src/class/cdc/cdc_device.c src/class/cdc/cdc_device.h \
  src/osal/osal.h src/osal/osal_none.h \
  src/portable/chipidea/ci_hs/ci_hs_type.h \
  src/portable/chipidea/ci_hs/ci_hs_mcx.h \
  src/portable/chipidea/ci_hs/dcd_ci_hs.c \
  | tar -x -C vendor/tinyusb
```

26 files, ~460 KB. The whole of `src/` is 182 files and 3.5 MB; a subset is
taken for the same reason the SDK's drivers are — an unused vendored file is a
file nobody has read — and unlike the SDK's `periph/`, nothing here is a
generated header whose includes would break if pruned. Every include in the
subset is either present or behind a `CFG_TU*` that `src/tusb_config.h` sets
to 0.

### Excluded from upstream 3, and why

- **`src/portable/chipidea/ci_fs/`** — the Full Speed device controller.
  Rejected, not deferred. UM12018: on FRDM-MCXN947 "only the HS USB controller
  and PHY interface is used and it is connected to the USB Type-C connector
  (J11)"; `USB0_FS` is routed to nothing. Note that `tusb_mcu.h` defines *both*
  `TUP_USBIP_CHIPIDEA_FS` and `TUP_USBIP_CHIPIDEA_HS` for `OPT_MCU_MCXN9`, so
  vendoring `dcd_ci_fs.c` as well would compile a second `dcd_init()` and the
  link would fail — which is the good outcome, but it is worth knowing the
  exclusion is load-bearing rather than tidiness.
- **The host stack** (`src/host/`, `hcd_ci_hs.c`, every `*_host.c`) — this is a
  device, and the FPGA owns the host role in this product.
- **Every class except CDC** — HID, MSC, MIDI, audio, video, net, DFU, vendor,
  bth, usbtmc, mtp. `CFG_TUD_*` is 0 for all of them, so they would compile to
  nothing; they are absent so that nobody has to check.
- **`src/typec/`** — USB-PD. The board's Type-C CC logic is a separate
  PTN5150A in DRP mode (UM12018 Table 11); the MCU does not participate.
- **All OSAL backends except `osal_none.h`** — no RTOS on CPU0.
- **`hw/`, `examples/`, `tools/`, `docs/`, `test/`** — the whole of TinyUSB's
  own build and board-support tree. `hw/bsp/mcx/family.c` was *read* while
  writing `usb_console_hardware_init()` (deviation 9) and deliberately not
  imported: it is board glue for TinyUSB's example build system, it configures
  LEDs and a UART this firmware already owns, and it would arrive with
  `board.h`, `pin_mux.c` and `clock_config.c` for a different clock profile
  than ours.

---

## Local deviations

Each is a change *we* made, or a vendor behaviour we deliberately did not adopt.

1. **`BOARD_PowerMode_OD()` is reimplemented in `src/platform.c`, not vendored.**
   The SDK's version lives in `boards/frdmmcxn947/project_template/board.c`
   (line 232) and is ten lines: `SPC_SetActiveModeDCDCRegulatorConfig` to
   `kSPC_DCDC_OverdriveVoltage` / `kSPC_DCDC_NormalDriveStrength`, then
   `SPC_SetSRAMOperateVoltage` to `kSPC_sramOperateAt1P2V` with
   `requestVoltageUpdate`. `board.c` as a whole includes `fsl_debug_console.h`,
   `fsl_lpi2c.h` and `fsl_lpflexcomm.h` and carries the camera, codec and
   accelerometer I2C helpers — none of which step 1 has any use for.
   `platform_power_mode_overdrive()` performs exactly the same two calls with
   the same arguments. If `board.c` is ever vendored for another reason, delete
   ours and call theirs.

2. **`-Wl,--no-warn-rwx-segments`.** The vendored linker script places `.data`
   with `AT(__DATA_ROM)`, so its load address in `m_text` (RX) and its virtual
   address in `m_data` (RW) end up in one LOAD segment, which `ld` 14.2.1
   reports as RWX on every link. The script is byte-exact and the layout is
   deliberate, so the flag is suppressed at the link rather than the script
   being patched.

3. **`-Wl,--defsym,__use_shmem__=1`.** Both vendored scripts already declare
   `RPMSG_SHMEM_SIZE = DEFINED(__use_shmem__) ? 0x2000 : 0`, reserve
   `rpmsg_sh_mem` at `0x2004E000 - RPMSG_SHMEM_SIZE`, exclude it from `m_data`,
   and emit a `.noinit_rpmsg_sh_mem (NOLOAD)` section. We set the symbol to
   reuse **RPMsg's address reservation, not RPMsg** — the CPU0 -> CPU1 snapshot
   goes there at step 6, and reserving it from step 1 stops CPU0's data growing
   into it in the meantime. Both `.ld` files stay byte-exact.
   `make check`'s `shared-window-reserved` rung asserts the defsym survived.

4. **`fsl_lpspi.c` is not vendored at all; only `fsl_lpspi.h` is, and the LPSPI
   slave bring-up is hand-written in `src/link.c`.** The design doc asked for
   the ERR051588 half of this to be recorded, and step 2 settles the rest.

   `LPSPI_SlaveInit()` is close to what we want but not close enough to adopt:
   it writes `TCR` wholesale from four fields (`CPOL | CPHA | LSBF | FRAMESZ`),
   which clears `TCR[BYSW]`, and it ends by enabling the module — so using it
   would mean calling it, disabling the module again, rewriting `TCR`, and
   re-enabling, for no gain over the fifteen register writes `link_spi_init()`
   performs in a single documented order. Excluding the `.c` also keeps its
   transactional API, its handles and its interrupt dispatch out of a tree whose
   stated priority is that the guarded-line count goes down.

   The ERR051588 consequence stands as recorded: the driver *does* reset the
   transmit FIFO, but only inside that transactional slave API, so no workaround
   comes for free. The recovery ladder in design doc section 4 is ours.
   `link_mcu_ready_set()` is split out of `link_init()` precisely so that
   ladder's first and last rungs already exist.

5. **`include/` is on the include path as `-isystem`, not `-I`.**
   `injection_wire.h` is generated from `protocol/report_injection_wire.json`
   by `tools/generate_report_injection_wire.py` and carries a do-not-edit
   header; it does not survive `-Wconversion` (`return (uint16_t)data[0] |
   ((uint16_t)data[1] << 8)` promotes to `int`). Warning about code we are
   forbidden to edit would only invite someone to edit it. The CH32 tree made
   the same call for the same file.

6. **`src/spi_frame.{c,h}`, `test/spi_frame_test.c` and
   `include/injection_wire.h` are byte-exact copies of the CH32 originals**, so
   the two controllers can be diffed against each other until step 10 retires
   `firmware/ch32h417/`. `spi_frame.c` is 141 lines with no MMIO and includes
   only `spi_frame.h` and `<stddef.h>`, so it ports unchanged rather than being
   reimplemented — the CRC-16, slot pack/unpack and sequence classification are
   hand-written per language and a second hand-written C copy would be a third
   implementation to keep in step, not a second.

   The claim is *enforced*, not asserted: `make check` runs `check-copies`,
   which `cmp`s all four files against `../ch32h417/`. A drifting copy is
   exactly how the two ends of a hand-written codec silently disagree, and
   `tests/test_wire_cross_language.py` still compiles the **CH32** tree
   (`FIRMWARE_ROOT = REPO_ROOT / "firmware" / "ch32h417"`), so nothing else in
   the repository would notice a divergence here.

7. **`fsl_edma_soc.c` is not vendored; only `fsl_edma_soc.h` is.** Removed at
   step 3, and not as tidying — it made the link fail.

   The file defines **nothing but** 32 strong
   `EDMA_<n>_CH<m>_DriverIRQHandler` wrappers around
   `EDMA_DriverIRQHandler(instance, channel)`, which dispatches through the
   transactional handle array `s_EDMAHandle[][]`. Verified with `nm`: every
   symbol it defines is one of those 32, and its only undefined reference is
   `EDMA_DriverIRQHandler` itself. Nothing else in the tree needs it —
   `EDMA_SetChannelMux` and the rest are `static inline` in `fsl_edma.h`.

   Step 3's RX retirement ISR must be named `EDMA_0_CH1_DriverIRQHandler`,
   because the vector-table entry `EDMA_0_CH1_IRQHandler` is a weak trampoline
   that branches to the Driver name and it is the Driver name that
   `startup_MCXN947_cm33_core0.S` `.set`s to `DefaultISR` (see the step-1
   findings below). The vendor's strong definition of that same symbol makes
   the link fail outright — which is the good outcome. Taking the trampoline
   name instead would have linked silently and left the other 31 in place.

   Those 31 were never inert. `EDMA_HandleIRQ()` opens with
   `assert(handle != NULL)`, SDK `assert()` is live in this image, and
   `nosys.specs` makes newlib's failure path hang rather than reset — so every
   one of those vectors was a latent hang behind any eDMA interrupt a later
   step enabled with the transactional API unused. Step 2 enabled none, so
   nothing showed. The weak `.set`-to-`DefaultISR` entries in the startup file
   are the correct occupants of those slots.

8. **No local modification to any vendored file.** Every file under `vendor/`
   was verified byte-identical to its upstream with `cmp` / `diff -r` after
   copying. Should that ever stop being true, the changed file and its rationale
   belong in this list, as `core/startup_v5f.S` is recorded in the CH32 tree.

9. **The USB HS clock and PHY sequence is hand-written in
   `src/usb_console.c`, not vendored.** Follows directly from rejecting
   `usb_phy.c` (see "Excluded from upstream 1"). What makes it defensible
   rather than a guess is that **two independent upstreams state the same
   sequence register for register**, and both were read before a line was
   written:

   - the SDK's own `USB_DeviceClockInit()` in
     `boards/frdmmcxn947/usb_examples/usb_device_cdc_vcom/bm/cm33_core0/virtual_com.c`,
     under `USB_DEVICE_CONFIG_EHCI`;
   - `hw/bsp/mcx/family.c` in the pinned TinyUSB tree, under
     `BOARD_TUD_RHPORT == 1 && CFG_TUSB_MCU == OPT_MCU_MCXN9`.

   They agree on all of it: `SPC0->ACTIVE_VDELAY = 0x0500`; the `ACTIVE_CFG`
   write; the `SCG0->LDOCSR` enable with the `TRIM_LOCK` key; the
   `AHBCLKCTRLSET[2]` gates; the `SOSCCFG`/`SOSCCSR` crystal start and
   `SOSCVLD` spin; `CLOCK_CTRL`'s `CLKIN_ENA` and `CLKIN_ENA_FM_USBH_LPT`;
   `CLOCK_EnableUsbhsPhyPllClock(kCLOCK_Usbphy480M, 24000000)`;
   `CLOCK_EnableUsbhsClock()`. TinyUSB then inlines exactly the body of
   `USB_EhciPhyInit()` for a part with neither `FSL_FEATURE_SOC_ANATOP_COUNT`
   nor `FSL_FEATURE_SOC_CCM_ANALOG_COUNT` — `TRIM_OVERRIDE_EN = 0x1f`,
   `CTRL |= ENUTMILEVEL2 | ENUTMILEVEL3`, `PWD = 0`, then the `TX` trim. Ours
   is that same body.

   The PHY trim constants are NOT open-coded: `BOARD_USB_PHY_D_CAL` (0x04),
   `BOARD_USB_PHY_TXCAL45DP` (0x07) and `BOARD_USB_PHY_TXCAL45DM` (0x07) come
   from the already-vendored `project_template/board.h`, and
   `BOARD_XTAL0_CLK_HZ` (24 MHz) from the already-vendored `clock_config.h`.
   So the board-specific numbers stay in the vendored file that owns them.

   **One interaction worth flagging to whoever touches power management next.**
   That `ACTIVE_CFG` write happens *after* `platform_init()` has already taken
   the part to overdrive for LPSPI6's 30 MHz slave ceiling. The levels it
   writes — `DCDC_VDD_LVL(3)`, `CORELDO_VDD_LVL(3)` — are the same overdrive
   levels, so it neither raises nor lowers the rail the link depends on; what
   it adds is `SYSLDO_VDD_DS` and `ACTIVE_VDELAY` for the PHY's analogue
   supply. If the core voltage policy is ever changed, these two writes have to
   be reconciled, because design doc section 2 is explicit that a routine power
   optimisation here silently becomes a protocol violation on the link.

10. **`src/usb_descriptors.c` and `src/usb_console.c` are built with
    `GLUE_WARNINGS`, not `APP_WARNINGS` — `-Wconversion` is dropped for those
    two objects only.** Both include `tusb.h`, and the warning fires inside
    TinyUSB's own descriptor macros (`TUD_CDC_DESCRIPTOR` packs 16-bit fields
    through `U16_TO_U8S_LE`, and `tu_htole16` and friends narrow deliberately).
    They are vendored and may not be edited, and `-isystem` does not suppress a
    warning raised by a macro expanded in our translation unit. Everything else
    in the app set still applies, `-Werror` included. Nothing in the app's own
    code needed the relaxation; `src/console.c` — the half that carries the
    logic — is built with the full `APP_WARNINGS` and has no TinyUSB include at
    all.

11. **The debug UART survives step 4.** `src/dbg_uart.c`'s own header says it
    "goes when step 4's TinyUSB CDC console replaces it", and the Makefile
    comment said the same. It stays, for two measured reasons rather than
    sentiment. The console is carried on the USB that the console is the
    instrument for debugging, so a USB fault removes the instrument exactly
    when it is needed — while the UART is a different peripheral, a different
    connector and a different host cable (MCU-Link VCOM on J17, not J11). And
    step 4's gates are *boot-rate* measurements against the 20% boot hazard, so
    the channel that reports them has to be live from `link_init()` onward,
    before USB enumeration has even been attempted. `src/usb_console.c` prints
    a one-line USB status report on that UART at 1 Hz for the same reason: a
    console cannot report its own absence.

12. **The vendored `system_MCXN947_cm33_core1.c` is deliberately not linked.**
    It remains byte-exact in the tree as the reference we deviated from. Its
    weak `SystemInit()` does not merely initialize CPU1: it writes ten
    chip-wide locations in SYSCON, SPC0, GDET0/1 and ITRC0, including an RMW of
    `SPC0->CORELDO_CFG` after CPU0 has raised `mcu_ready`. `src/system_core1.c`
    supplies the strong replacement and touches only CPU1's `SCB->CPACR` CP0/1
    access bits and `SCB->VTOR`; `CORE1_RETAIN` asserts that this object owns
    the linked `SystemInit` symbol. We **declined to put those chip-wide writes
    on CPU1**. We did **not** measure whether replaying NXP's sequence while the
    8 kHz link was live would have been tolerated.

13. **`boot_multicore_slave.c` is reimplemented as `src/core1_release.c`, not
    vendored.** The design doc says it is already in the vendor set; it is not.
    More importantly, the SDK file is gated on `__MULTICORE_MASTER` and refers
    to `__core_m33slave_START__`, which the byte-exact core0 linker script does
    not define because this build uses two separate images. Our module performs
    the same CPBOOT/CPUCTRL transaction from one CPUCTRL read, using the
    Makefile's `CORE1_OFFSET`. Rung 6 disassembles the linked function and
    checks both register addresses, store order, exactly one CPUCTRL read, both
    stores' dependency on that snapshot, and the exact boot/key/reset literals.

14. **Core1 uses `-mfloat-abi=soft`, not core0's hard-float ABI.** The requested
    `-mcpu=cortex-m33+nodsp` is correct, and `MCXN947_cm33_core1_COMMON.h`
    declares both `__DSP_PRESENT` and `__FPU_PRESENT` as zero. Combining that
    header with `-mfloat-abi=hard -mfpu=fpv5-sp-d16` makes CMSIS stop the build:
    "Compiler generates FPU instructions for a device without an FPU". Core1
    keeps the explicit `-mfpu=fpv5-sp-d16` selection for build symmetry but
    uses the soft ABI, selecting the toolchain's `v8-m.main/nofp` libraries and
    preventing FPU instructions. Suppressing CMSIS's check would encode a false
    hardware claim in the image.

15. **CPU0 refuses to release CPU1 onto a region that is not a plausible
    image.** `core1_image_valid()` checks the two words at 0x000C0000: the
    initial MSP must lie inside CPU1's `m_data` and be 8-byte aligned, and the
    reset vector must lie inside CPU1's flash window with bit 0 set. This has
    no counterpart in the SDK or the design document, and it is not caution: it
    is the difference between configuration (a) passing and CPU0 boot-looping
    (see the findings below). The predicate is portable and host-tested,
    including the erased-flash case it exists for. A skipped release is
    reported on the console as `NOT-RELEASED(no image)`, distinct from
    `HELD-IN-RESET`, so nobody reflashes a part that is already correct.

16. **The step-6 snapshot uses `_pad[7]`, not the document's `_pad[3]`.** The
    named fields in section 5 total 25 bytes; three padding bytes produce a
    28-byte C struct, contradicting the same section's explicit 32-byte ABI.
    Seven padding bytes make the stated size true while preserving every named
    field and its order. An alignment attribute was rejected because it would
    conceal the contract error rather than define the missing bytes. The
    resulting shared window remains the approved 48 bytes: the existing fourth
    word of the 16-byte step-5 prefix is now `cpu1_seen_slot_counter`, followed
    by the 32-byte snapshot. Both C static assertions and rung 4 enforce those
    sizes independently.

---

## Findings while building step 6

- **Section 5's displayed snapshot layout is 28 bytes, not 32.** Its arithmetic
  is `3 * 4 + 6 * 2 + 1 + 3 = 28`. The contract and the approved 48-byte shared
  window both require 32, so `src/link_snapshot.h` uses `_pad[7]` and asserts
  the result. `SHARED_WINDOW_SIZE` is passed from the Makefile to the artifact
  checker, which independently rejects either ELF if `g_shared_window` is not
  exactly 0x30 bytes. This is a design-document error, not a compiler-layout
  quirk.

- **Section 5 calls the module `shared_link_status.c`, but no such module ever
  existed.** Step 6 uses the approved `src/link_snapshot.{c,h}` name. The
  algorithm is still one unguarded implementation; only the barrier spelling
  differs between MCXN947 and host builds.

Step 6 has been built and host-tested only. Its LPCAC/coherency acceptance is a
hardware measurement: CPU0's live snapshot count and CPU1's echoed count must
track on the console while the blue LED cycles. No result is claimed here.

---

## Findings while building step 6

- **SRAM is coherent across the two cores; the seqlock needs `__DMB()` for
  ordering only.** Design doc §5 called this "strongly supported, UNVERIFIED"
  and asked for a design where being wrong would be cheap and visible. Measured
  on hardware 2026-09-16 over a 43 s window, CPU0 publishing at ~8 kHz and CPU1
  reading at ~20 Hz: CPU1's echoed `slot_counter` tracked CPU0's live value
  continuously, lagging 105 slots (13.1 ms, which is the sampling interval and
  not staleness), with **`snapshot_fail = 0`** — not one bounded read gave up.
  LPCAC stayed enabled and the window stayed at 0x2004C000; none of §5's
  escalation steps (`__DSB()`, disabling LPCAC, moving to SRAMX) were needed.

- **The echo beats the LED as an instrument.** §5 proposed watching
  `slot_counter` on the panel, and §9 step 6 proposes the RGB LED. Both need a
  human looking at the board and yield no number. `cpu1_seen_slot_counter` — a
  word CPU1 writes with the value it actually read — turns the coherency
  question into two numbers on the console that either track or do not. It is
  the same category as the CPU1 heartbeat that §3 already endorses: data CPU0
  observes and never waits on. The LED is still there and still blinks at
  ~1.95 Hz; it is simply no longer the only evidence.

- **§5's snapshot struct does not add up to the size it claims.** The named
  fields total 28 bytes, so the `_pad[3]` shown gives 28 and not the 32 the
  trailing comment asserts. `src/link_snapshot.h` keeps every named field and
  widens the padding to `_pad[7]`, realizing the stated 32-byte ABI rather than
  silently shipping a 28-byte struct or hiding the difference in an alignment
  attribute. The module is also named `link_snapshot.{c,h}`; §5's
  `shared_link_status.c` never existed in this tree.

- **CPU1 gets the blue LED because the other two are taken.** §9 step 6 says
  "the RGB LED" without saying which. CPU0 already drives green (P0_27) as its
  foreground heartbeat, and §3 reserves red (P0_10) for CPU0 signalling a hard
  link fault so that a dead CPU1 cannot hide one. Blue (P1_2) is what is left,
  and it has the useful side effect of keeping CPU1 off Port 0 entirely at this
  step.

- **Both cores necessarily share the clock-gate registers, and the boot ladder
  is what makes that safe.** CPU1 calling `CLOCK_EnableClock(kCLOCK_Port1)` is a
  read-modify-write on a chip-wide SYSCON register — the same *category* of
  write step 5 rejected in CPU1's SystemInit. The difference is real: enabling a
  gate for a peripheral CPU1 exclusively owns affects nothing CPU0 uses, whereas
  SystemInit re-ran chip policy (core LDO, glitch detect, RAM ECC, flash cache)
  that CPU0 had already set. It is race-free specifically because §4(a) releases
  CPU1 at rung 8, strictly after CPU0 has finished all of its own clock setup.
  **This gets sharper at step 7**, where §2's display pins put CS (P0_12),
  D/C (P0_7), WR (P0_9) and RD (P0_8) on Port 0 — the port whose GPIO registers
  CPU0's LED code is still writing at 1 Hz. §2 notes that no J8 pin is on Port 3
  "so there is zero physical overlap with the link", which is true and is not
  the same claim as §4(b)'s "disjoint pins": the *pins* are disjoint, the *port
  registers* are not. PSOR/PCOR are write-1-to-act and therefore safe for
  disjoint bits; PDDR is an RMW and must stay inside the boot ordering.

---

## Findings while building step 5

- **The design's literal forbidden-symbol wording cannot pass either byte-exact
  startup.** Both startup files define weak trampolines for the part's complete
  vector table. Core0 therefore necessarily contains weak `FLEXIO` and
  `EDMA_1_*` symbols, while core1 necessarily contains weak `EDMA_0_*`,
  `CTIMER2`, `GDET`, `ITRC` and `SPC` symbols. These are unhandled vector stubs,
  not peripheral ownership. Rung 5 rejects matching **strong definitions** and
  explicitly ignores only weak bindings; its self-test proves both halves of
  that discrimination.

- **A symbol-name ban on `SPC` / `GDET` / `ITRC` is not by itself a static
  guarantee against NXP's SystemInit sequence.** Those register macros compile
  into numeric MMIO addresses and need not survive as ELF symbol names. The
  actual guarantee combines the rung-1 ownership assertion that strong
  `SystemInit` comes from `build/core1/system_core1.o` with a rung-5
  disassembly allowlist: exactly two stores, to SCB CPACR and VTOR, in that
  order. Object survival and the absence of the vendor system object from
  `CORE1_OBJECTS` complete the chain. The name bans remain useful for catching
  future strong handlers/drivers, but are not overstated as proving direct-MMIO
  absence on their own.

- **The requested hard-FPU flags contradict the core1 device description.**
  This was found by the real target compile, not inferred from the core name;
  deviation 14 records the safe resolution.

- **The release checker must inspect emitted code, not source intent — and it
  is the checker that must bend.** GCC 14.2.1 at `-Os` encodes both 0x50000000
  and 0x000C0000 as Thumb-2 modified immediates, so neither reaches the literal
  pool, and a rung 6 that read only `.word` entries failed a perfectly correct
  image. An intermediate draft "fixed" this from the firmware side, with a
  branch-over literal load in inline assembly so the constant would appear
  where the checker was looking. That was backwards: it contorted shipped code
  to satisfy a test, and it came packaged with a silent early return (below).
  The fix belongs in `analyze_core1_release()`, which now collects `mov`/`movw`
  immediates alongside pool words, and in the self-test, which carries the real
  `objdump` output of the built function verbatim as a vector. Whether a
  constant arrives as a pool word or an immediate is a codegen detail no
  assertion may depend on.

- **`core1_release()` takes no argument, and that is a safety property.** It
  briefly took the boot address as a parameter and returned early when the
  argument disagreed with `CORE1_VECTOR_ADDR`. A silent no-op there is exactly
  the failure rung 6 exists to catch: a working link and a black screen, every
  counter healthy, nothing anywhere saying CPU1 was never released. The address
  is `CORE1_OFFSET` by definition, so the parameter only created a second place
  for it to be wrong. The `-D` now reaches exactly one translation unit.

- **RELEASING CPU1 ONTO AN ERASED REGION RESETS THE WHOLE CHIP.** This is the
  serious finding of step 5 and it falsifies the design document's invariant as
  written. Section 4 claims that if CPU1 "was never flashed at all, the FPGA
  link runs normally"; measured on the bench, it does not. With the core1
  region blank, both vector words read 0xFFFFFFFF, CPU1 faults on its first
  fetch, escalates to LOCKUP, and takes CPU0 down with it — **CPU0 boot-looped
  continuously**, re-running `link_init()` forever, with the link never
  reaching steady state. Configuration (a) of section 9 step 5 failed outright
  on the first attempt.

  `core1_image_valid()` (deviation 15) is the fix: CPU0 reads the two vector
  words and declines to release CPU1 unless they are a plausible pair. That is
  a validity check on a flash image, not a handshake — it reads two words and
  waits for nothing — so section 8's objection to MCMgr does not apply. With
  the check in place configuration (a) passes with every counter flat.

  **The lockup-to-reset path itself is inferred, not confirmed against the RM.**
  What is measured is the pair of outcomes: blank core1 region boot-loops CPU0,
  valid core1 image does not, with nothing else changed. The mechanism named
  here is the obvious reading of that, and the RM is not on disk (section 10).

- **A halted MCU is indistinguishable from the boot-random frame offset, from
  the FPGA side.** When LinkServer leaves the part stopped, `mcu_ready` floats
  high, so the FPGA believes the link is up, validates every returned slot, and
  gets nothing — producing `spi_bad_sof` saturated 1:1 with `spi_slots`, which
  section 10 documents as the signature of the boot-random frame offset. The
  two are told apart by the debug UART: a mis-framed board still reaches its
  foreground loop and still prints its periodic report, and a halted one prints
  nothing at all. This cost one full gate run before it was recognised.

- **LinkServer leaves the target halted unless its log says `restart on
  reset`.** Both `flash ... erase` followed by `flash ... load`, and the
  single-command `flash ... load -e`, wrote the image correctly and then left
  the part stopped at the boot-ROM stall; only a plain `load` printed `restart
  on reset` and actually ran. Treat that line as the success condition, not the
  `Finished writing Flash successfully` above it. Separately, LinkServer's
  default `--update-mode check` stalled for over three minutes on this setup
  and had to be killed twice; `-u none` completes in about eight seconds and is
  now the Makefile default.

- **`cpu1halt` / `cpu1start` exist because the debugger could not do it.**
  Section 9 step 5 configuration (c) asks for CPU1 halted in the debugger.
  LinkServer's gdbserver attached to `cm33_core1` without ever stopping it —
  the heartbeat kept advancing at 720/s through a supposed halt, so an early
  (c) "pass" was really a second measurement of (b) — and it reported
  `pc = 0x00000000` while reading the shared window correctly over the same
  connection. CPU0 re-asserting CPU1's reset is deterministic, repeatable,
  needs no debugger, and is harsher than a debugger halt because reset is
  asynchronous and can land mid-store. CPU0 already owns that reset line, so
  this is not a command *to* CPU1 and does not breach section 3's one-way
  data-only IPC rule. Section 3 asks for re-release to sit behind an explicit
  console command in any case; `cpu1start` is that command.

- **Every hardware reading must come from a board you just deliberately reset,
  captured from the moment of reset.** A board whose recent history includes a
  debugger attach reports nonsense: one mid-session sample showed no UART
  output, no USB enumeration and saturated `spi_bad_sof`, which read as a hard
  failure and was simply a part left halted by an earlier attach. The same
  configuration measured clean immediately afterwards when the capture started
  at the reset. This is the step-5 restatement of section 10's rule that a
  single reading is one draw from a distribution.

---

## Findings while building step 4

- **The USB vector on this board is `USB1_HS`, not `USB0`.** Worth stating
  because "USB0" is the obvious guess and it is wrong twice over. UM12018: "only
  the HS USB controller and PHY interface is used and it is connected to the USB
  Type-C connector (J11)" — `USB0_FS` (vector 50) is routed to nothing on the
  FRDM board. The vector that matters is `USB1_HS_IRQn`, 67, and the retention
  root is `USB1_HS_DriverIRQHandler`, not the weak trampoline
  `USB1_HS_IRQHandler` that the table actually names. `make check` reports it as
  `T ... from usb_console.o`, which is what proves the real handler survived
  `--gc-sections` rather than the vendor's `DefaultISR` alias.

- **`rhport = 1`, and the reason it could have been wrong for a long time
  without anyone noticing.** Design doc section 10 listed the number as "taken
  from `ci_hs_mcx.h`, not re-read". Re-read at this step and confirmed three
  ways in the pinned tree: `tusb_mcu.h` defines `TUP_RHPORT_HIGHSPEED 1` for
  `OPT_MCU_MCXN9`; `dcd_ci_hs.c` selects `ci_hs_mcx.h` under a comment reading
  "MCX N9 only port 1 use this controller"; and `hw/bsp/mcx/family.c` routes
  `USB1_HS_IRQHandler` to `tusb_int_handler(1, true)`.

  The trap is that **`ci_hs_mcx.h` ignores the number**. `CI_HS_REG(port)` casts
  `(void) port` away and returns `_ci_controller[0]` unconditionally, and
  `CI_DCD_INT_ENABLE`/`DISABLE` do the same. So the DCD would work with rhport
  0; what breaks is everything above it — `TUD_OPT_RHPORT` is derived from which
  `CFG_TUSB_RHPORTn_MODE` is defined, `tusb_int_handler()` range-checks against
  `TUP_USBIP_CONTROLLER_NUM` (2, from `tusb_private.h`), and the ISR's argument
  has to match the port the stack was initialised on. A wrong number here fails
  silently in some configurations and loudly in others, which is the worst
  combination for a value nobody has re-read.

  Related: `tusb_init(rhport, NULL)` **ignores its own `rhport` argument** and
  falls back to `TUD_OPT_RHPORT`. `usb_console_init()` therefore passes an
  explicit `tusb_rhport_init_t` instead of `NULL`, so the number that gets used
  is the number that was written down.

- **The CH32 console has no command surface to carry over.** Design doc
  section 7 says `usb_cdc_fs.c` "collapses to ~17 lines of TinyUSB glue plus
  descriptors", with the implication that its *commands* migrate. They do not
  exist: `firmware/ch32h417/src/usb_cdc_fs.c` is 644 lines of pure USBFS
  endpoint transport with no parser, its header describes the channel as
  carrying "kmbox-style injection text" that was never written, and
  `main_v5f.c` calls only `cdc_fs_init()` / `cdc_fs_poll()` — it never reads a
  byte out of the RX ring. The actual precedent for what a console should say
  is step 3's `link_report()` on the debug UART, so `stats` deliberately mirrors
  that line's field names and grouping.

- **The collapse is real but it is not 17 lines, and the useful number is a
  different one.** Counting non-comment, non-blank lines: the CH32's transport
  was 427, all of them guarded. The MCXN947's is 179 guarded lines in
  `usb_console.c`, of which about a third is the clock/PHY sequence and the
  debug-UART status report rather than USB glue. The change that matters is not
  the total but the split: 234 lines of console *logic* in `console.c` with
  **zero** guarded lines and a host test covering every command, against a CH32
  tree where the equivalent logic did not exist and could not have been tested
  if it had.

- **Two comments in `link.c` asserted something step 3's own experiment had
  already disproved, and are corrected here.** Both said, in different words,
  that the framing monitor plus the section 4 ladder repair a mis-framed boot —
  one of them concluding "alignment converges rather than depending on a lucky
  boot". The 20-boot experiment that closed step 3 found the opposite and this
  file records it: the ladder ran ~220 times against one mis-framed boot without
  ever repairing it, and such a boot needs a reset. The distinction the
  corrected comments now draw is that a **recovery** whose own re-arm lands
  mis-framed *is* repaired (measured: 60 ms, 480 bad slots) while a **boot**
  that comes up mis-framed is not. No code changed; the mitigation
  (`link_spi_enable_aligned()`) is untouched.

- **Bringing the USB stack up does not disturb the link, measured before any
  host was attached.** The step-2 gate re-run against the step-4 image with
  `tusb_init()` returning ok and the controller running but no cable on J11:
  `spi_slots` 8000.00/s, `spi_bad_sof` / `spi_bad_crc` / `spi_bad_length` /
  `spi_bad_type` / `spi_queue_full` / `link_losses` / `rx_invalid` all zero over
  a 14.9 s window, `link_ready=1`, `map_active=0`. This isolates the clock and
  PHY bring-up — which touches `SPC0->ACTIVE_CFG`, the core rail the link's
  30 MHz slave ceiling depends on — from anything the host does later.

- **Any step-4 gate must be measured at least ~70 s after boot, because step 3's
  provocation demo deliberately breaks the link before then.** `link_demo_step()`
  arms at `LINK_DEMO_ARM_SLOTS` = 160,000 slots (~20 s at 8 kHz) and then runs
  two provocation phases of `LINK_DEMO_HOLD_SLOTS` = 80,000 slots (~10 s) each.
  Measured, the cost of getting this wrong: a 22.9 s FPGA window started ~30 s
  after a reflash read `spi_bad_sof` at 173.97/s and `link_losses` 6 and
  verdicted FAIL, while the identical measurement taken after the demo had
  finished read 8000.30 slots/s with every counter flat over 27.9 s. Both are
  correct readings of a board doing different things. This is the same trap as
  step 3's classifier guarding on `ms > 35000`, and it now applies to the FPGA
  side of the gate as well as the UART side.

- **Adding the USB stack did not make the boot hazard worse: 6 of 6 clean.**
  Design doc section 10's standing warning is that any single-boot measurement
  on this board is one draw from a distribution with a 20% failure mode, so the
  step-4 image was re-flashed and re-measured six times with the step-3 method
  (`vcom.py` across the flash, anchored on the boot banner, verdict from the
  last report inside the window and before the deliberate provocations at
  ~41 s). Every trial: `sof=0 recov=0 framing=0`, ~109,310 slots retired in
  13.7 s (7,964/s MCU-side), and `init=ok` on the USB line. Against step 3's
  20/20 with the gap wait this is consistent and adds no evidence of harm,
  though six trials cannot detect a small change in a 20% rate — it rules out a
  gross regression, not a subtle one.

- **`OTGSC[BSV]` is the field that tells you whether J11 has a cable**, and
  having it on the debug UART is the difference between two diagnoses that look
  identical from the host. With no cable: `otgsc=0x0021100a` — BSV (bit 11) = 0,
  BSE (bit 12) = 1 — `PORTSC1[CCS] = 0`, and nothing appears in `/dev`. With a
  firmware fault the host sees exactly the same nothing. The board does route
  VBUS (UM12018 Table 11: J11 "provides the 5 V power supply (P5V_USB_HS)
  source to the board"), so a 0 there is a bench condition, not a limitation.

## Findings while building step 3

Three of these change what the firmware does and none of them is in the design
doc. The first contradicts a claim the design doc makes.

- **A boot can come up with the whole slot permanently offset, and it is
  boot-random.** Two consecutive flashes of a byte-identical image, nothing else
  changed, came up 69 bits out and then perfectly aligned. The offset does not
  decay; it held for the entire 20 s the board ran.

  Nothing in any status register says so. No underrun, no overrun, no DMA error
  — SR reads exactly as it does on a healthy link, because from the
  peripheral's point of view nothing went wrong. On the FPGA the only symptom is
  `spi_bad_sof` at 1:1 with `spi_slots`, which step 2 already recorded as the
  saturated, non-discriminating signature of *any* whole-slot corruption. It was
  only nameable once retirement made the received bytes readable: decoded, the
  received stream was the FPGA's own IDLE keepalive **rotated right by exactly
  69 bits**.

  This is the reason step 2's 22 s of flat counters was a weaker result than it
  looked. It was one boot.

  `link_spi_enable_aligned()` waits for the inter-frame gap on the chip-select
  pad before setting `CR[MEN]`, reading it out of `GPIO3->PDIR` — which reflects
  the pad whatever the PORT mux selects, so the pin is readable as an input at
  the same time as it is wired to PCS0, which `SR[MBF]` cannot do with the
  module disabled.

  **Measured properly, 20 boots per arm, `-DLINK_SKIP_GAP_WAIT` building the
  control:**

  | arm | clean | mis-framed |
  |---|---|---|
  | gap wait present | **20 / 20** | 0 |
  | gap wait removed | 16 / 20 | **4 (20%)** |

  Fisher's exact, one-tailed: **p = 0.053** — well supported, and *just* short
  of the conventional threshold, so state it as evidence rather than proof. Put
  the other way: if the wait did nothing, twenty clean boots in a row would
  happen 1.1% of the time. An earlier 7-of-7 against 1-of-2 is NOT pooled in;
  those runs came from a session with the code changing underneath, and
  combining non-comparable runs to reach significance is how the repo's old
  four-row yosys table became worthless.

  The mechanism is still **not** established — see the next item.

- **Three deliberate attempts to reproduce that offset at run time all failed,
  and the link self-heals from every mid-run perturbation tried.** Each was held
  for ten seconds with the fault monitor disarmed, and each ended with `sof`
  still zero and every FPGA counter still flat:

  | provocation | result |
  |---|---|
  | cycle `CR[MEN]` ~1 µs into the burst | no effect at all |
  | starve the transmit FIFO (`ERQ` off, 1 s) | sets `SR[TEF]`, then self-heals |
  | steal 3 words from the receive FIFO | receive path re-synchronises |

  So the boundary is **not** simply latched at `CR[MEN]`, and the boot case has
  a cause none of these reproduces. The transmit result also qualifies
  ERR051588's "does not self-heal" as the design doc states it: the erratum's
  own trigger fires — `SR[TEF]` latches, measured three times — but on this part
  a one-second underrun did not leave the transmit stream corrupt.

  The perturbation is kept in `link.c` as a labelled negative control. A
  provocation nobody records having tried gets tried again.

- **The framing monitor DETECTS a mis-framed boot but does NOT repair it.**
  This corrects an earlier claim in this file that it was "the backstop that
  makes the alignment hypothesis not need to be right". It is not a backstop.

  `link_retire_framing_lost()` reports the link mis-framed when at least 200
  slots were retired in a 50 ms window and *not one* of them parsed. The
  threshold is "none", not "most", because a framed link that is merely noisy
  still lands the FPGA's constant keepalive between the bad slots. That
  predicate fires correctly. The ladder that follows does not fix this fault.

  All four mis-framed boots in the 20-boot control arm looked like this:

  ```
  sof=92917  recov=220  framing=220   ms=11691
  sof=93049  recov=221  framing=221   ms=11701
  sof=92952  recov=220  framing=220   ms=11692
  sof=92915  recov=220  framing=220   ms=11690
  ```

  Every slot failing for the whole window, with the ladder firing once per
  ~53 ms monitor pass — about 220 times — and never succeeding. **A mis-framed
  boot is not recoverable in software on this part; it requires a reset.**

  The monitor's one observed success was repairing a *recovery's own* bad
  re-arm: detected and fixed 60 ms later at a cost of 480 bad slots, FPGA-side
  123.9 `spi_bad_sof`/s during the window and 0.0/s after, flat thereafter.
  That is a real result and worth keeping — but it is a different fault from
  the boot offset, and generalising from it was the error.

  Consequence for anyone building on this: the gap wait is doing the real work
  and there is no working fallback behind it. The mechanism is unexplained and
  the hazard without it is 1 boot in 5.

- **The recovery ladder deviates from design doc section 4 in one place: it
  drops `CR[MEN]` at rung 3 and restores it at rung 5.** Section 4 does not
  mention `MEN`. Without it the ladder can only re-arm the DMA, and re-arming a
  mis-framed slave changes nothing; dropping the module is what lets rung 5
  choose a new boundary in the inter-frame gap. Two smaller additions for the
  same reason: rung 5 does not return until the transmit FIFO holds a whole slot
  again, and rung 6 clears `SR` immediately before raising `mcu_ready`. The
  window between the FIFO reset and the refill is one in which the FPGA is still
  clocking an empty transmit FIFO, so it sets `SR[TEF]` *by construction* — a
  fault manufactured by the recovery rather than found by it. Leaving it latched
  had the monitor read it on its very next pass and recover forever.

- **Retirement runs in the ISR, not the foreground, and the numbers say it must.**
  A two-bank ring gives 250 µs of slack; one 115200-baud report line takes
  ~17 ms and would cost seventy rotations. In the ISR the cost is ~10 µs per
  125 µs slot, nearly all of it the bitwise CRC-16. Measured over 550,000 slots:
  `stall = 0`, `daddr_err = 0`, and ISR entries track retired slots exactly (the
  constant ~59 difference the report shows is the UART latency between printing
  one counter and reading the other, at 8 slots per millisecond).

- **The FPGA is not only sending keepalives.** About 1 in 8 slots is a real
  `INJ_TYPE_REPORT_FRAGMENT` (0x03) carrying a live sequence byte, at ~1 kHz —
  the mouse report rate, matching `native_reports`. Over 68,000 of them the
  classifier reported one sequence gap (across a recovery) and zero duplicates
  or stales. The design doc describes this direction as telemetry; it is worth
  recording that it is *populated* from step 3 onwards, because it means the
  sequence classifier is exercised by real traffic rather than only by tests.

- **Printing a shared buffer a byte at a time manufactures evidence.** A 32-byte
  hex dump takes ~30 ms at 115200 baud and the retirement ISR rewrites the
  source buffer 240 times in that window, so the line that reaches the terminal
  is a splice of hundreds of slots. On the bench a perfectly healthy keepalive
  acquired a scatter of stray bytes it never carried on the wire, which briefly
  read as corruption. The snapshot is now taken with the ISR masked.

---

## Findings while building step 2

Four of these change what the firmware does, and none of them is in the design
doc. Each was settled from files this tree imports, not from the Reference
Manual, which is still login-gated and not on disk.

- **`TCR[BYSW]` = 1** — design doc section 10 lists the byte order as "derived,
  not measured" and says it resolves at step 2. It resolves to *set*, and the
  vendor states the case directly. The eDMA moves 32-bit words out of a byte
  array on a little-endian core, so the word reaching the FIFO for slot bytes
  b0..b3 is `b0 | b1<<8 | b2<<16 | b3<<24`, and shifted MSB-first that puts b3
  on the wire first. `fsl_lpspi.h`'s `kLPSPI_SlaveByteSwap` comment covers
  exactly this: for a 32-bit frame a buffer "1 2 3 4 5 6 7 8" clocks out as
  "4 3 2 1 8 7 6 5" without the flag and "1 2 3 4 5 6 7 8" with it.

  `fsl_lpspi_edma.c` is the corroborating case rather than the same claim
  twice: on the DMA path there is no software marshalling at all — the engine
  reads memory straight into `TDR` — and the driver sets `TCR[BYSW]` from that
  same flag (lines 237-238 and 834-836). That is our situation exactly.
  Confirmed in the built image: `TCR` is the literal `0x004000FF`, i.e.
  `BYSW` set and `FRAMESZ` 255.

- **`CFGR1[PINCFG]` = 3, and nothing in the design doc predicts it.** The
  mikroBUS socket names its nets for a board acting as *master*, so J6's MOSI
  net lands on P3_20 = `FC6_P0` = the LPSPI **SOUT** pad, and its MISO net on
  P3_22 = `FC6_P2` = the **SIN** pad. (`FCn_P0` is SOUT: the SDK's own LPSPI
  slave example labels `PIO0_24/FC1_P0/...` as `LPSPI1_SOUT`.) We are the
  slave, so we must *read* the MOSI net and *drive* the MISO net — the opposite
  of the default. `PINCFG` 0b11 is "SOUT is used for input data; SIN is used
  for output data", which is precisely that swap. Left at the default 0b00 the
  MCU would drive P3_20 into the FPGA's own driver and listen on a wire nobody
  drives.

- **The four pin ALT numbers are not what the `pin_signal` string suggests, and
  the chip select is the odd one out.** Reading `FC6_Pn`'s position in e.g.
  `PIO3_20/WUU0_IN27/TRIG_OUT0/FC8_P4/FC6_P0/...` as an ALT index gives the
  wrong answer on three of the four pins: the string also lists functions that
  occupy no mux slot. Pairing every `/* Pin is configured as FCn_Pm */` comment
  in every FRDM/EVK `pin_mux.c` with the `kPORT_MuxAltN` it programs gives a
  rule with no counterexample in 787 configured pins:

  | pin offers | ALT |
  |---|---|
  | one FlexComm | ALT2 (136 cases) |
  | two FlexComms, the first | ALT2 (643 cases) |
  | two FlexComms, the second | ALT3 (8 cases) |

  P3_20/21/22 each list FC8 before FC6, so FC6 is **ALT3** on all three.
  P3_23 lists FC6 *only*, so there FC6_P3 is **ALT2**. Getting that one pin
  wrong leaves PCS0 unconnected, the slave never frames, and the symptom is not
  an error but total silence with every FPGA counter flat — indistinguishable
  from the link never having been attempted.

- **FlexComm6 cannot be clocked from FRO_HF.** `BOARD_BootClockPLL150M` leaves
  FRO_HF at **48 MHz** — its own YAML header says `{id: FRO_HF_clock.outFreq,
  value: 48 MHz}` and its body calls `CLOCK_SetupFROHFClocking(48000000U)`. LP2
  requires SCK <= f_periph/4, so 48 MHz caps SCK at 12 MHz against the 15 MHz
  the FPGA clocks: `kFRO_HF_DIV_to_FLEXCOMM6` would be a silent protocol
  violation, and it is the attach ID someone reaching for "the fast FRO" would
  pick. PLL0 is already at 150 MHz from the same profile, so `link_spi_init()`
  routes it through PLLCLKDIV — which the board profile does not touch, hence
  set explicitly — and halves it to 75 MHz. `link.h` static-asserts the
  >= 4x SCK relation so a later divider edit cannot quietly break it.

- **PORT5/GPIO5 have no clock gate to enable.** `mcu_ready` is P5_7, and
  `fsl_clock.h`'s `clock_ip_name_t` stops at `kCLOCK_Port4` / `kCLOCK_Gpio4` —
  there is no `kCLOCK_Port5`. This is consistent with design doc section 2
  calling P5_7 "an always-on VDD_BAT pad" when it rejects the pin for timer
  capture, and with the SDK's own `mc_pmsm` example writing `PORT5->PCR[]` with
  no clock enable anywhere. `link.c` therefore enables nothing for this pin.
  **If that turns out to be wrong the failure is silent**: `mcu_ready` stays
  low, the FPGA latches `transfer_ready` low, and every counter stays flat —
  which is what a *passing* gate looks like. Read `injection_link.link_ready`
  over JTAG to tell the two apart; it is driven straight from the synchronised
  `mcu_ready` pad (`gateware.py:805`).

- **eDMA errata 51327 does not apply here, which is a near-miss worth
  recording.** That erratum requires `NBYTES` to be a multiple of 8 when
  scatter-gather is used, and our minor loop is 4 bytes — exactly the value it
  would forbid. `MCXN947_cm33_core0_features.h:535` defines
  `FSL_FEATURE_EDMA_HAS_ERRATA_51327 (0)`, so `EDMA_CheckErrata()` compiles out
  and the pattern is legal on this part. The 4-byte minor loop is not
  incidental: it makes every transfer request-paced by the FIFO, whereas a
  32-byte minor loop would run to completion once started and could overrun a
  FIFO that is not empty.

- **SDK `assert()` is live in this image** (`__assert_func` is linked; nothing
  defines `NDEBUG`). Inherited from step 1's flags rather than introduced here,
  but it now matters more, because the vendored eDMA and LP_FLEXCOMM entry
  points assert on their arguments. All of the ones this code can reach are
  satisfied by construction (channel index, non-NULL descriptors); a violated
  one would hang in newlib rather than reset, since `nosys.specs` makes `_write`
  fail.

---

## Findings while reading the vendored sources (step 1)

Recorded here because the design doc's section 10 lists them as unverified and
these were resolved by reading files this commit imports.

- **CTIMER2's IRQ symbol name is `CTIMER2_IRQHandler`** — but rooting *that*
  name proves nothing. `startup_MCXN947_cm33_core0.S` uses a **two-level**
  dispatch for every peripheral vector: the table entry `<NAME>_IRQHandler` is a
  weak trampoline that branches to `<NAME>_DriverIRQHandler`, and it is the
  *Driver* symbol that the `def_irq_handler` macro `.set`s to `DefaultISR`. In a
  built image the three shapes look like this:

  ```
  CTIMER2_DriverIRQHandler  W  0x4f8   <- == DefaultISR, the spin stub
  CTIMER2_IRQHandler        W  0x598   <- the weak trampoline, a distinct address
  HardFault_Handler         W  0x500   <- a weak stub that branches to itself
  SysTick_Handler           T  0x1830  <- ours; the strong definition won
  ```

  So a gate that only asked "is it defined, and is its address not DefaultISR?"
  would pass the middle two while the real handler had been discarded. The
  binding letter is the discriminator, and that is what
  `tools/check_mcxn947_images.py` rung 1 tests. **For a peripheral IRQ, the
  retention root that means anything is `<NAME>_DriverIRQHandler`.**

- **There is no `NonCacheable` memory *region* in the vendored `.ld`.** Section
  10 asks whether one exists before step 2. It does not: `*(NonCacheable.init)`
  and `*(NonCacheable)` are collected into the ordinary `.data` output section
  in `m_data`, alongside `CodeQuickAccess` and `DataQuickAccess`. The attribute
  therefore buys nothing on this part as the script stands — anything relying on
  it for DMA coherency needs an MPU region or a different placement, decided
  explicitly.

- **`SystemInit()` enables LPCAC** — `SYSCON->LPCAC_CTRL &= ~DIS_LPCAC_MASK`
  (`system_MCXN947_cm33_core0.c:99`). It is on by default from reset of every
  image, which is the starting condition for section 5's LPCAC question at
  step 6. It also disables RAM ECC to recover the full RAM size, disables the
  aGDET/dGDET glitch detectors' chip-reset path, and sets `SCB->VTOR` from
  `__Vectors`.

- **The FRDM-MCXN947 LED pinout is P0_10 red, P0_27 green, P1_2 blue, PORT mux
  ALT0, and active LOW** (SDK `boards/frdmmcxn947/project_template/board.h` and
  `pin_mux.c`). Worth stating because the MCX-N9XX-EVK maps its RGB LED to
  GPIO3[2:4] **active high**, and at least one SDK export in circulation carries
  that EVK mapping under `BOARD_NAME "FRDM-MCXN947"`. The board package that
  ships inside the FRDM SDK is the authority. Step 1 blinks green; red is left
  alone because the design reserves it for CPU0 signalling a hard link fault
  directly.

- **`board.h` puts `BOARD_LCD_DC_GPIO_PIN` on P0_10, the red LED**, while design
  doc section 2 assigns display D/C to P0_7. Not chased here — flagged for
  whoever writes step 7.

- **`~/git/dm-mcx-streamdeck/` is a different, older SDK and must not be used as
  a source for this tree.** Every overlapping file differs from 24.12.00: its
  CMSIS is Core(M) 5.4 against 24.12.00's 6.1, its `MCXN947_cm33_core0.h` is the
  5.0 MB monolithic form, its `clock_config.c` has *empty function bodies* (the
  generated code was stripped, so `BOARD_BootClockPLL150M` is declared and never
  defined), and its `board.h` maps the RGB LED to GPIO3[2:4] active-high — the
  MCX-N9XX-EVK pinout — under `BOARD_NAME "FRDM-MCXN947"`. Nothing in this tree
  came from it.
