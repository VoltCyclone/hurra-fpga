# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Amaranth gateware for a Cynthion r1.4 (Lattice ECP5 LFE5U-12F) that acts as a USB
*host* on TARGET-A, enumerates an attached HID boot mouse, and presents a verbatim
clone of it to a PC on AUX — relaying reports and optionally mutating them. `README.md`
covers what it does and how to operate it; this file covers what a change to it must
respect.

## Commands

```sh
pytest                              # whole suite; Amaranth simulation only, no hardware
pytest tests/test_poller.py         # one file
pytest tests/test_host.py::test_name # one test
pytest -k injection                 # by name
make lint                           # ruff check + ruff format --check over src and tests
make rtlil                          # platform-independent host core -> build/host.il; no FPGA toolchain
LUNA_PLATFORM=cynthion.gateware.platform:CynthionPlatformRev1D4 make build
make verify                         # lint + test + rtlil + the three firmware targets
```

`pyproject.toml` sets `pythonpath = ["src"]` and `testpaths = ["tests"]`, so bare `pytest`
works from the repo root. Invoking a module directly does need it: `PYTHONPATH=src python3
-m hurra_cynthion.regdebug ...`.

`make build` refuses to run without `LUNA_PLATFORM` — LUNA resolves the board from that
variable and there is no default.

`make verify` needs `riscv-none-elf-gcc` on `PATH`, **which is not installed on this
machine**. `make firmware-test` works regardless: the CH32 unit tests compile for the host
via `HOST_CC` (default `cc`).

## Toolchain: the yosys version decides whether this design closes timing

**Use yosys >= 0.60. Nothing else about the toolchain matters nearly as much.**

Measured 2026-09-15 by controlled experiment — identical commit, `src/` byte-identical,
yosys the only variable, full LUNA flow:

| yosys | fmax (seed 9) | bitstream |
|---|---|---|
| 0.48+47 — oss-cad-suite 2025-01-01 | 56.57 MHz | none |
| 0.53+15 — `/usr/local/oss-cad-suite/bin` | 53.82 MHz | none |
| 0.60 — `/opt/homebrew/bin` | 65.45 MHz | yes |
| **0.68+136 — oss-cad-suite 2026-09-01** | **68.71 MHz** | **yes** |

A 2x2 cross separating the two tools shows **synthesis is the load-bearing half**: newer
yosys is worth ~+9-10 MHz and closes timing even on the *old* pinned nextpnr 0.7; newer
nextpnr alone is worth only ~+2-3 MHz and still misses.

`build_env.py` now refuses to build with yosys older than 0.60 rather than producing a
netlist that cannot close. Check what you have before trusting any build:

```sh
yosys -V        # must be >= 0.60
which yosys nextpnr-ecp5 ecppack
```

**An earlier version of this section had this exactly backwards** — it called Homebrew 0.60
"wrong, shadows the bundle", called the bundled 0.53+15 "the measured-good version", and
told you to `export PATH=/usr/local/oss-cad-suite/bin:$PATH` before every bitstream build,
which guarantees a failing build on this machine. It also claimed a Homebrew yosys "failed
timing at 51.55 MHz and still emitted a bitstream"; that cannot happen in the production
flow, where `build_top.sh` runs under `set -e` with `ecppack` after `nextpnr`.

Do **not** reach for `synth_ecp5 -nowidelut`. It helps old yosys marginally (1-2 of 12 seeds)
and actively *hurts* new yosys: on 0.60 it drops 8/12 seeds to 4/12 and 65.45 MHz to 57.59.

## Timing closure: HEAD closes timing on yosys >= 0.60, and fails on older

The 60 MHz `usb` (ULPI) domain closes at HEAD **provided the toolchain is right**. On
oss-cad-suite 2026-09-01 the tree produces a real bitstream at 69.05 MHz (seed 7), **12 of
12 seeds passing**. On the old CI pin it passes **3 of 12** — so an older toolchain is not
"produces no bitstream", it is "produces one on a quarter of seeds", which is worse: a
single green build there proves nothing. See "The yosys floor, measured properly" below,
which supersedes the single-seed table above.

**Every CI run in this repository's history is red on the `bitstream (ECP5, pinned seed)`
job, and none of them were an RTL problem** — CI pinned oss-cad-suite 2025-01-01 (yosys
0.48+47). The pin is now 2026-09-01.

