# MCXN947 controller

Replacement for the CH32H417 on the PMOD-A injection link, on an **FRDM-MCXN947**
development board with an **LCD-PAR-S035** panel.

**The FPGA side does not change.** No gateware edit, no netlist perturbation, no
re-sweep. That is the most important scoping fact here and belongs in the PR
description.

Companion to `RP2350_CONTROLLER.md`, which records the alternative that was
evaluated and not chosen; §1 says why. Both are tracked repository
documentation. They are design records rather than plans of record: §10 lists
what is derived rather than measured, and nothing here has been built.

Grounded in: the repo; UM12018 Rev 2.0 (board UM); MCXN947 datasheet Rev 8.2
(11 June 2026); errata MCXNx4x_0P02G Rev 1.0 and MCXN_1P02G Rev 4.0
(17 March 2026); AN14300; and the locally unpacked MCUXpresso SDK 24.12.00 at
`~/mcuxpresso/02/SDKPackages/SDK_24_12_00_FRDM-MCXN947-2`. Claims that could not
be grounded are marked **UNVERIFIED** in §10 and nowhere else.

---

## 1. Why this part

The decisive difference against the RP2350 is that **the MCXN947 has a specified
SPI slave and the RP2350 does not.**

RP2350's PL022 is disqualified twice: a 12.5 MHz slave ceiling (`SSPCLK/12`), and
— rate-independently — Motorola SPI with SPO=0/SPH=0 requires the master to raise
CS *between each transfer*, which a 256-bit continuous-CS slot violates. PIO is
therefore mandatory rather than preferable, and there is no official PIO SPI
slave in the datasheet or in `pico-examples`. The timing case for it also rests
on a PIO-output-to-pad delay Raspberry Pi does not publish.

The MCXN947 side, measured rather than argued:

| parameter | requirement | MCXN947 | margin |
|---|---|---|---|
| Slave TX frequency (LP1, OD, LPSPI6–9) | 15 MHz | 30 MHz | 2x |
| Data valid after SCK (LP10, LPSPI6–9) | < 33.3 ns | 13 ns | 2.5x |
| Frame size | 256 bits, one per CS | `FRAMESZ=255` | exact |

The 33.3 ns figure is ours, not NXP's: `spi_link.py` latches MISO at t = 50.0 ns
after the SCK fall, so a slave valid *at* that edge has zero setup and fails. A
simulation sweep decodes at 0 / 16.7 / 33.3 ns and fails at 50.0 ns;
`test_slave_presenting_at_the_latching_edge_does_not_decode` pins the boundary.
**Quote 33.3 ns, never 50.0** — the gap between them is setup time, not headroom.
For comparison the CH32's `tV(SO)` is 25 ns, so this is a ~2.5x improvement on
the single tightest electrical parameter in the link.

Honest costs, stated as plainly: ERR051588 is live on current silicon (§4); the
part is badly overspecified for a 13.7%-duty SPI slave (the NPU, Ethernet,
CAN-FD and 2 MB flash are all dead weight); and for production the friendly
package, VPBT (172-pin 0.65 mm QFP, keeps USB HS), carries an 84-piece MOQ and a
16-week lead time. Prototype on FRDM; check VPBT lead time before committing a
layout.

---

## 2. Hardware allocation

### The link — mikroBUS J6, LPSPI6

`LP_FLEXCOMM6`. **LPSPI0–2 cannot carry this link** — 12.5 MHz slave TX ceiling,
below the 15 MHz SCK — and the board's Pmod header J7 is wired to FC0 *and* is
DNP, so it is doubly unusable. Use the mikroBUS socket.

| J6 pin | signal | MCU pin | FlexComm signal | PORT mux | note |
|---|---|---|---|---|---|
| 3 | `ME_FC6_SPI_CS` | P3_23 | `FC6_P3` = PCS0 | **ALT2** | the odd one out |
| 4 | `ME_FC6_SPI_CLK` | P3_21 | `FC6_P1` = SCK | ALT3 | also Arduino J1 pin 15 |
| 5 | `ME_FC6_SPI_MISO` | P3_22 | `FC6_P2` = SIN pad | ALT3 | MCU drives it; see PINCFG |
| 6 | `ME_FC6_SPI_MOSI` | P3_20 | `FC6_P0` = SOUT pad | ALT3 | MCU reads it; see PINCFG |
| 8 | GND | — | — | — | bridge GND only, not 3V3 |

**The mux column is measured, and P3_23 really does differ from its three
neighbours.** Reading `FC6_Pn`'s position in the `pin_signal` string (e.g.
`PIO3_20/WUU0_IN27/TRIG_OUT0/FC8_P4/FC6_P0/...`) as an ALT index is wrong —
that string also lists functions occupying no mux slot. Pairing every
`/* Pin is configured as FCn_Pm */` comment in every FRDM/EVK `pin_mux.c` with
the `kPORT_MuxAltN` it programs gives a rule with no counterexample across 787
configured pins: a pin offering one FlexComm puts it at ALT2; a pin offering two
puts the first at ALT2 and the second at ALT3. P3_20/21/22 list FC8 before FC6,
so FC6 is ALT3; P3_23 lists FC6 alone, so there it is ALT2. Muxing the chip
select to ALT3 with its neighbours leaves PCS0 unconnected and the slave never
frames — and the symptom is not an error, it is total silence with every FPGA
counter flat, which is indistinguishable from the link never being attempted.

