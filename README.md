# hurra-cynthion

hurra-cynthion is gateware for a [Cynthion](https://greatscottgadgets.com/cynthion/) r1.4
that sits between a USB HID device and a PC. The Cynthion enumerates the device on its
TARGET-A port and presents a clone of it to the PC on AUX, with the device's own VID/PID and
descriptors, at the same link speed. Reports pass through as they arrive. The PC's HID class
requests and interrupt-OUT reports, such as rumble or keyboard LEDs, reach the real device.
An optional NXP FRDM-MCXN947 on PMOD-A can add motion and clicks to a mouse's reports or set
a gamepad's sticks, driven by the `km.*` and `pad.*` serial commands that KMBox and MAKCU host
tooling already sends.

## Hardware

You need a Cynthion r1.4 and a USB HID device that runs at Full or High Speed (check it
against [Supported devices](#supported-devices)). AUX goes to the PC and CONTROL to a build
machine. The board and the device on TARGET-A both draw power from CONTROL. Injection adds an
NXP FRDM-MCXN947 and some jumper wires.

```
HID device ──> TARGET-A  [ Cynthion r1.4 ]  AUX ──> PC
                            CONTROL ──> build machine (power and apollo)
                            PMOD-A  ──> FRDM-MCXN947 (optional)
                                          J11 ──> host running KMBox/MAKCU tooling
```

### Wiring

The FPGA is the SPI master and runs SPI mode 0 at 15 MHz. It clocks one 32-byte slot every
125 µs. PMOD-A numbers are physical connector pins. J5 and J6 are the two rows of the MCXN947
board's mikroBUS socket.

| Signal | Driven by | Cynthion PMOD-A | FRDM-MCXN947 |
|---|---|---|---|
| `sck` | FPGA | 1 | J6 pin 4 (P3_21) |
| `mosi` | FPGA | 2 | J6 pin 6 (P3_20) |
| `miso` | MCU | 3 | J6 pin 5 (P3_22) |
| `cs_n` | FPGA | 4 | J6 pin 3 (P3_23) |
| `mcu_ready` | MCU | 7 | J5 pin 2 (P5_7) |
| `usb_sync` | FPGA | 8 | J3 pin 3 (P1_22) |
| GND | | 5 or 11 | J6 pin 8 |

Join the grounds and keep the two 3.3 V rails apart. PMOD-A pins 9 and 10 are unused, and the
current firmware does not read `usb_sync`. The FPGA pulls its inputs down, so an unplugged MCU
reads as absent.

## Build

```sh
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -e .
yosys -V && which yosys nextpnr-ecp5 ecppack
LUNA_PLATFORM=cynthion.gateware.platform:CynthionPlatformRev1D4 make build
```

The install needs Python 3.11 or newer and brings the pinned dependencies, including the
`apollo` CLI. Bitstream builds also need `yosys`, `nextpnr-ecp5` and `ecppack` from a single
OSS CAD Suite release, with yosys 0.60 or newer; the build refuses an older yosys, which rarely
closes timing on this design. The tested release is oss-cad-suite 2026-09-01 (yosys 0.68). The
third line shows what the build will find; if `YOSYS` is set, the build uses that binary instead
of the one on `PATH`. The Makefile stops if `LUNA_PLATFORM` is unset. The bitstream lands in
`build/hurra-cynthion.bit`, and it exists only if timing passed.

## Flash

```sh
apollo configure build/hurra-cynthion.bit       # SRAM; lost on reset or power cycle
apollo flash-program build/hurra-cynthion.bit   # configuration flash
apollo reconfigure                              # load from flash now
```

Use `configure` to try a build and the other two to keep one. With the bitstream in flash the
board relays from power-up, and CONTROL can go to any USB supply.

## Supported devices

Any HID device with an interrupt-IN endpoint on a HID interface works, within these limits:

| | Limit |
|---|---|
| Speed | High Speed or Full Speed. Low Speed devices are refused. AUX mirrors TARGET. |
| Topology | One device, plugged straight into TARGET-A. A hub fails enumeration. |
| Interfaces | Up to 4. Alternate settings other than 0 are skipped. |
| Interrupt-IN endpoints | Up to 4 on HID interfaces, max packet 64 bytes. Numbers 1..15, each used once. |
| Descriptors | Configuration up to 1024 bytes. Report descriptor up to 2048 bytes per interface. |
| Audio interfaces | Tolerated, as on a DS4. Their endpoints are not relayed. |

A device outside these limits, or one that fails enumeration six times running, lights LED 4.
On the PC's side:

- HID class requests on EP0 reach the device, SET_IDLE and SET_PROTOCOL included. A response
  over 64 bytes is STALLed.
- The first interrupt-OUT endpoint on a HID interface is relayed. Further ones get no handshake.
- When a BIOS puts an interface in boot protocol, reports on that interface pass through
  unmodified. A PC bus reset or SET_CONFIGURATION returns the device to report protocol and
  discards queued reports.
- Vendor requests are STALLed, which keeps the PC away from the device's firmware-update path.

## Injection

The FRDM-MCXN947 firmware compiles a field map from the device's report descriptors and
uploads it to the Cynthion. Console commands then become injection requests. With no MCU
attached no map is ever active and every report passes through unmodified.

### Firmware

Building needs `arm-none-eabi-gcc` and `python3` on `PATH`. Flashing needs NXP LinkServer and
a cable to J17, the board's MCU-Link debug probe.

```sh
make -C firmware/mcxn947 check    # build both core images, then inspect them
make -C firmware/mcxn947 flash    # program both cores through the MCU-Link
make -C firmware/mcxn947 probes   # list the probes LinkServer can see
```

The Makefile looks for `/Applications/LinkServer_24.12.21/LinkServer`; set `LINKSERVER` to use
another. J17 also
carries a diagnostic UART at 115200 8N1. `firmware/ch32h417/` holds firmware for the earlier
controller, a WCH CH32H417, on the same link contract.

### Console

The console is USB CDC on the J11 Type-C connector (J17 is the probe), and it enumerates as
`hurra-adapter` (VID:PID 1209:0001). It echoes input and prompts with `hurra> `. `help` lists
the built-in commands, and `stats` prints link counters with the `km_*` tallies.

`km.*` and `pad.*` input is off at every boot, so those lines get `unknown command; try help`.
Turn it on with `kmmode makcu` or `kmmode kmbox`:

| Mode | Accepted command | Refused command |
|---|---|---|
| `makcu` | `km.move(10,0)`, then `>>>` on the next line | `km.moveto(!noabsolute)`, then `>>>` |
| `kmbox` | no reply | `km.moveto(!noabsolute)` |

The `hurra> ` prompt follows every line in both modes. Replies use the command's namespace,
`km.` or `pad.`. In `makcu` mode acknowledgements are on at boot; `km.echo(0)` silences them and
`km.echo(1)` restores them. Refusals always print. A reply, or silence in `kmbox` mode, means
the MCU accepted the command. Nothing on the link reports whether the PC saw the result.

The MCU uploads the map after the device's first report, so move the mouse or press something
on the pad once after plugging it in, and again after any link drop. Until then, and with
nothing attached, motion and button commands answer `notready`. Once the descriptors are
compiled, `km.*` on a gamepad answers `nomouse` and `pad.*` on a mouse answers `nopad`, whether
or not the device has reported yet. A keyboard, or any device with neither layout, answers
`nomouse` to `km.*` and `nopad` to `pad.*`. `km.version()`, `km.echo()` and the no-argument getters
answer in any state. `busy` means the one-deep command slot was full, so retry. `badargs`,
`badstate`, `nobutton` and `unknown` point at the command itself.

### Mouse commands

A mouse qualifies when its report descriptor has a Generic Desktop Mouse collection with
relative X and Y in one report, each able to hold ±127. Motion adds to what the mouse reports
and works on a stationary mouse too. A leading `.` stands for `km.`.

| Command | Effect |
|---|---|
| `km.move(dx,dy)`, `km.move(dx,dy,n)` | Adds `dx` and `dy` counts, at most 64 per axis per report, in at least `n` steps (up to 512) if given. Bezier control points after `n` are accepted and ignored. |
| `km.left(s)`, and `right`, `middle`, `side1`, `side2` | `1` presses and `0` releases. `2` also releases, without sending a report of its own. With no argument, answers the injected state in `makcu` mode. |
| `km.click(b)`, `km.click(b,n)` | Clicks button `b` once, or `n` times. Buttons are 1 left, 2 right, 3 middle, 4 side1, 5 side2. |
| `km.click(b,1,ms)` | Holds `b` for `ms` milliseconds, counted at 8 reports per millisecond. The conversion assumes an 8 kHz mouse; on a 1 kHz mouse the hold lasts eight times longer than asked. A count above 1 with a hold answers `noclock`. |
| `km.wheel(n)` | Scrolls `n` notches, one per report. |
| `km.pan(n)`, `km.tilt(n)` | Scrolls horizontally, one notch per report. |
| `km.lock_ml(s)`, and `lock_mr`, `lock_mm`, `lock_ms1`, `lock_ms2` | `1` hides that real button from the PC. `0` passes it again. |
| `km.version()` | Answers `km.version(hurra-mcxn947 kmcmd 1)`. |

Injection adds to the mouse's own motion and has no motion mask, so absolute moves and axis
locks are refused. Button locks work. The Cynthion clones keyboards but cannot type on them.

| Refusal | Commands |
|---|---|
| `noabsolute` | `moveto`, `silent` |
| `nopos` | `getpos`, `screen` |
| `noaxismask` | `lock_mx`, `lock_my`, `lock_mw`, `lock_mx+`, `lock_mx-`, `lock_my+`, `lock_my-`, `lock_mw+`, `lock_mw-` |
| `nocatch` | `catch_ml`, `catch_mr`, `catch_mm`, `catch_ms1`, `catch_ms2` |
| `nokeyboard` | `press`, `down`, `up`, `string`, `isdown`, `disable`, `mask`, `remap`, `keyboard`, `init` |
| `nostream` | `buttons`, `axis`, `mouse`, `mo` |
| `nodevice` | `baud`, `bypass`, `turbo`, `remap_button`, `remap_axis`, `invert_x`, `invert_y`, `swap_xy`, `led`, `serial`, `log`, `hs`, `release`, `reboot`, `fault`, `device`, `info`, `help` |

### Gamepad commands

A pad qualifies when its report descriptor has a Generic Desktop Game Pad or Joystick
collection, or a Multi-axis Controller, with absolute X and Y in one report. Pad commands set
the field to the value given, where mouse commands add to it.

| Command | Effect |
|---|---|
| `pad.lx(v)`, `pad.ly(v)`, `pad.rx(v)`, `pad.ry(v)` | Holds a left or right stick axis at `v`. With no argument, answers the held value in `makcu` mode. |
| `pad.lt(v)`, `pad.rt(v)` | Holds the left or right trigger at `v`. With no argument, answers as above. |
| `pad.hat(d)` | Holds the hat at `d`, `0`..`7` clockwise from up, or `8` for centred. |
| `pad.btn(n,s)` | Presses (`1`) or releases (`0`) button `n`, 1..32. |
| `pad.release()` | Returns every held axis and the hat to the pad's own values. Buttons keep their state. |
| `pad.hold(ms)` | Releases every held axis and the hat after `ms` milliseconds. A later axis or hat command cancels it. |

`v` runs from -32768 to 32767 and is scaled to the field's logical range. -32768 is the field's
minimum and 32767 its maximum, so 0 centres a stick. Channels map to HID usages as on a DS4:
`lx` X, `ly` Y, `rx` Z, `ry` Rz, `lt` Rx, `rt` Ry. On a report without Rx and Ry, Accelerator
becomes `lt` and Brake becomes `rt`. A channel the pad does not have is dropped.

A pad command takes effect on the pad's next report. The MCU acknowledges it at once, and a pad
that is not reporting shows nothing on the PC until it reports again.

## LEDs

| LED | Signal |
|---:|---|
| 0 | TARGET device attached |
| 1 | enumeration in progress |
| 2 | enumerated |
| 3 | report activity (toggles per report) |
| 4 | host error |
| 5 | AUX clone configured by the PC |

## Diagnostics

`regdebug` reads the debug registers over JTAG through CONTROL. From the repository root:

```sh
alias regdebug='PYTHONPATH=src python3 -m hurra_cynthion.regdebug --no-force-offline --map report-injection'
regdebug dump
regdebug read usb_speed
regdebug watch native_reports polls_issued
regdebug write speed_policy 2
```

Keep both flags. Without `--no-force-offline` regdebug forces the FPGA offline before
connecting, which stops the relay you meant to watch. Without `--map` it decodes against the
`capture` map, which does not match this bitstream.

`speed_policy` is the one writable register, 0 after configuration. Bit 0 forces AUX to Full
Speed. Bit 1 skips the TARGET chirp, so the device runs at Full Speed and AUX follows; writing
2 does that from the next replug. `usb_speed.aux_speed` reads 0 at High Speed, 1 at Full.

For a low report rate, `native_reports` alone cannot tell a poll never issued from one the
device NAKed. Read `polls_issued` and `poll_naks` as deltas against `spi_slots`, which ticks
every 125 µs; polls short of the device's interval point at the host, and NAKs filling the gap
point at a device with nothing to send.

`relay_drops` is the only record of a report the relay discarded. Its `unmatched` field counts
reports for an endpoint the clone does not serve, and `congested` counts reports dropped on a
full endpoint queue. Both should read 0.

## Development

```sh
python3 -m pip install -e '.[dev]'
make lint                      # ruff check and ruff format --check over src and tests
make test                      # pytest; Amaranth simulation, no hardware
make rtlil                     # host core to build/host.il; no FPGA toolchain
make verify                    # lint, test, rtlil, CH32H417 firmware; needs riscv-none-elf-gcc
make -C firmware/mcxn947 test  # MCXN947 host tests; a C compiler and python3 only
make firmware-test             # CH32H417 host tests; a C compiler only
```

The MCU link's wire format lives in `protocol/report_injection_wire.json`. Edit the JSON and run
`tools/generate_report_injection_wire.py` (it needs `ruff` on `PATH`), which rewrites
`src/hurra_cynthion/injection_wire.py` and both firmware `injection_wire.h` headers. Never edit
those by hand; `tests/test_injection_wire.py` fails when they drift.

`make -C firmware/mcxn947 DEMO=1 check` builds the bench image instead of the shipping one. It
drifts a mouse cursor up and left at about 80 counts per second, and 20 seconds after boot it
breaks the SPI link on purpose and lets the fault monitor repair it. Rebuild without `DEMO=1`
before flashing a board anyone else will use.

## Layout

| Path | Contents |
|---|---|
| `src/hurra_cynthion/` | Amaranth gateware and the host-side Python tools, `regdebug` among them |
| `tests/` | pytest simulation suite |
| `protocol/` | `report_injection_wire.json`, the wire contract for the MCU link |
| `tools/` | the wire-contract generator and a DS4 descriptor-to-fixture converter |
| `firmware/mcxn947/` | FRDM-MCXN947 controller firmware |
| `firmware/ch32h417/` | firmware for the earlier CH32H417 controller |
| `firmware/mcxn947/include/injection_wire.h` | generated, like its CH32H417 twin and `src/hurra_cynthion/injection_wire.py` |
| `firmware/teensy_hs_mouse/` | a synthetic High Speed mouse used as a bench instrument |
| `docker/` | pinned ECP5 toolchain image for synthesis and seed sweeps; see `docker/README.md` |

## Licence

MIT. See `LICENSE`.