### The yosys floor, measured properly

Re-measured 2026-09-16 on deterministic netlists, same source, same nextpnr
(0.11.1-19), same 12 seeds, only yosys differing — each arm synthesised twice to
confirm its netlist was stable before any timing was believed:

| yosys | netlist | 12-seed sweep |
|---|---|---|
| 0.48+47 (old CI pin) | `453a543565f19387` | **3 of 12 pass** |
| 0.68+136 (current pin) | `508f4f8854cda8e4` | **12 of 12 pass** |

This supersedes the four-row single-seed table above as *evidence*, though the table's
direction was right. That table was confounded: every row was one seed-9 measurement on a
different random netlist, and seed 9 alone spans 59.69–68.71 MHz on one unchanged yosys —
a 9.0 MHz spread against the 14.9 MHz the table attributed to version differences. The
0.60-vs-0.68 gap of 3.3 MHz sits entirely inside that noise and supports nothing.

Also note old yosys is not "never closes": it closes on 3 of 12 seeds. The earlier claim
that it produces no bitstream was true of seed 9 specifically and overstated as general.

### Synthesis was not reproducible until 2026-09-15

LUNA names endpoint submodules after their class and falls back to
`f"{name}_{id(endpoint)}"` for the second and later instance of one
(`luna/gateware/usb/usb2/device.py:339`). All four relay endpoints are
`USBStreamInEndpoint`, so three of the four were named after an object address —
**every build synthesised a different netlist.** Two builds from byte-identical source
on an identical pinned toolchain gave sha `f896e7d3` and `f2cf4f37`, and seed 9 measured
59.69 MHz FAIL on one and 62.21 MHz PASS on the other. Naming churn alone moved the
pinned seed across the constraint.

`device.py`'s `_RELAY_ENDPOINT_CLASSES` gives each endpoint its own subclass, which keeps
LUNA on its stable-name path. Three concurrent builds now produce sha `3c1c3404c085e7c2`
byte for byte. `tests/test_device.py` guards it, and `synth.sh` writes `top.sha256`.

**That sha is path-sensitive.** yosys embeds source paths in `src` attributes, so the same
logic built from a different directory hashes differently — a container build from `/work`
and a native build from `~/ctr/exp` gave different shas with identical logic (98 modules,
24249 cells, same fmax on all twelve seeds). Compare shas only across builds from the same
path. CI always builds from `$GITHUB_WORKSPACE`, so within CI it is stable.

Consequences worth internalising:

- **Every sweep recorded before 2026-09-15 measured a netlist that no longer exists**, and
  could not have been reproduced even at the time. That includes the 8-of-12 sweep this
  file used to tell you to trust.
- **12/12 is not an improvement in timing.** The fix froze a netlist that was previously
  redrawn every build, and this draw is a good one. Random draws swept 8, 10 and 11 of 12,
  so 12/12 is the top of the observed range, not outside it. What changed is that the
  number is now a property of the design rather than a coin toss.
- **A red gate used to mean nothing, and a green one meant less.** Any CI history from
  before this date carries no signal about timing.

**A full self-contained handoff lives in `docs/handoffs/TIMING_CLOSURE_HANDOFF.md`** — the
controlled experiment, the four-way version table, the yosys-vs-nextpnr cross, the real
critical path, and the measurement traps. That directory is gitignored, so it is local only.

**The mechanism is unexplained.** A "7.6x LUT7 macro explosion" (197 vs 26) looks causal and
is a real measured correlate, but it is refuted as an explanation: a netlist with *zero* wide
muxes still fails, and the *passing* 0.68 netlist retains 291 L6MUX21 / 1836 PFUMX. Do not
build reasoning on it.

Margins are uneven — the pinned seed 7 has +15.1% but the worst seed, 9, has +1.58% — so
the design has real headroom at the pin and almost none at the bottom of the distribution.
Consequences for any RTL change:

- **A seed sweep is required, not a single build.** `build_env.py` documents measured
  sweeps and pins `DEFAULT_PLACER_SEED = 7`. Any netlist edit invalidates that sweep.
  The pin moved 9 → 7 on 2026-09-15: seed 9 was inherited from a sweep of a netlist that
  no longer exists, and on the current one it is the *worst* of the twelve.