**`CFGR1[PINCFG]` must be 3, because the socket's net names assume the board is
master.** J6's MOSI net lands on `FC6_P0`, which is the LPSPI **SOUT** pad, and
its MISO net on `FC6_P2`, the **SIN** pad. (`FCn_P0` is SOUT: the SDK's own
LPSPI slave example labels `PIO0_24/FC1_P0/...` as `LPSPI1_SOUT`.) As the
*slave* we must read the MOSI net and drive the MISO net — the opposite of the
default. `PINCFG` 0b11 is "SOUT is used for input data; SIN is used for output
data", which is exactly that swap. At the default 0b00 the MCU would drive
P3_20 into the FPGA's own output driver and listen on a wire nobody drives.

`mcu_ready` on J5 pin 2 (`ME_INT`, P5_7) as plain GPIO. Nothing may be populated
on Arduino J1 pins 5 or 15.

**Core voltage mode is load-bearing.** LP1 is quoted per drive mode: Overdrive
gives LPSPI6–9 30 MHz, Standard Drive 25, Mid-Drive 25 — and takes LPSPI3–5 to
12.5. A routine power optimisation silently becomes a protocol violation. Run
Overdrive at 150 MHz. LP2 additionally requires SCK ≤ f_periph/4, so FlexComm 6
must be clocked ≥ 60 MHz; do not attach it to FRO12M — **and not to FRO_HF
either.** `BOARD_BootClockPLL150M` leaves FRO_HF at 48 MHz (its own YAML header
says `{id: FRO_HF_clock.outFreq, value: 48 MHz}`; its body calls
`CLOCK_SetupFROHFClocking(48000000U)`), which caps SCK at 12 MHz against the
15 MHz the FPGA clocks. `kFRO_HF_DIV_to_FLEXCOMM6` is the attach ID anyone
reaching for "the fast FRO" would pick and it is a silent protocol violation.
PLL0 is already at 150 MHz from the same profile, so route it through PLLCLKDIV
— untouched by the board profile, so set it explicitly — and halve it to
75 MHz, which is SCK × 5. `link.h` static-asserts the ≥ 4× SCK relation.

### Frame phase — `usb_sync` on Arduino J3 pin 3

`usb_sync` is `sof_tick & ~sof_tick_d`, a **one-cycle 16.67 ns strobe**. Too
narrow for GPIO polling; it needs timer input capture with the input filter
disabled.

Use **P1_22, ALT4 = `CT_INP14`**, on Arduino J3 pin 3 (a populated 2x8 header,
no DNP). No conflict with FC6 (Port 3), the display (Ports 0/2/4), USB or SWD;
the only other consumer is camera J9 pin 24, and the camera is unused.

Rejected, recorded so they are not re-proposed:

- **P5_7** — *no CTIMER capture function at all* (ALT0 GPIO, ALT1 `TRIG_IN11`,
  ALT3 `TAMPER5`, analog `ADC1_B15`), and an always-on VDD_BAT pad besides.
- **P1_8** (`CT_INP8`) — MCU-Link UART RXD *and* the default UART ISP pin.
- **P1_14** (`CT_INP10`) — LAN8741 PHY `ENET_RXD0`; the PHY would drive it.

**The CTIMER instance is a free choice, not a pin constraint.**
`fsl_inputmux_connections.h` exposes a full cross-bar — `CT_INP0..19` reach
CTIMER0..4 via `INPUTMUX->TIMERnCAPTSELm`. Use **CTIMER2**, leaving CTIMER0 free
for the SCTimer/PWM work that examples reach for first.

### Display — J8 FlexIO header, FlexIO0 8080 16-bit

The panel is an **LCD-PAR-S035**: 480x320, **ST7796S** controller. J8 is
populated (no DNP marker, unlike J7 and J12) and the UM names the S035 as one of
the two panels it exists for, so it mates directly — no adapter.

Not SPI. The only spare 4-wire SPI pinned to a header is FC1/LPSPI1 on Arduino
J2, which caps at 25 MHz master *and* is the MCU-Link USB-to-SPI bridge
interface. FlexIO0 8080 16-bit is 8x faster and collides with nothing.

Data on `FLEXIO0_D16..D31` (P2_8–P2_11, P4_12–P4_23); WR = P0_9 (`FLEXIO0_D1`),
RD = P0_8 (`FLEXIO0_D0`); CS = P0_12, D/C = P0_7, RST = P4_7 as GPIO.
**No J8 pin is on Port 3**, so there is zero physical overlap with the link.

**R34 / R28 (USB1_OTG_PWR / OC on P4_16 / P4_17) stay DNP.** That is what keeps
P4_16/P4_17 free as `FLEXIO0_D24/D25` and the bus 16 bits wide. This is
reversible with a soldering iron, so it is a standing constraint and belongs in
the display module's header next to the bus-width configuration — whoever
populates those resistors will be doing USB power work and will not read display
commits.

Budget at 12.5 MHz WR (`baudRateDiv = 6`, 80 ns tWC), 1 RGB565 pixel per WR:
full frame 12.3 ms, text row 614 µs, aggregate 25 MB/s. **No full framebuffer** —
307,200 B for a page of hex, when the panel's own GRAM already holds the image.
Two 480x16 row buffers ping-ponged plus character/attribute shadows, ~33 KB.
Render at 20 Hz from a snapshot, diffing dirty cells coalesced into row runs.

### USB — J11 only

UM12018: *"only the HS USB controller and PHY interface is used and it is
connected to the USB Type-C connector (J11)."* `USB0_FS` is routed nowhere. J17
is the MCU-Link probe's own USB, a separate LPC MCU.

**Bench consequence: J17 gives you SWD and no console; J11 gives you the console
and no debugger. Plug in both cables.**

---

## 3. Core split

**CPU0 — everything that touches the wire protocol, including the console.**
LPSPI6, eDMA0 ch0/ch1 and their IRQs, ERR051588 recovery, `mcu_ready`, the
CTIMER2 capture, `spi_frame.c`, `link.c`, `map_upload.c`, `console.c` +
TinyUSB, and the WWDT.

