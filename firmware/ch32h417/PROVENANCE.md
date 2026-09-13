# CH32H417 Firmware Provenance

- Platform reference: [VoltCyclone/Hurra-v3](https://github.com/VoltCyclone/Hurra-v3)
- Imported commit: `231b326502e4e0232a62a8373d2a70165ea1be63`
- Allowed behavior sources: startup/linker/build, USB CDC, SPI frame codec,
  low-level SPI setup patterns, WS2812 PIOC.
- Excluded behavior: inject_link, usb_merge, actions, kmbox_cmd, ferrum,
  humanize, gesture, HID host/device, two_board.
- WCH vendor sources remain unmodified; local adaptations live in `src/`.
- The reference WCH package exposes USB and PIOC through headers/registers and
  does not contain `ch32h417_usb.c` or `ch32h417_pioc.c`.

Imported unchanged:

- `core/startup_v3f.S`
- `core/link_v3f.ld`, `core/link_v5f.ld`
- `core/system_ch32h417.c`, `core/system_ch32h417.h`
- `core/timebase.h`
- `include/ch32h417_conf.h`, `include/ch32h417_it.h`,
  `include/ch32h417_port.h`
- `vendor/wch/Core/`, `vendor/wch/Debug/`, and
  `vendor/wch/Peripheral/inc/`
- WCH peripheral sources for RCC, GPIO, DMA, SPI, TIM, IWDG, Ethernet, and
  FLASH. `ch32h417_flash.c` was added when `main_v3f()` began calling
  `SystemInit()`: `core/system_ch32h417.c:131` has `SetSysClock()` call
  `GPIO_IPD_Unused()` unconditionally, that lives in `ch32h417_gpio.c`, and it
  calls `FLASH_BOOT_GetMode()`. Without it the V3F image does not link. Copied
  byte-for-byte from the reference platform; its header was already vendored.
  The reference links `wildcard vendor/wch/Peripheral/src/*.c` into both cores
  and lets `--gc-sections` sort it out; this tree vendors a selected set, so
  the dependency is named in `V3F_VENDOR_DRIVERS` instead.

Imported, then modified here (each change and its rationale):

- `tools/merge_images.py` — added a required fifth argument, the flash
  ceiling, and a bound on `v5f_offset + len(v5f)`. The tool previously checked
  only that the V3F image did not overrun the V5F offset, so an oversized V5F
  image produced a merged binary that fits nothing and fails at flash time.
  Ceiling is `0x10000 + 128K = 0x30000`, from `core/link_v5f.ld:12`.
- `core/startup_v5f.S` — one immediate: `CPU_RUN_CTLR` (CSR `0xBC0`) at the
  former `:481` changed from `0x1237B3E0` to `0x345AB3E0`. Bits `[31:16]` are
  the four FPU clock dividers (RM V1.7 §4.2.3.7), divisor = field + 1, rule
  `fxxx_freq(max) >= core_clock / fxxx_clkdiv`, V5F maxima 128/96/76/38 MHz.
  WCH's defaults `0x1237` = /2 /3 /4 /8 give 200.0/133.3/100.0/50.0 MHz at
  this platform's configured 400 MHz V5F clock — all four out of spec, so
  WCH's own 400 MHz profile violates its own rule. `0x345A` = /4 /5 /6 /11 =
  100.0/80.0/66.7/36.4 MHz is the minimum legal set. Bits `[15:0]` unchanged.
  The vendor's `/* Configure Prefetch */` comment above the line was replaced
  with the field decode: it is copy-pasted from `startup_v3f.S` and is wrong
  for the V5F, which has no `pipe_acc` field. `core/startup_v3f.S` is
  deliberately untouched — its `0x123703E1` is legal at the V3F's 100 MHz
  against V3F maxima 80/53/40/20, and V3F is soft-float.
- `core/timebase.c` — one line: the 1 MHz prescaler now rounds to nearest,
  `(core_hz + 500000) / 1000000` instead of `core_hz / 1000000`. Truncation
  makes `millis()` run fast whenever the timer clock is not a whole number of
  MHz (72.5 MHz would be 0.69 % fast). Exact at both 70 MHz and 100 MHz, so it
  is latent, not observable. Changed in the commit that first compiles the
  file; there is no bisect value in a broken-then-fixed pair for dead code.

  **Open, for whoever wires the tick (roadmap Phase 2 Task 5):** the file is
  compiled into the V5F image but its header comment and its `core_hz`
  fallback both assume V3F. TIM3 is an HB1 peripheral and HB1 is clocked from
  **HCLK**, which is 100 MHz under the enabled profile. On V3F that equals
  `SystemCoreClock`; on V5F it does not — `SystemAndCoreClockUpdate()` gives
  the V5F `SystemCoreClock = SystemClock >> HPRE` (400 MHz), matching
  `RCC_GetClocksFreq`'s `Core_Frequency` split at
  `vendor/wch/Peripheral/src/ch32h417_rcc.c:625-629`. **A V5F caller must pass
  `HCLKClock`, not `SystemCoreClock`**, or `millis()` runs 4x fast.