- **`build/top.tim` never exists after a plain `make build`.** LUNA builds in
  `tempfile.mkdtemp()` unless `--keep-files` is passed (`luna/__init__.py:91`), so the CI
  "report timing" step — guarded by `if [ -f build/top.tim ]` — is a silent no-op and has
  never printed a frequency. To get the report, invoke the module directly and add the
  flag: `PYTHONPATH=src LUNA_PLATFORM=... python3 -m hurra_cynthion.gateware --output
  build/hurra-cynthion.bit --keep-files`. Otherwise the only visible number is the fmax in
  nextpnr's ERROR line. **`--keep-files` writes into the literal `build/` directory of the
  CWD, ignoring `--output`** (`luna/__init__.py`: `build_dir = "build" if args.keep_files
  else tempfile.mkdtemp()`), so running it from the repo root overwrites `build/top.json`,
  `build/top.tim` and `build/top.lpf`. Run it from a scratch directory if those are
  reference artefacts you care about.
- **The existence of `build/hurra-cynthion.bit` is the pass signal.** nextpnr writes no
  bitstream on a timing failure. Do not parse a frequency out of the log to decide — and
  note `top.tim` carries four `Max frequency` lines (two clocks × placement/routing); the
  **last** line naming `$glbnet$aux_phy_0__clk__o` is the post-routing result, and the
  earlier line for that same clock is a placement estimate reading ~10 MHz low. A previously
  recorded "6/6 at 68.75 MHz" sweep turned out to be a misread of that file.
- **`build_env.py` runs before the LUNA import** and is the single choke point every
  bitstream build passes through. It appends `--placer-heap-timingweight 60 --seed 7` to
  `AMARANTH_nextpnr_opts` and *aborts* if the composed options lose the timing-weight flag,
  rather than producing a marginal result. It also enforces `MINIMUM_YOSYS_VERSION`: a
  bitstream build on yosys < 0.60 aborts immediately rather than spending ten minutes
  producing no bitstream.
- **`DETERMINISM_VARS` pins `OMP_NUM_THREADS` and friends to 1.** nextpnr's `--threads`
  does not reach the HeAP placer's Eigen solver, which diverges at its first iteration with
  core availability — the same netlist and seed measured 68.75 MHz built alone and
  63.59 MHz built alongside six other jobs. A pinned seed is meaningless without this, and
  so is any sweep. **With them pinned, placement is reproducible even under
  oversubscription**: a 12-seed sweep run 4-at-a-time on 12 cores and the same sweep run
  12-at-a-time on 12 cores produced bit-identical results on all twelve seeds, so a sweep
  may be parallelised freely.
- Cross-die control signals are what bind the domain. One wire (`clear`, enumerator →
  SPI link) once carried 3.17 ns of pure routing, 19% of the period. Prefer publishing a
  precomputed signal from its owner over re-deriving it at the consumer with a wide
  comparator fed from across the die — `DescriptorStore.descriptor_generation_changed`
  exists for exactly this reason.

## Architecture

The production top is `CynthionMouseHostTop` in `gateware.py`. Understanding the dataflow
means reading `host.py`, `gateware.py` and `device.py` together:

```
TARGET-A ─> BoundedMouseHost ─> ReportInjectionDataPlane ─> MouseCloneDevice ─> AUX ─> PC
            (host.py)           (gateware.py)               (device.py)
                                      ^
                                 SPISlotMaster <─ PMOD-A <─ CH32H417 MCU
                                 (spi_link.py)
```

**Host side (`host.py`).** `BoundedMouseEnumerator` drives TARGET-A power itself
(`aux_vbus_en`) rather than sensing VBUS, runs the host half of the USB 2.0 §7.1.7.5 chirp
handshake, and walks the descriptor sequence. One `InterruptInPoller` runs per captured
endpoint, but they all share a **single** `USBHostTransactionEngine` through
`USBHostTransactionArbiter`: enumeration control transfers and SOF hold strict priority,
pollers rotate round-robin, and only the poller that started the in-flight transaction is
routed its result. `ReportMergeMux` then merges the poller streams into one
interface/endpoint-tagged byte stream.

**Descriptor cloning.** Captured descriptors land in a `DescriptorStore` that is copied
verbatim into the AUX clone's private store, so LUNA's `USBDevice` serves the PC the same
VID/PID, configuration and HID/report descriptors the real device gave us. Because the
clone hands over the device's original `bInterval` byte, **AUX must mirror TARGET's
speed** (`device.full_speed_only` is driven from `~host.high_speed`) — otherwise a Full
Speed `bInterval=1` would be advertised to the PC as 1 microframe (8 kHz) against the
~1 kHz actually delivered.

