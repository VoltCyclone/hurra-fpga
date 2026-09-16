# RP2350 injection controller — design record

**Written 2026-09-15. Self-contained — assumes no prior context.**

Replaces the CH32H417 MCU on the PMOD-A injection control link with an RP2350 Feather.
Motivation is developer experience: the CH32H417 is difficult to work with (hand-rolled
USBFS driver, dual-core image merging, thin vendor documentation). The FPGA side does not
change at all.

## What is being replaced

The CH32H417 currently:

1. Acts as the **SPI slave** endpoint of the FPGA's fixed-slot link on PMOD-A
2. Uploads the injection **field map** (`MAP_BEGIN` / `MAP_ENTRY` / `MAP_COMMIT`)
3. Issues **injection commands** (`RELATIVE`, `BUTTON_STATE`, `PHYSICAL_MASK`, `CLEAR`)
4. Presents a **USB CDC** console to a control host
5. Tracks **frame phase** from the FPGA's `usb_sync` strobe
6. Drives a watchdog and a WS2812 status LED

Per-report mutation happens in *gateware* (`ReportInjectionEngine`), not on the MCU. The MCU
uploads maps and issues low-rate commands; it does no per-report work. This matters for
sizing: no candidate MCU is compute-bound here.

## The link contract (measured from `spi_link.py` / `gateware.py`)

### Electrical

PMOD-A, from `_INJECTION_LINK_PMOD_A` in `gateware.py`. Pin numbers are **physical PMOD
connector pins**; see `docs/hardware/ch32-cynthion-wiring.md`.

| pin | signal | FPGA dir | notes |
|---|---|---|---|
| 1 | `sck` | out | 15 MHz |
| 2 | `mosi` | out | FPGA → MCU |
| 3 | `miso` | in | MCU → FPGA, `PULLMODE=DOWN` |
| 4 | `cs_n` | out | active low, one assertion per 32-byte slot |
| 7 | `mcu_ready` | in | `PULLMODE=DOWN` |
| 8 | `usb_sync` | out | frame-start strobe |
| 9, 10 | `spare0`, `spare1` | in | unused, held high-Z with pull-down |

All `LVCMOS33`. FPGA inputs carry explicit pull-downs because the toolchain otherwise leaves
pads with no pull at all.

> **`mcu_ready` is a trap.** A floating-high `mcu_ready` silently discards every transmitted
> frame: `send_message` dequeues on `mcu_ready`, and the descriptor export only retriggers on
> a **rising** `link_ready` — so the export fires once into a dead MCU and never retries.
> The RP2350 must drive this pin actively, low until genuinely ready.

### Timing

From `SPISlotMaster(clock_hz=60_000_000, slot_cycles=7_500, sck_div=4)`:

- `slot_cycles=7500` at 60 MHz → **125 µs slots (8 kHz)**, aligned to USB microframes
- `sck_div=4`, `half_period = sck_div // 2 = 2` → SCK toggles every 2 cycles → **SCK = 15 MHz**
- 32-byte frame = 256 bits at 15 MHz = **17.1 µs**, i.e. **13.7% duty**, ~108 µs idle per slot
- Aggregate bandwidth 256 KB/s — trivial

**Clock headroom.** `sck_div` must be even and ≥2, so there is a ladder if 15 MHz proves
marginal on the bench:

| `sck_div` | SCK | transfer | duty |
|---|---|---|---|
| 4 (today) | 15 MHz | 17.1 µs | 13.7% |
| 8 | 7.5 MHz | 34.1 µs | 27% |
| 16 | 3.75 MHz | 68.3 µs | 55% |

This is a one-line gateware change. It is the first escape hatch if the slave path misbehaves.

### SPI mode — derived from source, verify in simulation before trusting

- `sck = Signal(init=0)` → idle low → **CPOL = 0**
- MOSI is driven at slot start while SCK is still low (`mosi.eq((SOF >> 7) & 1)`,
  `spi_link.py:302`) and updated in the falling-edge branch (`spi_link.py:403`) → data is
  stable across the rising edge → **CPHA = 0**