**CPU1 — pure presentation.** ST7796S/DBI/FlexIO0 + eDMA1, the status LED
ladder, the seqlock reader. Nothing else.

### Why the console is on CPU0, not CPU1

The instinct is to keep CPU0 pristine. The FIFO finding inverts it: **LPSPI6's
FIFO is 8 x 32 bits = 256 bits = exactly one 32-byte slot**, so the whole
outgoing frame is staged before CS ever falls. CPU0 has no latency deadline at
all — it has a *staging window* of 107.93 µs, which is 16,190 cycles at 150 MHz.
A ChipIdea HS bulk ISR plus `tud_task()` plus `link_poll()` plus `console_poll()`
do not come close, and missing the window is benign (§4).

Against that, a console on CPU1 costs three real things: every injection command
would cross the core boundary, turning a one-way snapshot into a bidirectional
command queue; the console is the thing you debug the link *with*, so a display
crash would take out your only diagnostic channel exactly when you need it; and
it would make CPU1 load-bearing by the back door.

So: **CPU0 = link + console as one coherent unit; CPU1 = presentation; IPC is
strictly one-way CPU0 -> CPU1, data only, never commands.** That asymmetry is
what makes the safety invariant cheap rather than a discipline problem.

### Watchdog stays on CPU0

The WWDT that can reset the part is CPU0's, and its health bits are CPU0's
subsystems only — link slot progress, DMA re-arm delta, USB serviced. **CPU1
liveness is never a health bit**: if it were, a display crash would reset the
link, the exact inversion §4 forbids. CPU1 gets no hardware watchdog; its
liveness is *observed* by CPU0 via a heartbeat word and *reported* on the
console. Any automatic re-release of CPU1 must sit behind an explicit console
command — an automatic one turns a display bug into a self-concealing reset loop.

Status LED is CPU1's (a frozen LED is *informative* — it is the visible symptom
of CPU1 being down), with one carve-out: **CPU0 drives the red LED directly on a
hard link fault**, bypassing CPU1, so a dead CPU1 cannot hide a link fault.

---

## 4. The safety invariant

> **The display is non-load-bearing. `mcu_ready` is a function of CPU0 state
> only. If CPU1 hangs, crashes, is never released from reset, or was never
> flashed at all, the FPGA link runs normally and every FPGA-side counter stays
> flat.**

Guaranteed structurally, not by discipline:

**(a) Boot ordering — `mcu_ready` is raised before CPU1 exists.**

```
1. SPC overdrive -> BOARD_BootClockPLL150M() -> flash wait states
2. LPSPI6 pin mux; eDMA0 TCD rings written; BOTH TX buffers seeded with a
   complete valid IDLE slot
3. ERQ on both channels; LPSPI6 enabled
4. raise mcu_ready                  <-- THE LINK IS LIVE HERE
5. zero the shared window
6. USB PHY bring-up + tud_init()
7. arm WWDT
8. release CPU1                     <-- THE DISPLAY STARTS HERE, LAST
9. enter foreground loop
```

Step 4 strictly precedes step 8. There is no execution order in which CPU1 can
influence whether the link comes up.

**(b) CPU1 owns no resource the link path needs** — different serial peripheral,
different DMA controller, different SRAM region, different flash image, disjoint
IRQ set, disjoint pins. Enforced statically by §7 rung 5.

**(c) The IPC is lock-free and one-way.** The writer cannot wait on CPU1 under
any circumstance, including CPU1 halted mid-read. This is the property a
hardware semaphore would destroy.

**(d) CPU1 is never a watchdog health bit.**

### The steady-state invariant that makes a missed deadline benign

> The TX buffer the DMA will next read is never empty and never partially
> written. It holds the previous slot's complete bytes until fully overwritten.

A missed staging window therefore puts a **byte-identical repeat** on the wire.
The FPGA's sequence classifier scores `delta == 0` as DUPLICATE — drain, do not
act, do not advance. No malformed frame is ever emitted and every `spi_bad_*`
counter stays flat. Two corollaries: both TX buffers must be seeded with a
complete valid IDLE frame before `ERQ` is set (zeroed buffers fail SOF), and only
ever refill the buffer the DMA is *not* pointing at.

### ERR051588

Confirmed live on **both** mask sets on disk, neither carrying a "fixed in" note:

> *Transmit FIFO pointers are corrupted when a transmit FIFO underrun occurs
> (SR[TEF]) in slave mode.* Workaround: reset the transmit FIFO (`CR[RTF] = 1`)
> before writing any new data.

It does not self-heal, and it presents as a `spi_bad_sof`/`spi_bad_crc` storm
with `spi_slots` still advancing — a confusing signature. Underrun is
structurally unreachable in steady state (whole slot pre-staged), so it is
reachable only across reset, a debugger halt, or a re-arm window. Recovery:

1. **Drive `mcu_ready` LOW** — the FPGA's `send_message = mcu_ready & tx_queued`,
   so this stops the FPGA consuming TX frames, and all four error counters are
   gated on `transfer_ready`, so the garbage slots are *ignored* rather than
   counted.
2. Clear `ERQ` on both channels; wait for `CHn_CSR[ACTIVE] = 0`.
3. `CR[RTF] = 1` **and** `CR[RRF] = 1`; clear `SR[TEF]`. RTF must precede any new
   TX write.
4. Clear `CHn_CSR[DONE]`, rewrite both TCD rings, re-seed both TX buffers.
5. Re-enable `ERQ`. 6. Raise `mcu_ready`.

Count recoveries; a nonzero count is a bug, not routine. Note `fsl_lpspi.c`
already carries an ERR051588 workaround, but only inside the transactional slave
API which this firmware does not use — record that deliberate divergence in
`PROVENANCE.md`.

---