**Injection plane (`gateware.py`, `injection.py`, `injection_map.py`).** Reports pass
through `ReportInjectionEngine`, which mutates complete reports against a field map
uploaded at runtime by the MCU over a 32-byte fixed-slot SPI link, one slot per 125 µs.
With no MCU attached (`link_ready = 0`) no map is ever active and every report passes
through unmodified. The authoritative path is `host -> engine -> monitor -> relay`; **no
telemetry signal may enter that ready chain.**

**The relay never backpressures** (`relay.py`). It is the sink in front of the single
shared injection engine, so any stall there would halt every endpoint at `injection.py`'s
`OUTPUT` state. Instead it admits or drops *whole reports* and records the drops in the
`relay_drops` register — that counter is the only evidence a report was discarded.

### Clock domain

Nearly everything is in the **`usb`** domain (60 MHz, sourced from the ULPI clock):
`m.d.usb`, not `m.d.sync`. The ratio in `src/` is ~535 `m.d.usb` to 9 `m.d.sync`.
`FrameScheduler` is written in `sync` and wrapped at instantiation with
`DomainRenamer({"sync": "usb"})`. New sequential logic should use `m.d.usb` directly.

### Bounds are deliberate, not incidental

`descriptors.py` fixes the envelope: at most 4 interfaces and 4 interrupt-IN endpoints,
endpoint numbers 1..4, max packet size 64, alternate setting 0 only. Enumeration refuses to
commit a capture unless it saw an interface with `bInterfaceClass == 3` and
`bInterfaceProtocol == 2`, failing with `UNSUPPORTED_TOPOLOGY`. `MAX_ENDPOINTS` bounds the
*count*; `RELAY_ENDPOINT_NUMBERS` bounds the *numbers* — a device may legally declare
endpoint 5 while declaring only one endpoint, so the two are not interchangeable.

## Debug registers are append-only

`debug_regs.py` holds three maps (`capture`, `aux-clone`, `report-injection`); only
`report-injection` matches the production bitstream. `aux_clone_diag.py` is the one
diagnostic top still in the tree — the `capture` map has no top and survives only for its
schema.

`RegisterMap._alloc` is declaration-ordered. **Appending is safe; inserting a register
shifts every later address and silently invalidates every reader built against the old
map.** New registers go at the end of the factory function. Fields pack LSB-first in
declaration order, 32 bits per register, and the same append-only rule applies within a
register.

`magic` reads `0xcafe1234` for all three maps, so it confirms the readback path works but
does *not* confirm the map matches the loaded top.

One-cycle pulses must be latched to survive an asynchronous JTAG read — see
`debug.latch_on(...)`, `debug.sticky(...)` and `debug.counter(...)` in the top.

## The wire contract is generated

`protocol/report_injection_wire.json` is the single source of truth. Both of these are
generated from it and carry a "do not edit by hand" header:

- `src/hurra_cynthion/injection_wire.py`
- `firmware/ch32h417/include/injection_wire.h`

Change the JSON, then regenerate:

```sh
python3 tools/generate_report_injection_wire.py    # needs ruff on PATH; it formats the output
```

`tests/test_injection_wire.py` gates this (`header == render_c(schema)`, plus regeneration
idempotency), so a hand-edit or a stale regeneration fails the suite.

CRC-16, slot pack/unpack and sequence classification are **hand-written per language** (C
by hand against generated Python), so structural tests cannot catch the two ends drifting.
`tests/test_wire_cross_language.py` compiles the real firmware source and diffs its
behaviour against the Python reference. The three implementations deliberately disagree
about rejection *reasons* — reasons are diagnostics. The one predicate all three must share
is the FPGA's `valid_return`:

```
deliverable <=> SOF == 0x68 and known_type and length == expected(type)
                and CRC ok and type != IDLE
```

Length is per-type **exact**, not a maximum, and `IDLE` ships length 0. New admission rules
append their cases to `_VECTORS` in that file rather than adding a parallel mechanism.

## Testing conventions

The suite is Amaranth simulation end to end — 29 files, 529 collected tests, no hardware.