- `next_rx_byte = ((rx_byte_shift << 1) | miso_q)[:8]` → **MSB-first**
- `cs_n` asserts at slot start (`spi_link.py:291`), releases after `bit_index == 255`
  (`spi_link.py:417`) → **one 256-bit frame per CS assertion**

**So: SPI mode 0, MSB-first, CS-framed 32-byte slots.** `spi_link.py:320-333` contains a
detailed note about the master's MISO sample instant being one `usb` cycle later than naive
reading suggests — read it before debugging any bit-alignment issue.

### Full-duplex means "pre-stage the reply"

MOSI and MISO exchange within the same 32-byte window. The MCU never computes a response
mid-transfer; its outgoing frame must already be in a DMA buffer when the slot opens. With
~108 µs of slack per slot this is relaxed, but it is a *staging* discipline, not a latency
budget. `usb_sync` exists so the MCU knows where the slot boundary is.

`usb_sync` itself is `sof_tick & ~sof_tick_d` — a **one-cycle frame-start strobe**, not a
clock. Capture its edges.

## Wire protocol

`protocol/report_injection_wire.json` is the single source of truth. Two files are generated
from it and must never be hand-edited:

- `src/hurra_cynthion/injection_wire.py` — Python reference
- `firmware/ch32h417/include/injection_wire.h` — C header (relocate for the new target)

```sh
python3 tools/generate_report_injection_wire.py    # needs ruff on PATH
```

`tests/test_injection_wire.py` gates regeneration (header must equal `render_c(schema)`, plus
idempotency), so a stale or hand-edited file fails the suite.

### Frame format

- SOF `0x68`, total 32 bytes
- 4-byte header: `SOF`, `type`, `sequence`, `length`
- 26-byte payload
- CRC-16/CCITT-FALSE (init `0xFFFF`, poly `0x1021`) over bytes 0..29, little-endian trailer

### The admission predicate every implementation must share

```
deliverable <=> SOF == 0x68 and known_type and length == expected(type)
                and CRC ok and type != IDLE
```

Length is per-type **exact**, not a maximum. `IDLE` ships length 0; everything else ships 26.
Rejection *reasons* are allowed to differ between implementations (they check in different
orders by design) — only admissibility must agree.

### The sequence-classification trap

Sequence classification is NEXT / DUPLICATE / GAP / STALE on the 8-bit delta (`< 0x80` is
forward). **`IDLE` frames are not sequenced** — `tx_sequence` only increments under
`send_message`, and the hardwired idle frame carries sequence byte 0.

Feeding idles into the classifier makes **>99.9% of classifications STALE on a perfectly
healthy link**. Letting the window follow idles is worse: it pins the window to 0 and every
message with sequence ≥ 2 becomes a GAP. Filter idles out *before* classifying.
`tests/test_wire_cross_language.py::test_classifier_is_never_fed_an_idle_sequence` pins this
executably — keep an equivalent for the new target.

### Contract implementations (do not create a fourth casually)

There are three hand-written implementations: the Python reference, `spi_frame.c`, and the
Cynthion gateware (`spi_link.py`). `tests/test_wire_cross_language.py` exists because these
can silently drift — it compiles the real firmware source and diffs behaviour against Python,
because a structural test cannot catch semantic divergence. Any new implementation needs the
same differential treatment.

## What carries over

The tree already separates portable logic from MMIO behind `#if defined(CH32H417)`:

| file | total lines | inside guards | carries over |
|---|---|---|---|
| `spi_frame.c` | 141 | **0** | **100% — verbatim** |
| `ch32_link.c` | 406 | 188 | ~54% (retirement, queue, sequence window) |
| `usb_time.c` | 170 | 89 | ~48% (timing / EMA core) |
| `watchdog.c` | 61 | 21 | 66% (gating policy) |
| `status_led.c` | 136 | 30 | 78% (priority / style policy) |
| `usb_cdc_fs.c` | 644 | all | **0% — replace with TinyUSB** |

Preserve this discipline on the new target (`#if defined(RP2350)` or similar) so the
host-compiled unit tests keep working: `make firmware-test` builds the MMIO-free halves with
`HOST_CC` and needs no cross toolchain. That is a genuinely valuable property — keep it.

## What gets deleted

