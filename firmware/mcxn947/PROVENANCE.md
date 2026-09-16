# MCXN947 Firmware Provenance

Target: **FRDM-MCXN947** (MCXN947VDF, dual Cortex-M33), replacing the CH32H417 on
the PMOD-A injection link. Design: `docs/MCXN947_CONTROLLER.md`.

**This tree is at migration step 2 of that document's section 9** — CPU0 only:
clock, blink, and LPSPI6 as an SPI slave on LP_FLEXCOMM6 driven by a
self-loading eDMA0 scatter-gather ring that transmits a permanently IDLE slot.
There is no RX retirement, no USB, no display and no CPU1 image. What is
*absent* is recorded here too, because "we did not vendor it yet" and "we
decided not to vendor it" are different claims.

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
| `vendor/mcux-sdk/devices/MCXN947/fsl_device_registers.h` | same |
| `vendor/mcux-sdk/devices/MCXN947/system_MCXN947_cm33_core0.{c,h}` | same |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_{common,common_arm,reset,port}.h` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_{clock,spc,gpio}.{c,h}` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_reset.c` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_lpflexcomm.{c,h}` | `devices/MCXN947/drivers/` |
| `vendor/mcux-sdk/devices/MCXN947/drivers/fsl_edma.{c,h}`, `fsl_edma_core.h`, `fsl_edma_soc.{c,h}` | `devices/MCXN947/drivers/` |
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
- **`middleware/usb/phy/usb_phy.{c,h}`**, `boot_multicore_slave.c`, and the
  ST7796S / DBI / FlexIO display stack. Steps 4, 5 and 7 respectively.
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

**The core1 pair is imported but not built.** Step 5 is the first step that
links it. It is here now so that the two scripts can be read side by side while
reasoning about the shared window, and so that step 5 is a Makefile change
rather than another vendoring commit.

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

7. **No local modification to any vendored file.** Every file under `vendor/`
   was verified byte-identical to its upstream with `cmp` / `diff -r` after
   copying. Should that ever stop being true, the changed file and its rationale
   belong in this list, as `core/startup_v5f.S` is recorded in the CH32 tree.

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
