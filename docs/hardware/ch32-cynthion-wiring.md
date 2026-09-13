# CH32H417 ↔ Cynthion board-to-board wiring

Physical wiring for the report-injection control link between the CH32H417
control MCU and the Cynthion FPGA. The logical pin assignments this implements
are in `src/hurra_cynthion/gateware.py`; the wire format the link carries is
`protocol/report_injection_wire.json`.

## Boards

- **Control MCU:** WCH **CH32H417QEU6** (QFN128) USB 3.0 development board — the
  same dev board the Hurra-v3 firmware was developed on. Its on-board WCH-LinkE
  carries flash and debug, so no external programmer is needed. Runs the SPI1
  slave, USBFS CDC-ACM control channel, Ethernet, TIM8 capture, and WS2812.
- **Data plane:** **Cynthion** (Great Scott Gadgets, Lattice ECP5). Runs the USB
  device-clone + injection gateware and is the **SPI master**. **PMOD A** carries
  the link; PMOD B stays free for JTAG or diagnostics.

## Electrical summary

- FPGA is SPI **master**, **mode 0** (CPOL=0, CPHA=0), **SCK = 15 MHz**, one
  32-byte full-duplex slot every **125 µs**.
- CH32 is the SPI1 **slave** with **hardware NSS**.
- Both sides are **3.3 V** logic.
- **Only GND is shared.** Each board is powered from its own USB port. Do **not**
  tie the two 3.3 V rails together — two actively driven rails must not be bridged
  by assumption (design spec §7.1). Leave the PMOD 3V3 pins unconnected unless the
  assembled power architecture explicitly permits a reference/supply.

## Connection table

`user_pmod` is the Amaranth resource index the gateware requests; it equals the
spec's `IO0..IO7`. The ECP5 ball behind each PMOD pin is **revision-dependent**
and resolved by the `cynthion` platform (see "FPGA-side binding" below) — wire to
the **physical PMOD pin**, which is identical across board revisions.

| Signal | Cynthion PMOD A pin | `user_pmod` (spec IO) | Direction | CH32 pin | CH32 function |
|---|---:|---|---|---|---|
| SCK | 1 | IO0 | FPGA → MCU | PA5 | SPI1_SCK (AF5) |
| MOSI | 2 | IO1 | FPGA → MCU | PA7 | SPI1_MOSI (AF5) |
| MISO | 3 | IO2 | MCU → FPGA | PA6 | SPI1_MISO (AF5) |
| CS / NSS | 4 | IO3 | FPGA → MCU | PA4 | SPI1_NSS (AF5, hardware) |
| `MCU_READY` | 7 | IO4 | MCU → FPGA | PA3 | GPIO output |
| `USB_SYNC` | 8 | IO5 | FPGA → MCU | PC6 | TIM8_CH1 (AF3) input capture |
| GND | 5 or 11 | — | — | GND | common ground (**required**) |
| 3V3 — do **not** bridge | 6 / 12 | — | — | — | each board self-powered |
| spare | 9 | IO6 | — | — | reserved |
| spare | 10 | IO7 | — | — | reserved |

The MOSI/MISO directions follow from the FPGA being master: MOSI is master-out
(FPGA → slave), MISO is slave-out (MCU → FPGA).

On the CH32H417QEU6 dev board these land on two headers: SPI1 and `MCU_READY` are
on **J10** (pins 1–5 = PA3/PA4/PA5/PA6/PA7), and `USB_SYNC` is on **J5** at PC6,
because PA2 is not broken out. Confirm the exact J5 pin number for PC6 against the
board silk before wiring.

## PMOD A pin map (Cynthion side)

Standard keyed 2×6 PMOD. Confirm pin 1 against the board silk (squared pad / "1"
marker) before wiring — orientation is easy to mirror.

```text
        PMOD A
   ┌───────────────────────────────┐
   │  1    2    3    4    5    6    │   1 SCK   2 MOSI  3 MISO  4 CS   5 GND  6 3V3
   │  7    8    9   10   11   12    │   7 RDY   8 SYNC  9 —    10 —   11 GND 12 3V3
   └───────────────────────────────┘
      (1 SCK · 2 MOSI · 3 MISO · 4 CS · 7 MCU_READY · 8 USB_SYNC)
```

## Wiring diagram

```text
  Cynthion  (ECP5, SPI master)                CH32H417QEU6 dev board (SPI1 slave)
  ────────────────────────────                ──────────────────────────────────
  PMOD A                                       GPIO headers: J10 (PA3–PA7), J5 (PC6)

  pin 1  IO0  SCK        ───────────────────►  PA5   SPI1_SCK
  pin 2  IO1  MOSI       ───────────────────►  PA7   SPI1_MOSI
  pin 3  IO2  MISO       ◄───────────────────  PA6   SPI1_MISO
  pin 4  IO3  CS/NSS     ───────────────────►  PA4   SPI1_NSS
  pin 7  IO4  MCU_READY  ◄───────────────────  PA3   GPIO out
  pin 8  IO5  USB_SYNC   ───────────────────►  PC6   TIM8_CH1 (AF3)  [J5]

  pin 5  GND            ─────────────────────  GND   common ground (REQUIRED)
  pin 6  3V3            ──✗ do NOT connect ──  3V3   (each board self-powered)
```

## Signal notes

- **`MCU_READY` (PA3 → FPGA):** held **low until both SPI DMA directions are
  armed**; the FPGA must not begin slot transfers until it reads high. See the
  DMA discipline in design spec §7.3.
- **`USB_SYNC` (FPGA → PC6):** one pulse **per full-speed USB SOF**, captured by
  **TIM8_CH1** input capture (AF3) on header J5. This gives the CH32 scheduler the
  USB-frame time reference; there is no shared free-running MCU clock. PA2/TIM5_CH3
  (the natural 32-bit-timer choice) is not broken out on this board, so an exposed
  TIM8 channel is used; TIM8 also avoids Hurra-v3's prior TIM2 cross-core ownership
  concern. TIM8 is 16-bit, so a software overflow counter widens the timestamp to a
  32-bit USB-time base (design spec §7.2).
- **SPI1 alternate function:** PA4–PA7 use **AF5**; PC6 uses **AF3** (TIM8_CH1);
  PA3 is a plain GPIO output.

## FPGA-side binding (why balls are not listed)

The gateware requests the `user_pmod` resource (indices 0–7); the `cynthion`
platform maps those to physical PMOD A pins `1 2 3 4 7 8 9 10` and to the ECP5
balls for the connected board revision. The ball set differs by revision — for
example PMOD A is `A3 A4 A5 A6 … C6 B6 C7 B7` on r0.3–r0.5/r1.0 but
`C9 B9 D11 C12 … C8 D8 D9 C10` on r1.4 — while the **physical pin numbers are the
same on every revision**. Wire to the physical pins in the table above and the
platform handles the ball mapping; the harness is revision-independent.

## Verify before first transfer

1. Continuity: each of the six signals plus GND, PMOD-pin to PAx.
2. No 3V3-to-3V3 bridge; GND common.
3. On bring-up, sanity-check the RX/TX pairing and that `MCU_READY` reaches the
   FPGA high only after DMA arm (design spec §7.2 lists the logic-analyzer checks:
   mode 0, 15 MHz, 32 bytes, 125 µs cadence, PA3 readiness, PC6 SOF capture).