- Use `HostTiming.simulation()` in tests, never `HostTiming.hardware()`. The hardware
  profile derives 50 ms resets and 100 ms attach debounces from `clock_hz`, which no
  bounded simulation can run through. Each test file typically has a local `simulate()`
  helper that builds the DUT, adds a clock on the **`usb`** domain
  (`add_clock(1e-6, domain="usb")`) and drives it.
- Substitute a scripted seam for the shared engine rather than the real one — see
  `ScriptedTransaction` in `tests/test_poller.py`, which the testbench drives directly.
- Cross-language tests `skipif` on a missing host `cc`, and the target-codegen test on a
  missing RISC-V toolchain. On this machine the C tests run and the RISC-V one skips.

`ruff` is configured with `line-length = 100` and `select = ["B", "E", "F", "I", "SIM",
"UP"]`. Several gateware modules carry a file-level `# ruff: noqa: SIM117` because SIM117's
`with` collapsing obscures nested Amaranth control-flow DSL structure — keep that pragma
when adding nested `m.If`/`m.Switch` blocks.

## Firmware

`firmware/ch32h417/` builds two images for the dual-core CH32H417 (V3F and V5F) and merges
them at a fixed flash offset.

**This MCU is slated for replacement by an FRDM-MCXN947 — see
`docs/MCXN947_CONTROLLER.md`**, which records the link contract (SPI mode 0, MSB-first,
15 MHz, CS-framed 32-byte slots every 125 µs), the dual-core split, and a ten-step migration
order. An RP2350 Feather was designed in parallel and **rejected** — `docs/RP2350_CONTROLLER.md`
is kept as the record of that comparison, not as a plan. The deciding difference is that the
MCXN947 has a real SPI *slave* peripheral where the RP2350 would need a bespoke PIO program
for the same job. Note
the dual-core split is vestigial: `main_v3f.c` is 46 lines that call `SystemInit()` (which
programs both cores' clocks), poke `WAKEIP` for boot ordering, then spin forever, and
`shared_window.h` is used only by `trap_witness.c`. All real work is on V5F.

Things that are easy to get wrong in the current firmware:

- **`--gc-sections` silently discards anything with no path from an entry point.** A
  translation unit that is compiled and linked but not yet called is thrown away. The
  `V5F_LDFLAGS` `--undefined=` list is the retention root set — ISR handlers especially,
  since the vector table references them `.weak`, so without a strong-symbol root the real
  handler is dropped and the weak trap stub is linked instead.
- **`make all` exits 0 even when the application was discarded.** `make check` is the real
  gate: it asserts on the built artifacts (flash/RAM geometry, per-object survival, an
  expected MMIO store) via `tools/check_images.py`. Flash and RAM geometry live in the
  Makefile once and are passed to both the merge tool and the checker so they cannot drift.
- Host-testable logic is kept MMIO-free behind `#if defined(CH32H417)`, which is why the
  same sources compile for `make test` on the host.

`firmware/teensy_hs_mouse/` is a test *instrument*, not part of the product: a synthetic
High Speed boot mouse emitting reports at a known fixed rate with a sequence counter, so
the relay's report rate measures the negotiated link speed instead of inferring it from a
status bit.

### Vendor code is byte-exact

`firmware/*/vendor/` plus the files listed in `.gitattributes` are imported unmodified and
exempted from the whitespace gate. Do not reformat them. `PROVENANCE.md` in each firmware
directory records the upstream commit, what was deliberately excluded, and every local
deviation with its rationale (e.g. one changed immediate in `startup_v5f.S` because WCH's
own 400 MHz profile violates its FPU divider rule). Any new deviation belongs there too.

## Stale references

`docs/` holds `hardware/ch32-cynthion-wiring.md` and the two MCU controller designs.
Commit `3ba7d8b` ("clean old docs") deleted `TIMING_CLOSURE.md`, `BRAM_BUDGET.md` and `HIGH_SPEED_SCOPE.md`, but
`README.md` still tables all three and `build_env.py`, `relay.py` and `tests/test_relay.py`
still cite them in comments. Don't go looking for those files — the timing-closure
reasoning that mattered is inlined in `build_env.py`'s module docstring and the
`DEFAULT_PLACER_SEED` comment. `docs/BRINGUP_LOG.md` and `docs/handoffs/` are gitignored by
design, so they may exist locally and never in a clone.