## 5. IPC — a versioned seqlock, nothing more

```c
typedef struct {
    uint32_t seq;               /* even = stable, odd = write in progress */
    uint32_t slot_counter;
    uint32_t native_report_count;
    uint16_t usb_frame, usb_subframe;
    uint16_t link_flags;        /* INJ_LINK_STATUS_FLAG_* */
    uint16_t descriptor_generation, map_generation, fault_flags;
    uint8_t  last_rx_sequence;
    uint8_t  _pad[3];
} link_snapshot_t;              /* 32 bytes */
```

Writer (CPU0, 8 kHz): `seq++` (odd) -> `__DMB()` -> stores -> `__DMB()` ->
`seq++` (even). Reader (CPU1, 20 Hz): retry while `s0 != s1 || (s0 & 1)`, bounded
to 4 attempts then render the last good copy. Writer critical section is tens of
ns against a 125 µs period; collision probability per read ~1e-4.

Both halves are pure functions of a struct pointer, so `shared_link_status.c` is
**unguarded and host-tested** — a unit test interleaves writer and reader at
every offset and asserts no torn read.

**Rejected, with reasons:**

- **SEMA42** — makes the writer capable of *blocking on the reader*. A CPU1 crash
  while holding the gate stalls CPU0's `link_poll` and the link dies because the
  display died. Directly violates (c). Also unnecessary: single-writer /
  single-reader with a versioned buffer needs no mutual exclusion.