The CH32's dual-core apparatus is **vestigial** and goes away entirely. `main_v3f.c` is 46
lines whose `main()` calls `SystemInit()` (which programs both cores' clocks, so it must run
on V3F), pokes `WAKEIP`/`SHUTDOWN1` for boot ordering, then spins in `for(;;)`.
`shared_window.h` is included only by `trap_witness.c` — crash bookkeeping, not dataflow. All
real work is on V5F.

Removed: `main_v3f.c`, `startup_v3f.S`, `startup_v5f.S`, `link_v3f.ld`, `link_v5f.ld`,
`tools/merge_images.py`, `tools/check_images.py`'s dual-image assertions, the flash/RAM
geometry constants, the `Core_V3F` / `Core_V5F` / `-Dsystick2` build matrix, the WCH
`vendor/` tree, and `PROVENANCE.md`'s WCH deviations. The 329-line firmware Makefile
collapses substantially.

## RP2350 implementation plan

| concern | approach |
|---|---|
| SPI slave | **PIO** program: mode 0, MSB-first, 256 bits per CS assertion. PIO is a hardware state machine, so slot response does not depend on interrupt latency — this is the main reason to prefer RP2350 over SAMD51, whose SERCOM slave has a 2-deep RX FIFO (`BUFOVF` exposure) and a `t_SOV` MISO-valid ceiling |
| DMA | two chained channels — TX from the staged 32-byte frame, RX into a landing buffer — retriggered per slot |
| CS framing | PIO waits on `cs_n` assert; deassert ends the frame |
| frame phase | capture `usb_sync` edges (PIO, or a timer + DMA timestamp) to replace the TIM8/EMA half of `usb_time.c` |
| host link | **TinyUSB** CDC device. Well-trodden on RP2 — materially easier than the CH32's hand-rolled USBFS |
| core split | optional: core1 owns the slot loop, core0 owns USB + command generation. Not required — 108 µs of slack makes single-core feasible |
| watchdog | RP2350 has one; port the gating policy from `watchdog.c` |
| status LED | NeoPixel via PIO. Note `ws2812_pioc_code.c` already targets the CH32's *PIOC*, which is the same concept — the program structure should map closely |

## Open items to resolve before committing

1. **RP2350 pad erratum.** There is a known early-silicon GPIO/pull-down issue on RP2350.
   **This was not verified** — confirm which stepping you have and whether it affects inputs
   you plan to use. If it bites, the RP2040 Feather has an identical PIO story with less
   headroom and is the de-risk.
2. **Confirm SPI mode in simulation**, not just by reading source. The derivation above is
   from `spi_link.py` line references; validate against the existing Amaranth testbench
   (`tests/test_spi_link.py`) before writing PIO assembly.
3. **Decide `sck_div`.** Keep 4 (15 MHz) or pre-emptively drop to 8 (7.5 MHz) for bench
   margin. Changing it is a one-line gateware edit but re-perturbs the netlist, which matters
   given the open timing problem.
4. **CI**: add a job building the RP2350 image, and keep `spi_frame.c` compiled for the new
   target in the differential test. Note the existing `CH32 firmware build` job is currently
   failing with exit 127 (`riscv-none-elf-gcc` not on `PATH`) — don't inherit that pattern.
5. **Decide the fate of `firmware/ch32h417/`.** Delete, or keep until the RP2350 path is
   proven on hardware. The CH32 link works today.

## Sequencing

**Do not start this until timing closes.** `HEAD` currently produces no bitstream (53.82 MHz
against a 60.00 MHz constraint) — see `docs/handoffs/TIMING_CLOSURE_HANDOFF.md`. The CH32
link is functional on hardware today (`regdebug magic` reads `0xcafe1234` off the board), so
this swap is a developer-experience improvement, not a blocker, and it cannot be validated
end-to-end until a bitstream exists.

Suggested order:

1. Close timing (separate handoff)
2. PIO slot endpoint + DMA, validated against the FPGA with the existing register counters
   (`spi_slots`, `spi_bad_sof`, `spi_bad_crc`, `spi_bad_length`, `spi_bad_type`,
   `spi_queue_full`) as the acceptance signal
3. Port the portable C halves; keep `make firmware-test` green throughout
4. TinyUSB CDC console
5. Watchdog + status LED
6. Retire `firmware/ch32h417/`
