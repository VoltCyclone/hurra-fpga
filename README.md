# hurra-cynthion

USB HID relay gateware for a [Cynthion](https://greatscottgadgets.com/cynthion/)
r1.4 (Lattice ECP5 LFE5U-12F, CABGA256, speed grade 8), written in Amaranth on
top of LUNA. It acts as a USB *host* on the TARGET-A port — powering the port,
resetting, enumerating an attached HID boot mouse and polling its interrupt-IN
endpoints — and presents a clone of that device to a PC on the AUX port,
relaying the reports and optionally mutating them in flight. Both links
negotiate High Speed (480 Mb/s) through a host-side chirp handshake. Debug
registers are read over JTAG with Apollo.

## How it works

```
mouse / receiver ──> TARGET-A  [ Cynthion r1.4 ]  AUX ──> PC
                                  CONTROL ──> build machine (apollo, regdebug)
                                  PMOD-A  ──> CH32H417 (optional, injection control)
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

Captured descriptors land in a `DescriptorStore`, which is copied verbatim into
the AUX clone's private store, so LUNA's `USBDevice` serves the PC the same
VID/PID, configuration, HID and report descriptors the real device gave us.
One `InterruptInPoller` runs per captured endpoint; they share a single
`USBHostTransactionEngine` round-robin through an arbiter, with SOF and
enumeration control transfers holding strict priority. `bInterval` is stored in
the device's own encoding and decoded against the negotiated speed — frames at
Full Speed, `2**(bInterval-1)` microframes at High Speed — because the clone
hands the PC the original byte.

Reports from all pollers merge into one interface- and endpoint-tagged byte
stream, pass through `ReportInjectionEngine`, and are relayed to the matching
AUX endpoint. The injection engine mutates complete reports against a field map
uploaded at runtime by a CH32H417 MCU over a 32-byte fixed-slot SPI link on
PMOD-A, one slot per 125 us. With no MCU attached (`link_ready = 0`) no map is
ever active and every report passes through unmodified.

AUX mirrors whatever TARGET negotiated: `device.full_speed_only` is driven from
`~host.high_speed`, so the clone is transparent in speed as well as in
descriptors.

## Requirements

- Cynthion r1.4, and a USB HID boot mouse or its wireless receiver.
- Python 3.11 or newer. Runtime pins are `amaranth==0.5.7`, `cynthion==0.2.3`,
  `luna-usb==0.2.2`, `usb-protocol==0.9.1`; the `dev` extra pins
  `pytest==9.0.1`, `ruff==0.8.1`, `build==1.2.2.post1`.
- An ECP5 toolchain — `yosys`, `nextpnr-ecp5`, `ecppack` — all from the same
  OSS CAD Suite release. Measured good: yosys 0.53+15 (`690081810`),
  nextpnr-ecp5 0.8-9-g764b5402, Trellis `ecppack` 1.4-72-gf98e72e.

Mixing toolchain releases is not cosmetic here. A Homebrew yosys shadowing the
bundle produced a netlist that failed timing at 51.55 MHz *and still emitted a
bitstream*. Run `which yosys` before trusting any build.

The `apollo` CLI comes from `apollo-fpga`, a dependency of `cynthion`, so it is
installed by the step below.

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
from that variable, and there is no default. The bitstream is written to
`build/hurra-cynthion.bit`.

`make rtlil` writes the platform-independent host core to `build/host.il` and
needs no platform or FPGA toolchain.

`build_env.py` installs the place-and-route environment before LUNA is
imported: it appends `--placer-heap-timingweight 60 --seed 9` to
`AMARANTH_nextpnr_opts` and pins `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS` and `EIGEN_DONT_PARALLELIZE` to 1. The thread pinning is what
makes a build reproducible — nextpnr's `--threads` does not reach the HeAP
placer's Eigen solver, which diverges at its first iteration with core
availability; the same netlist and seed measured 68.75 MHz built alone and
63.59 MHz built alongside six other jobs. A build whose composed options are
missing the timing-weight flag aborts rather than producing a marginal result.

This design has effectively no timing margin on the 60 MHz ULPI domain, and
placement is seed-sensitive, so any RTL change needs a seed sweep rather than a
single build. nextpnr writes no bitstream on a timing failure, which makes the
existence of the output file the pass signal — not a frequency parsed out of
the log. `docs/TIMING_CLOSURE.md` covers what binds the domain, and how to read
the right line out of `build/top.tim`.

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

A flash-resident board comes up and relays with no debug connection attached at
all.

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

Three register maps exist — `capture`, `aux-clone` and `report-injection` —
one per diagnostic top. Only `report-injection` matches the production
bitstream. Addresses do not correspond between maps, and `magic` reads
`0xcafe1234` for all three, so it confirms the readback path works but does
*not* tell you the map is right. Check the map against the loaded top before
trusting a value.

Registers are 32 bits with fields packed LSB-first, allocated in declaration
order, and appended only — inserting one shifts every later address and
silently invalidates readers built against the old map. Counters mostly
saturate; `spi_slots`, `polls_issued` and `poll_naks` wrap mod 2**32 and are
meant to be read as deltas over a window. That trio is the useful one for
diagnosing a low report rate: `native_reports` counts reports *received*, so a
poll that was never issued and a poll the device NAKed produce an identical
number, while `polls_issued` against the free-running `spi_slots` reference
separates a slow host from a quiet device.

`speed_policy` is the one writable register. Bit 0 forces AUX to Full Speed,
bit 1 suppresses the TARGET chirp; AUX follows TARGET, so bit 1 alone returns
the whole relay to Full Speed without building a second bitstream.

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

## Tests and checks

```sh
make lint    # ruff check + ruff format --check over src and tests
make test    # pytest; Amaranth simulation only, no hardware needed
make rtlil
```

`make verify` runs those plus `firmware-test`, `firmware-check` and
`firmware`, which build the CH32H417 image and its host-side unit tests and
need `riscv-none-elf-gcc` on `PATH`.

## Layout

| Path | |
|---|---|
| `src/hurra_cynthion/` | Amaranth gateware and the host-side Python tools |
| `tests/` | pytest simulation suite |
| `protocol/` | `report_injection_wire.json`, the versioned wire contract for the MCU link |
| `firmware/ch32h417/` | MCU firmware driving the injection control link |
| `firmware/teensy_hs_mouse/` | synthetic High Speed HID mouse used as a test instrument |
| `tools/` | `generate_report_injection_wire.py`, which emits the Python and C sides of that contract |
| `docs/` | bring-up log, timing closure, block RAM budget, design records |

## Test instrument

`firmware/teensy_hs_mouse/` is a bare-metal USB device for a SparkFun MicroMod
Teensy (iMXRT1062) that presents a minimal boot mouse at High Speed and emits
reports at a fixed, known rate with a sequence counter in the payload. It
offers `bInterval = 1`, which is 8000 reports/sec at High Speed and 1000/sec at
Full Speed, so the relay's report rate measures the negotiated link speed
instead of inferring it from a status bit, and a shortfall is an unambiguous
drop rather than a hand holding still. See
[`firmware/teensy_hs_mouse/README.md`](firmware/teensy_hs_mouse/README.md).

## Documentation

`docs/` holds the engineering records. None are needed for normal use, but
several are worth reading before specific kinds of change:

| | |
|---|---|
| [`docs/TIMING_CLOSURE.md`](docs/TIMING_CLOSURE.md) | What binds the 60 MHz ULPI domain, and the toolchain threading that made earlier sweeps unreproducible. Read before touching a cross-module control signal. |
| [`docs/BRAM_BUDGET.md`](docs/BRAM_BUDGET.md) | Accounts for all 56 EBRs and the rule deciding LUTRAM vs block RAM. Read before adding or resizing a memory. |
| [`docs/HIGH_SPEED_SCOPE.md`](docs/HIGH_SPEED_SCOPE.md) | What moving from 12 Mb/s to 480 Mb/s touches, and which half LUNA already solves. |
| [`docs/hardware/`](docs/hardware/) | Board-to-board wiring for the MCU control link. |