- **MAILBOX** — a doorbell, when CPU1 wants the latest value on its own 20 Hz
  cadence, not to be woken 8,000 times a second. 400 spurious interrupts per
  useful one. Keep in reserve for a rare edge signal ("link faulted, repaint
  now"); adding it later is purely additive.
- **RPMsg-Lite / eRPC** — wrong shape (we publish state, we do not send
  messages), wrong failure semantics (vring indices are shared mutable state on
  *both* sides, so a CPU1 crash mid-transaction leaves a ring CPU0 must reason
  about — the entire design point is that CPU0 never reasons about CPU1), and
  wrong cost (hundreds of lines of target-only guarded code in a tree whose
  top-priority property is that the guarded count goes *down*). eRPC is worse
  still: bidirectional call/return means the display can block the link path by
  definition.

One line for the PR: **we are publishing state, not sending messages, and the
consumer is allowed to die.**

**Cache coherency.** The Cortex-M33 here has no core data cache. LPCAC sits on
the flash/NVM path; CACHE64 belongs to FlexSPI, unused. On that reading SRAM is
coherent across cores and the seqlock needs `volatile` + `__DMB()` for *ordering
only*. Strongly supported, **UNVERIFIED** against the RM — so design so that
being wrong is cheap and visible: **put `slot_counter` on screen from the first
render.** It advances 8,000 times a second, so a frozen `slot_counter` against a
console reporting a healthy link is the unambiguous signature of a stale read.
Escalate cheapest-first if it happens (`__DSB()`, then disable LPCAC, then move
the window to SRAMX at 0x04000000). Do **not** pre-emptively disable LPCAC —
turning off a real feature to fix a bug you have not observed is how you end up
not knowing which change mattered.

Separately: **eDMA buffers on both cores need a non-cached story regardless**,
now sharper because the display's rows are DMA-written at 20 Hz from CPU1 while
the link's slots are DMA-written at 8 kHz from CPU0, in different regions, and a
coherency bug in either presents as a framing bug.

---

## 6. Memory map

Verified directly in the vendored scripts.

**Flash** — `m_core1_image` ORIGIN in the core0 script *is* `m_interrupts` ORIGIN
in the core1 script, so the merge offset cannot drift:

| region | range | size | owner |
|---|---|---|---|
| core0 vectors + text | 0x00000000–0x000C0000 | 768 K | CPU0 |
| core1 vectors + text | 0x000C0000–0x00100000 | 256 K | CPU1 |
| `m_flash1` | 0x00100000–0x00200000 | 1 M | unused |

**SRAM** — 512 K total: RAMX 96 K at 0x04000000 (code bus) + RAMA..RAMH 416 K
contiguous at 0x20000000.

| region | range | size | owner |
|---|---|---|---|
| `m_sramx` | 0x04000000–0x04018000 | 96 K | CPU0 code bus; fallback home for the shared window |
| core0 `m_data` | 0x20000000–0x2004C000 | 304 K | CPU0 + link DMA buffers |
| **shared window** | 0x2004C000–0x2004E000 | 8 K | snapshot + CPU1 heartbeat |
| core1 `m_data` | 0x2004E000–0x20068000 | 104 K | CPU1 + display buffers (~33 K) |

**The shared window costs zero linker deviation.** Both scripts already declare
`RPMSG_SHMEM_SIZE = DEFINED(__use_shmem__) ? 0x2000 : 0`, both reserve
`rpmsg_sh_mem` at `0x2004E000 - RPMSG_SHMEM_SIZE`, both exclude it from `m_data`,
and both emit a `.noinit_rpmsg_sh_mem (NOLOAD)` section collecting
`*(.noinit.$rpmsg_sh_mem*)`. Link both images with
`-Wl,--defsym,__use_shmem__=1` and place the snapshot there. **We are reusing
RPMsg's address reservation, not RPMsg.** Both `.ld` files stay byte-exact.

This mirrors the CH32 exactly (`RAM_SHARED` 8 K declared in both scripts, gated
by `check_images.py --ram-shared-start/end`). CPU1 cannot allocate into the
shared window or CPU0's region — enforced by the linker, since the core1 script's
`m_data` is the only RW region it has.

---

## 7. Build

**Vendor a subset. Not west, not Zephyr.** `make firmware-test` must not acquire
an SDK or CMake dependency, and the SDK being already unpacked is an argument for
copying out of it rather than depending on a fetcher. The repo has twice already
chosen to vendor. What would change this: Ethernet coming off the deferred list
(lwIP is where hand-vendoring stops paying), or the display needing LVGL.

**Three upstreams**, all recorded in one `PROVENANCE.md`:

1. MCUXpresso SDK 24.12.00 for FRDM-MCXN947 — BSD-3-Clause. Device headers,
   `periph/` whole (pruning a generated header would violate byte-exactness),
   drivers (`fsl_common`, `clock`, `reset`, `spc`, `gpio`, `port`, `edma`,
   `lpspi`, `lpflexcomm`, `ctimer`, `wwdt`, `inputmux`, `flexio*`), CMSIS core,
   ~~`middleware/usb/phy/usb_phy.{c,h}` (the only file from `middleware/usb/`)~~,
   `project_template/{clock_config,board}.{c,h}`, `boot_multicore_slave.c`, and
   the display stack: `components/display/st7796s`,
   `components/video/display/dbi/fsl_dbi`, `.../dbi/flexio/fsl_dbi_flexio_edma`.

   **`usb_phy.{c,h}` cannot be "the only file from `middleware/usb/`", and was
   rejected at step 4 rather than taken.** Measured: `usb_phy.c`'s first
   include is `usb.h`, which it needs for `kUSB_ControllerEhci0` and the
   `kStatus_USB_*` returns; `middleware/usb/include/usb.h` includes
   `usb_misc.h`, `usb_spec.h` and `fsl_os_abstraction.h`. So taking one file
   takes 944 lines of the NXP USB stack's common headers plus the OSA layer —
   both excluded by name below. `USB_EhciPhyInit()`'s whole contribution on
   this part (no ANATOP, no CCM analog) is `TRIM_OVERRIDE_EN`, two `CTRL` bits,
   `PWD = 0` and the `TX` trim, and the SDK's own `USB_DeviceClockInit()` and
   TinyUSB's `hw/bsp/mcx/family.c` state the surrounding clock sequence
   identically register for register. It is written out in
   `src/usb_console.c`; see `firmware/mcxn947/PROVENANCE.md` deviation 9.
2. **NXP hal_nxp / mcux-sdk classic layout** — for the four files SDK 24.12.00
   does **not** ship: `MCXN947_cm33_core{0,1}_flash.ld` and
   `startup_MCXN947_cm33_core{0,1}.S`. The SDK ships only the MCUXpresso-IDE
   managed-linker startup and *no* `.ld` at all. Available locally at
   `~/zephyrproject/modules/hal/nxp/mcux/mcux-sdk/devices/MCXN947/gcc/`.
3. `hathach/tinyusb` at a pinned tag — **MIT**, a third licence in the tree.
   Pinned at **`0.20.0`** (`3af1bec1`, 2025-11-20) at step 4: the newest
   release tag, and a descendant of the local clone's own HEAD.

**Excluded, deliberately:** the NXP USB device stack, FreeRTOS, lwIP, mbedTLS,
LVGL/emWin, MCMgr (§8), and **`fsl_dbi_flexio_smartdma`** — SmartDMA is a *third*
bus master and admitting it would put an engine nobody has reasoned about against
the link's arbitration budget.

`.gitattributes`: add `firmware/mcxn947/vendor/** -whitespace`.

**The two upstreams are different SDK vintages, which matters for the vector
table.** Checked at step 1: hal_nxp's `startup_*.S` vector block against
24.12.00's own startup is 172 entries in identical order, no slot inserted or
removed, and slot indices agree with `IRQn_Type` at `EDMA_0_CH0` (1), `CTIMER2`
(34), `LP_FLEXCOMM6` (41) and `QDC0_COMPARE` (124). Only *names* diverge, at
slots this design does not use: hal_nxp says `ENC0_*`/`ENC1_*` where 24.12.00
renamed the peripheral to QDC, so **anyone wiring a QDC interrupt must define
the `ENC*` handler name while the NVIC enum reads `QDC*`**; and hal_nxp's
`SM3`/`TRNG0` at slots 165-166 are reserved in 24.12.00.

**Do not take board pin definitions from another MCXN947 project's SDK export.**
A local one (`~/git/dm-mcx-streamdeck`) declares `BOARD_NAME "FRDM-MCXN947"`
while mapping the RGB LED to GPIO3[2:4] active-high — the MCX-N9XX-EVK pinout.
The FRDM board package in 24.12.00 is authoritative: **P0_10 red, P0_27 green,
P1_2 blue, PORT mux ALT0, active LOW.**

### `--gc-sections` and `make check`

The lesson transfers and is *worse* here: `startup_MCXN947_cm33_core*.S` declares
every handler `.weak`, exactly as WCH's does, so without a strong-symbol root the
real handler is dropped and the weak `DefaultISR` stub is linked instead.
**Two retention root sets now, one per image.**

Geometry declared once in the Makefile and passed to both `merge_images.py` and
`check_images.py`. `make check` asserts, for **both** images:

1. Every `--undefined=` root is defined in its own image **and does not resolve
   to `DefaultISR`** — the single most likely silent failure on this part.
   **An address comparison alone is not sufficient, and "one line of `nm`" was
   wrong.** Measured at step 1: the startup has a *two-level* dispatch and
   three distinct weak shapes.

   ```
   CTIMER2_DriverIRQHandler  W  0x4f8   == DefaultISR
   CTIMER2_IRQHandler        W  0x598   weak trampoline, own address
   HardFault_Handler         W  0x500   weak self-loop, own address
   SysTick_Handler           T  0x1830  ours, strong
   ```

   The vector table names `<NAME>_IRQHandler`, which is a weak trampoline that
   branches to `<NAME>_DriverIRQHandler`; only the *Driver* name is `.set` to
   `DefaultISR`. So rooting the vector-table name and comparing addresses
   returns PASS against NXP's own trampoline while the handler is unimplemented
   — the exact failure the rung exists to catch. **For a peripheral vector the
   root that means anything is `<NAME>_DriverIRQHandler`.** Test the `nm`
   binding letter (weak vs strong) as well as the address, and that the named
   owning object defines it.
2. Object survival per core.
3. Image geometry — and **core1's lowest LOAD PhysAddr equals `CORE1_OFFSET`**,
   read from the ELF rather than trusted from the Makefile. That is the
   anti-drift check.
4. RAM geometry, and neither image allocating into the shared window.
5. **Forbidden-symbol lists, which make §4(b) static rather than a convention:**
   no LPSPI6 / LP_FLEXCOMM6 / eDMA0 / CTIMER2 symbol in `core1.elf`; no FlexIO /
   eDMA1 / ST7796S / DBI symbol in `core0.elf`.
6. CPU1-release MMIO assertion (`SYSCON->CPBOOT`, `SYSCON->CPUCTRL`) — the
   analogue of the CH32's `WAKEIP` check. **Its absence produces a working link
   and a black screen**, which is the most easily overlooked failure in the tree.
7. Merged-image bounds.

### Host-testability

Guard macro `#if defined(MCXN947)` — **same polarity as today, and the polarity
is the load-bearing part**: MMIO goes inside the guard, so a new file with no
guard is portable *by default* and lands in `make firmware-test` for free. One
module = one `.c`, portable half first, at most one trailing guard block.

Guarded lines go from ~1104 to roughly 300; `usb_cdc_fs.c` (644 lines, 627
guarded) collapses to ~17 lines of TinyUSB glue plus descriptors, and the two
genuinely new modules (`map_upload.c`, `console.c`) are 100% portable by
construction. **Mechanize it**: `make firmware-guardcount` fails if the total
exceeds a number checked into the Makefile. Ratchet down as files land.

Rule for the Makefile comment: **if a host test ever needs `-Ivendor`, the module
is mis-split.**

### Toolchain, debug, CI

`CROSS_COMPILE ?= arm-none-eabi-`. One variable, no probe, no `$(HOME)` fallback.
`arm-none-eabi-gcc` is already installed by the existing Teensy CI job; reuse
that step. (The current `firmware-ch32` job's exit 127 is *not* a Makefile
double-dash bug — `CC` evaluates correctly with or without the tool present. The
likely cause is the npm global-path step; moot, the job is being deleted.)

Flash with **LinkServer**, NXP's own tool. `flash` is never a CI target.

**This section previously said J-Link, and that was wrong.** The FRDM-MCXN947
ships its on-board MCU-Link running *CMSIS-DAP* firmware — it enumerates as NXP
`0x1fc9:0x0143`, "MCU-LINK FRDM-MCXN947 (r0E7) CMSIS-DAP V3.128" — which
`JLinkExe` cannot drive at all. Reaching J-Link would mean reflashing the
MCU-Link with SEGGER firmware: reversible, but it takes the board away from
MCUXpresso IDE and from every NXP example, in exchange for nothing.

LinkServer is the vendor tool for that probe and that part, ships with
MCUXpresso IDE, and identifies the target unaided:

```
  #  Description                                    Serial         Device    Board
  1  MCU-LINK FRDM-MCXN947 (r0E7) CMSIS-DAP V3.128  AQE3AU1FLFDNJ  MCXN947   FRDM-MCXN947
```

`make -C firmware/mcxn947 flash` loads `core0.elf` — the ELF rather than the
`.bin`, so the load addresses come from the linker script and there is no
`--addr` to drift. Verified on hardware 2026-09-16: two sectors written, target
reset, blink running. `make probes` lists what is attached.

---

## 8. Core1 boot — raw register release, not MCMgr

`boot_multicore_slave.c` (47 lines, BSD-3, already in the vendor set, gated on
`__MULTICORE_MASTER`): set `SYSCON->CPBOOT` (0x50000804) to the core1 vector
address, then `SYSCON->CPUCTRL` (0x50000800) with key 0xC0C4 to enable the clock
and release reset.

**MCMgr is rejected because its entire value is the thing the invariant
forbids.** `MCMGR_StartCore(kMCMGR_Start_Synchronous)` *waits for CPU1 to signal
back* — putting a wait-for-CPU1 into CPU0's boot path is the most direct possible
way to break "if CPU1 never starts, the link still runs." Used safely it would
have to be asynchronous with no startup data, at which point it is three lines of
wrapper over three stores, plus ~600 lines of target-only code against the
guarded-line ratchet. Raw release is auditable and assertable by rung 6.

Set `CPBOOT` to the literal `CORE1_OFFSET` rather than the embedded-blob symbol —
a `--defsym` is easier to assert than a section placement. That is a one-line
local deviation; record it in `PROVENANCE.md`.

---

## 9. Migration order

The link is proven on hardware before anything else is ported, and CPU1 does not
appear until it cannot affect that. Acceptance is always external: the FPGA's own
counters over JTAG.

1. **Skeleton + toolchain (CPU0 only).** Clock, blink, `make check` green.
   Isolates linker script / startup / retention roots / J-Link before they can be
   confused with a link problem.
2. **LPSPI6 + eDMA0 ring armed -> `mcu_ready` raised -> TX permanently IDLE.**
   *Gate: every `spi_bad_*` and `spi_queue_full` flat **while `link_ready = 1`**,
   `spi_slots` advancing at 8 kHz, and `map_active` never leaving 0.*
   `spi_bad_sof` flat proves byte order (**the `TCR[BYSW]` question resolves here
   and nowhere else**); `spi_bad_crc` flat proves bit alignment and CPOL/CPHA;
   `spi_slots` stuck at 1 means the TCD ring is not self-loading.

   **An earlier version of this step said "`mcu_ready` low" and gated on
   `spi_slots` advancing with the error counters flat. That gate is vacuous and
   was measured to be so** — it passes with no MCU firmware whatsoever. Two
   facts in the gateware, both verified by reading the RTL and confirmed on the
   bench at 8,049 slots/s against a board running only step 1's blink:

   - `spi_link.py:281` increments `slot_counter` on the fixed cadence boundary,
     under a comment reading "A fixed slot boundary is generated independently
     of transfer state". **`spi_slots` at 8 kHz is the FPGA's own cadence and
     says nothing about the MCU.**
   - Every `bad_*` counter is gated on `transfer_ready`, which is latched from
     `mcu_ready` at slot start (`spi_link.py:289`) — e.g.
     `with m.If(transfer_ready & (rx_sof != SOF))` at `:433`. **With `mcu_ready`
     low the FPGA never validates a returned slot, so no error counter can
     increment and "all flat" is meaningless.**

   `mcu_ready` low is the *boot* discipline — hold it low until both DMA
   directions are armed, exactly as `docs/hardware/ch32-cynthion-wiring.md`
   specifies — not the steady state. Raise it once the ring is armed.

   **§4 already knew this and this step contradicted it.** The ERR051588
   recovery sequence above says, of driving `mcu_ready` low: "all four error
   counters are gated on `transfer_ready`, so the garbage slots are *ignored*
   rather than counted" — and *uses* that to suppress counting during recovery,
   ending at "6. Raise `mcu_ready`." Low being a deliberate
   stop-counting-and-ignore state is exactly why it cannot also be the state a
   gate reads counters in.

   **Safety does not come from `mcu_ready` being low; it comes from TX being
   IDLE.** The wire contract's admission predicate is
   `deliverable <=> SOF == 0x68 and known_type and length == expected(type) and
   CRC ok and type != IDLE`, so an IDLE slot is never deliverable, no command is
   ever committed, and no map can activate. `map_active` staying 0 is therefore
   part of the gate rather than an assumption — if it ever leaves 0 with TX
   IDLE, something is wrong with that reasoning and the step should stop.
3. **RX retirement + the portable transport half.** Includes **deliberately
   provoking ERR051588** by halting the core while the FPGA clocks SCK. A
   recovery path that has never run is not a recovery path.
4. **TinyUSB CDC console on CPU0.** *Gate: enumerates at HS, `stats` matches
   JTAG.* **Re-run the step-2 gate under bulk console load** — this is the
   empirical check on §3's core-placement decision.
5. **Two-image build; CPU1 released, doing nothing.** *Gate — the invariant's
   acceptance test, in three configurations:* (a) core1 region erased -> link
   runs, counters flat; (b) both flashed -> same, plus heartbeat advances;
   (c) both flashed, CPU1 halted in the debugger -> identical to (a).
   **If any of the three fails, stop. The split is wrong and no amount of display
   code will fix it.**
6. **Snapshot IPC, no display yet** — CPU1 mirrors `slot_counter`'s low bits onto
   the RGB LED so the IPC is observable before the panel exists. This is where
   the LPCAC question gets answered empirically.
7. **Display.** *Gate: 20 Hz repaint with link counters still flat under a full
   repaint, and 5(c) still passing mid-render.*
8. **Map uploader** — `entries_crc32`, CRC-32/ISO-HDLC (poly 0xEDB88320
   reflected, init/xorout 0xFFFFFFFF) over the concatenated 26-byte MAP_ENTRY
   payloads in transmission order; empty map is 0x00000000. *Gate: `COMMIT_ACCEPTED`
   for a good map **and** `error == CRC` for a deliberately corrupted one* — both
   halves matter.
9. **Injection commands, frame phase, watchdog, status LED.** Watchdog last, as
   `main_v5f.c` does today.
10. **Retire `firmware/ch32h417/`** — own commit, no other change, so the
    `FIRMWARE_ROOT` move bisects cleanly.

Steps 5 and 6 are new and both cheap. Placing them **between** the console and
the display keeps the invariant's acceptance test from being entangled with
display bugs.

---

## 10. Open and UNVERIFIED

- **BOOT-RANDOM FRAME OFFSET — open, and the most serious thing in this list.**
  Measured at step 3: **1 boot in 5 comes up with the whole 256-bit slot
  permanently offset** (4 of 20, with the mitigation compiled out). It is
  invisible to every status register — no underrun, no overrun, no DMA error,
  `SR` reads exactly as on a healthy link — and the only FPGA-side symptom is
  `spi_bad_sof` saturated 1:1 with `spi_slots`, which is the *non-discriminating*
  signature of any whole-slot corruption. It was nameable only once step 3's
  retirement made the received bytes readable: decoded, the stream is the FPGA's
  own keepalive rotated right by a fixed number of bits.

  `link_spi_enable_aligned()` waits for the inter-frame gap on the CS pad before
  `CR[MEN]` and is 20/20 clean against 16/20 without (Fisher one-tailed
  p = 0.053). **The mechanism is not established** and three deliberate attempts
  to reproduce the offset at run time all failed, so the boundary is not simply
  latched at `CR[MEN]`.

  **The framing monitor is not a backstop for this.** It detects the condition
  and runs the recovery ladder ~220 times without ever repairing it; a
  mis-framed boot needs a reset. See `firmware/mcxn947/PROVENANCE.md`.

  Until the mechanism is understood, treat any single-boot measurement on this
  board as one draw from a distribution with a 20% failure mode — including
  every gate in §9 that was accepted on one boot.

  **The mitigation is not complete.** All 40 experiment trials used a
  flash-triggered reset. On 2026-09-16 a board running the default build — gap
  wait present — came up mis-framed after a **VBUS power reset** (plugging in
  J11), with the ladder looping past recovery #959 without repairing. So the
  20/20 result does not generalise to power-on, and the hazard is reduced by an
  unknown amount rather than removed. Whatever explains the mechanism has to
  explain that too.

- **Serial number source.** `SYSCON->DIEID` is *revision and die number*,
  identical across boards of the same revision — wrong for a serial. No UUID
  register in `PERI_SYSCON.h`; SDK `components/silicon_id/` has no MCXN947
  implementation; the datasheet says identity comes from the PUF. Look at
  `devices/MCXN947/drivers/romapi/` first, then the RM's PUF/ELS chapter. Use a
  fixed placeholder meanwhile, and **never put a PUF transaction on the boot path
  between `mcu_ready` and the foreground loop.**
- ~~**`TCR[BYSW]` byte order**~~ — **RESOLVED at step 2: set it.** The eDMA
  moves 32-bit words out of a byte array on a little-endian core, so the word
  reaching the FIFO for slot bytes b0..b3 is `b0 | b1<<8 | b2<<16 | b3<<24`,
  and shifted MSB-first that puts b3 on the wire first. `fsl_lpspi.h`'s
  `kLPSPI_SlaveByteSwap` comment states the case: for a 32-bit frame a buffer
  "1 2 3 4 5 6 7 8" clocks out "4 3 2 1 8 7 6 5" without the flag and
  "1 2 3 4 5 6 7 8" with it. `fsl_lpspi_edma.c` corroborates from the other
  side — on the DMA path there is no software marshalling at all and the driver
  sets `TCR[BYSW]` from that same flag. Confirmed in the built image: `TCR` is
  the literal `0x004000FF`. Still to be confirmed on the bench, which is what
  `spi_bad_sof` flat means.
- **`FCR[TXWATER]`/`[RXWATER]` encoding** — field *widths* are settled from the
  header (both 3 bits, `TXWATER` at bit 0, `RXWATER` at bit 16, so 0–7 against
  an 8-deep FIFO). The threshold *semantics* still want the RM. Step 2 uses
  `TXWATER = 7`, `RXWATER = 0` on the reading that the request asserts while
  the TX FIFO holds ≤ TXWATER words: only that value keeps the FIFO full, and
  §3's "the whole outgoing frame is staged before CS ever falls" is true only
  if it is. A lower value would still work at these rates but would give that
  argument away.
- ~~**CTIMER2's exact IRQ symbol name**~~ — **RESOLVED at step 1.**
  `CTIMER2_IRQHandler`, vector slot 34 (`CTIMER2_IRQn`). But that name is a
  weak trampoline: the root to declare is `CTIMER2_DriverIRQHandler`. See §7
  rung 1.
- **LPCAC does not cache SRAM** — strongly supported; confirm before step 6.
  Detectable by design (frozen `slot_counter`). Narrowed at step 1:
  `SystemInit()` *enables* LPCAC (`SYSCON->LPCAC_CTRL &= ~DIS_LPCAC_MASK`), so
  the question starts from "on by default in every image" rather than from an
  unknown. `SystemInit()` also disables RAM ECC and the aGDET/dGDET chip-reset
  path — worth knowing before trusting either.
- ~~**`NonCacheable` region in the vendored `.ld`s**~~ — **RESOLVED at step 1:
  there is no such region.** `*(NonCacheable.init)` and `*(NonCacheable)` are
  collected into the ordinary `.data` output section in `m_data`, alongside
  `CodeQuickAccess`/`DataQuickAccess`. The attribute buys nothing as the script
  stands, so step 2's two-DMA-engine coherency story needs an MPU region or an
  explicit placement decision — not this.
- **ILI9341 `tWC` = 66 ns** — the figure behind `baudRateDiv = 6`; the panel
  datasheet is not on disk. (S035/ST7796S may differ; confirm for the actual
  panel.)
- **SJ20 pin 2-3** — one schematic glance before soldering to J3 pin 3.
- ~~**TinyUSB `rhport` numbering**~~ — **RESOLVED at step 4: it is 1**, and
  re-reading was worth doing for a reason other than the number. Three
  independent statements in the pinned tree agree: `tusb_mcu.h` sets
  `TUP_RHPORT_HIGHSPEED 1` for `OPT_MCU_MCXN9`; `dcd_ci_hs.c` selects
  `ci_hs_mcx.h` under "MCX N9 only port 1 use this controller"; and
  `hw/bsp/mcx/family.c` calls `tusb_int_handler(1, true)` from
  `USB1_HS_IRQHandler`. But `ci_hs_mcx.h` itself **ignores the argument** —
  `CI_HS_REG(port)` discards `port` and always returns controller index 0 — so
  a wrong value would not fail where you would look for it. What depends on it
  is `TUD_OPT_RHPORT`, `tusb_int_handler()`'s range check against
  `TUP_USBIP_CONTROLLER_NUM`, and the ISR argument matching the port the stack
  was initialised on. Related trap: `tusb_init(rhport, NULL)` ignores its own
  `rhport` and falls back to `TUD_OPT_RHPORT`; pass an explicit
  `tusb_rhport_init_t`.
- ~~**`JLINK_DEVICE = MCXN947_M33_0`**~~ — **MOOT: the flow is LinkServer, not
  J-Link** (see §7). The name itself is confirmed correct, from the SDK's own
  debug configs, which also give `MCXN947_M33_1` for core1 — relevant only if
  someone reflashes the MCU-Link with SEGGER firmware.
- **The MCX Nx4x Reference Manual itself** is login-gated and not on disk. Every
  "needs the RM" item above is blocked on obtaining it.
