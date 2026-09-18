# hurra-cynthion

USB HID relay gateware for a [Cynthion](https://greatscottgadgets.com/cynthion/)
r1.4 (Lattice ECP5 LFE5U-12F, CABGA256), written in Amaranth on top of LUNA.

It acts as a USB *host* on the TARGET-A port — powering the port, resetting,
enumerating an attached HID boot mouse and polling its interrupt-IN endpoints —
and presents a clone of that device to a PC on the AUX port, relaying its reports
and optionally mutating them in flight. Both links negotiate High Speed
(480 Mb/s) through a host-side chirp handshake. Debug registers are read over
JTAG with Apollo.

## How it works

```
mouse / receiver ──> TARGET-A  [ Cynthion r1.4 ]  AUX ──> PC
                                  CONTROL ──> build machine (apollo, regdebug)
                                  PMOD-A  ──> injection-control MCU (optional)
```

`enumerator.py` owns the TARGET side: it drives TARGET-A power itself
(`aux_vbus_en`) rather than sensing VBUS, resets the bus, runs the host half of
the USB 2.0 §7.1.7.5 chirp handshake, and walks the standard descriptor
sequence. Enumeration is deliberately bounded — at most 4 interfaces and 4
interrupt-IN endpoints, endpoint numbers 1..4, max packet size 64, alternate
setting 0 only — and it refuses to commit a capture unless it saw an interface
with `bInterfaceClass == 3` and `bInterfaceProtocol == 2`, failing with
`UNSUPPORTED_TOPOLOGY` otherwise. A failed attempt re-resets and retries, up to
six times.

Captured descriptors land in a `DescriptorStore`, copied verbatim into the AUX
clone's private store, so LUNA's `USBDevice` serves the PC the same VID/PID,
configuration, HID and report descriptors the real device gave us. One
`InterruptInPoller` runs per captured endpoint; they share a single
`USBHostTransactionEngine` round-robin through an arbiter, with SOF and
enumeration control transfers holding strict priority. `bInterval` is stored in
the device's own encoding and decoded against the negotiated speed — frames at
Full Speed, `2**(bInterval-1)` microframes at High Speed — because the clone
hands the PC the original byte. AUX mirrors whatever TARGET negotiated
(`device.full_speed_only` is driven from `~host.high_speed`), so the clone is
transparent in speed as well as in descriptors.

Reports from all pollers merge into one interface- and endpoint-tagged byte
stream, pass through `ReportInjectionEngine`, and are relayed to the matching AUX
endpoint. The injection engine mutates complete reports against a field map
uploaded at runtime by an external MCU over the SPI control link (see
[Report injection](#report-injection)). With no MCU attached (`link_ready = 0`)
no map is ever active and every report passes through unmodified.

## Requirements

- A Cynthion r1.4 and a USB HID boot mouse (or its wireless receiver).
- Python 3.11 or newer. Runtime and dev dependencies are pinned in
  `pyproject.toml` and installed by the command below.
- An ECP5 toolchain — `yosys`, `nextpnr-ecp5`, `ecppack` — from a single, recent
  OSS CAD Suite release. **Use yosys 0.60 or newer**; older releases do not close
  timing on this design reliably. The tested release is oss-cad-suite 2026-09-01
  (yosys 0.68). Check what is on your `PATH` before trusting a build:

  ```sh
  yosys -V && which yosys nextpnr-ecp5 ecppack
  ```

The `apollo` CLI is installed as a dependency of `cynthion` by the step below.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

## Build

```sh
LUNA_PLATFORM=cynthion.gateware.platform:CynthionPlatformRev1D4 make build
```

The Makefile refuses to run without `LUNA_PLATFORM` — LUNA resolves the board
from that variable and there is no default. The bitstream is written to
`build/hurra-cynthion.bit`. `make rtlil` writes the platform-independent host
core to `build/host.il` and needs no platform or FPGA toolchain.

nextpnr writes no bitstream when timing fails, so the existence of
`build/hurra-cynthion.bit` is the pass signal — not a frequency parsed from the
log. The 60 MHz ULPI domain closes with little margin and placement is
seed-sensitive, so any RTL change should be validated with a seed sweep rather
than a single build. `build_env.py` pins the placer seed and the solver thread
counts so that builds are reproducible; a build whose composed nextpnr options
lose the timing-weight flag aborts rather than emitting a marginal result.

## Load

```sh
apollo configure build/hurra-cynthion.bit
```

That is a volatile SRAM load: a reset or power cycle reverts the FPGA to
whatever is in configuration flash. To make it persistent:

```sh
apollo flash-program build/hurra-cynthion.bit
apollo reconfigure
```

A flash-resident board comes up and relays with no debug connection attached.

## Report injection

Report mutation is driven by an optional external MCU over a 32-byte fixed-slot
SPI link on PMOD-A, one slot every 125 µs. The wire format is versioned in
`protocol/report_injection_wire.json`, the single source of truth from which the
Python and C bindings are generated. The MCU uploads a descriptor-derived field
map and then issues motion, button and mask commands; the FPGA applies them to
live reports transactionally.

Firmware is provided for two controllers, both speaking the same contract:

- `firmware/mcxn947/` — NXP FRDM-MCXN947, the current controller.
- `firmware/ch32h417/` — WCH CH32H417, the earlier controller.

With no MCU attached the relay is fully transparent.

## Reading debug registers

The production top exposes `report_injection_register_map()` over LUNA's
JTAG-tunnelled debug SPI. `regdebug` is the schema-aware reader:

```sh
PYTHONPATH=src python3 -m hurra_cynthion.regdebug --no-force-offline \
    --map report-injection dump
PYTHONPATH=src python3 -m hurra_cynthion.regdebug --no-force-offline \
    --map report-injection read usb_speed
PYTHONPATH=src python3 -m hurra_cynthion.regdebug --no-force-offline \
    --map report-injection watch native_reports polls_issued
```

Subcommands are `magic`, `read`, `write`, `dump`, `mem` and `watch`.
`--no-force-offline` matters: the default forces the FPGA offline before
connecting, which stops the relay you are trying to observe.

Registers are 32 bits with fields packed LSB-first, allocated in declaration
order and appended only — inserting one shifts every later address and silently
invalidates readers built against the old map. Counters mostly saturate;
`spi_slots`, `polls_issued` and `poll_naks` wrap mod 2**32 and are meant to be
read as deltas over a window. That trio diagnoses a low report rate:
`native_reports` counts reports *received*, so a poll that was never issued and a
poll the device NAKed produce an identical number, while `polls_issued` against
the free-running `spi_slots` reference separates a slow host from a quiet device.

`speed_policy` is the one writable register. Bit 0 forces AUX to Full Speed,
bit 1 suppresses the TARGET chirp; AUX follows TARGET, so bit 1 alone returns the
whole relay to Full Speed without building a second bitstream.

```sh
PYTHONPATH=src python3 -m hurra_cynthion.regdebug --no-force-offline \
    --map report-injection write speed_policy 2
```

## LEDs

| LED | Signal |
|---:|---|
| 0 | TARGET device attached |
| 1 | enumeration in progress |
| 2 | enumerated |
| 3 | report activity (toggles per report) |
| 4 | host error |
| 5 | AUX clone configured by the PC |

## Tests

```sh
make lint    # ruff check + ruff format --check over src and tests
make test    # pytest; Amaranth simulation only, no hardware needed
make rtlil   # platform-independent host core; no FPGA toolchain
```

`make verify` runs those plus the CH32H417 firmware build and its host-side unit
tests, which need `riscv-none-elf-gcc` on `PATH`. The MCXN947 firmware has its
own `make test` and `make check` under `firmware/mcxn947/`.

## Layout

| Path | |
|---|---|
| `src/hurra_cynthion/` | Amaranth gateware and the host-side Python tools |
| `tests/` | pytest simulation suite |
| `protocol/` | `report_injection_wire.json`, the versioned wire contract for the MCU link |
| `tools/` | `generate_report_injection_wire.py`, which emits the Python and C sides of that contract |
| `firmware/mcxn947/` | MCXN947 controller firmware for the injection link |
| `firmware/ch32h417/` | CH32H417 controller firmware for the injection link |
| `firmware/teensy_hs_mouse/` | synthetic High Speed HID mouse used as a bench test instrument |
