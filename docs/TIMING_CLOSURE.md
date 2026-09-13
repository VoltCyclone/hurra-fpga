# Timing closure on the 60 MHz ULPI domain

What binds `$glbnet$aux_phy_0__clk__o` at 60 MHz, how it was measured, and two
results that contradict what a single build would have told you.

Measured 2026-09-12 with yosys 0.53+15 and nextpnr 0.8 (`--12k --package
CABGA256 --speed 8 --placer-heap-timingweight 60`), solver threading pinned.

## Results

Six-seed sweep, parent commit against this one:

> **These numbers do not reproduce and are retained only as a record of what
> was believed. See "The sweep below does not reproduce" at the end of this
> document for the re-measurement that supersedes them.**

| | pass | mean | best | worst |
|---|---|---|---|---|
| before | 5/6 | 60.92 | 62.47 (seed 6) | 59.44 |
| **after** | **6/6** | **66.18** | **68.75** (seed 5) | **63.30** |

Worst seed after the change beats best seed before it. Resources: 19,789 ->
18,927 `LUT4` (81% -> 77%), 7,450 -> 7,435 DFF, EBRs unchanged at 27/56.

## The path that was actually binding

`build_env.py` documented a cone through `sequence_class_allowed` into
`map_store.crc.CE`. That was stale. The measured path was:

```
host.enumerator.fsm_state[1]                       (19,43)  0.40 ns
  ... 6 LUT levels through enumerator / descriptor_store / ep_count ...
device.clear_LUT4_Z                                (21,46)  6.65 ns
  -> Net `clear`                  routing 3.17 ns, (21,46) -> (61,23)
injection_plane.status_pending                     (61,23) 10.00 ns
  ... TX arbitration, weaving injection_plane <-> spi_link <-> monitor ...
spi_link.tx_memory.0.1$DPRAM_COMB3.WRE             (63,18) 16.33 ns
                                    4.49 ns logic, 11.84 ns routing
```

`clear` alone carried **3.17 ns — 19% of the whole period — in one wire**,
because it is a combinational decode of the enumerator FSM (placed at X≈20) and
was consumed by the injection/SPI region (X≈61). 75% of the path was routing,
not logic; the design is routing-limited at 81% LUT occupancy, not depth-limited.

## The two cuts

Both remove the same defect: a far-away signal entering the injection-plane TX
cone combinationally.

1. **`descriptor_export` no longer masks its outputs with `store.clear`.** The
   mask bought exactly one cycle over the registered reset that already clears
   `fragment_pending` and `payload_state` on the next edge. That cycle is not
   load-bearing — `spi_link` abandons an in-progress build whenever `tx_valid`
   drops, queues nothing before byte 31, and the arbiter releases
   `tx_owner_locked` on the same event, so a frame started during a clear is
   unwound and never sent.

2. **`DescriptorStore` publishes `descriptor_generation_changed`.** Consumers
   previously re-derived it from a delayed copy and a 16-bit comparison, putting
   that comparator — fed by a bus crossing most of the die — at the head of
   `invalidate`, `tx_valid` and `tx_abort`. The producer knows when it
   increments. Cycle-identical; pinned by
   `test_generation_changed_strobe_matches_a_delayed_comparison_cycle_for_cycle`.

### Neither works alone

| variant | pass | mean |
|---|---|---|
| baseline | 5/6 | 60.92 |
| cut 1 only | 3/6 | 59.46 |
| cut 2 only | 1/6 | 57.90 |
| **both** | **6/6** | **66.18** |

Each edit measured on its own is **worse than doing nothing**. Removing one long
cone leaves the other binding while still reshuffling placement, so the win only
appears once the whole class is gone. A single build, or a single-change commit,
would have read as a regression and been reverted.

## Builds were not reproducible, and that was not about this change

nextpnr's `--threads` does not reach the HeAP placer's linear solver, which is
Eigen and parallelises through OpenMP. Unpinned, the analytic placer diverges at
its **first iteration** depending on how many cores happen to be free:

| same netlist, same seed | result |
|---|---|
| built alone | 68.75 MHz |
| built alongside six others | 63.59 MHz |
| either, with `OMP_NUM_THREADS=1` | 68.75 MHz |

Five megahertz apart on identical input, either side of the constraint.
`apply_build_environment` now pins `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS` and `EIGEN_DONT_PARALLELIZE`.

**Every seed sweep recorded before this fix was unreproducible**, including the
one this file replaces. That predates the cone work; it is a property of the
toolchain, not of the design.

## A lever measured and deliberately not taken

`injection.engine.stationary_template_cache` is declared 512 bits x 16 — one
64-byte record per layout — but **both ports only ever touch one byte per
cycle**: the fill loop walks `cache_copy_index`, and `STATIONARY_LOAD` walks
`stationary_load_index` and immediately narrows the read with `word_select`.

A 512x16 memory has no efficient block-RAM mapping (15 EBRs, 97% of their depth
wasted against the x36 width limit), so it lands in LUTRAM as 128
`TRELLIS_DPR16X4` — the largest LUTRAM block in the design. Flattened to 8x1024
it is one `DP16KD` in x9 mode; `STATIONARY_PREFETCH` already supplies the bubble
the synchronous read needs, so the load loop keeps its rate and gains no state.

Measured: **-1,071 LUT4 (77% -> 73%) for +1 EBR**, and **-3.55 MHz mean**
(62.63 against 66.18), 6/6 seeds either way.

Not taken, because margin is the binding constraint and LUTs are not. Reach for
it if High Speed work runs out of LUTs rather than out of margin.


## The sweep above does not reproduce

Re-measured 2026-09-12, later the same day, while investigating why the
host-side High Speed work failed to close. Same toolchain, same flags, same
`DETERMINISM_VARS`, seed 5, on three commits:

| commit | `TRELLIS_COMB` | usb clock |
|---|---|---|
| `ac4bfb7` these cone cuts | 20,150 (82%) | **60.03 MHz** PASS |
| `71ce187` High Speed steps 1-4 | 20,088 (82%) | **60.75 MHz** PASS |
| High Speed step 5 (chirp FSM) | 21,016 (86%) | **55.44 MHz** FAIL |

`ac4bfb7` is the commit the table at the top of this document describes. It
measures **60.03 MHz at seed 5, not 68.75**, and its resource count is 20,150
`TRELLIS_COMB` rather than the 18,927 `LUT4` recorded above -- which are not
the same metric, and comparing them is itself one of the ways these numbers
went wrong.

**Consequence: this design has had effectively zero timing margin since the
cone cuts, not the 5.5% recorded above.** The cuts were still worth making --
they removed a real 3.17 ns cross-die route -- but they bought a passing build,
not headroom. Any change that adds logic has to pay for itself.

### How to read `top.tim` without repeating this

`top.tim` contains **four** `Max frequency` lines, not two: two clocks
(`aux_phy_0__clk__o` at 60 MHz and `clocking._clk_120MHz`) times two stages
(post-placement estimate, then post-routing final).

```
297:Info:  ... aux_phy_0__clk__o: 46.19 MHz (FAIL)    <- placement estimate
298:Info:  ... _clk_120MHz:      276.93 MHz (PASS)    <- placement estimate
882:Info:  ... aux_phy_0__clk__o: 60.03 MHz (PASS)    <- THE ANSWER
883:Info:  ... _clk_120MHz:      194.74 MHz (PASS)
```

The usb-domain result is the **third** line. Taking the second yields the
120 MHz clock's *placement estimate*, which passes comfortably and looks like a
healthy result for a build that produced no bitstream. A helper written during
this investigation made exactly that mistake.

Extract it as:

```sh
grep "Max frequency for clock" build/top.tim | sed -n '3p'
```

and confirm the build actually emitted a `.bit` before trusting any number --
nextpnr exits non-zero on a timing failure and writes no bitstream, so the
presence of `top.bit` is the real pass/fail signal.

### Seed sweep on step 5 (chirp FSM), for the record

0/6 pass: seed 1 58.29, seed 2 55.58, seed 3 56.66, seed 4 51.40, seed 5 55.44,
seed 6 55.08. Seed 5 reproduced its standalone value exactly, so the
determinism pinning is working; there is simply no seed that recovers 4.6 MHz.

## Registering the subsystem boundaries does not help (measured, reverted)

The cones cut in `ac4bfb7` were cross-subsystem combinational paths, and a
second one bound immediately afterwards. The obvious generalisation is a
registered boundary discipline: put a flop on every signal crossing between
`host`, `injection_plane`, `device`, `spi_link` and `debug`, so each long route
gets a clock cycle to itself. The latency cost is irrelevant on a bus whose
events are microseconds apart.

Measured on the step-5 netlist. The three status crossings at the head of the
measured critical path were registered -- `injection_plane.session_active` from
`host.enumerated`, `sof_tick` to both `injection_plane` and `spi_link`, and all
six LEDs, whose output pads were dragging a logic net toward the die edge.

| | before | after |
|---|---|---|
| pass | 4/6 | 4/6 |
| mean | 61.28 MHz | 61.20 MHz |
| best | 63.52 | 65.71 |
| worst | 57.12 | 56.32 |
| spread | 6.40 | 9.39 |
| `TRELLIS_COMB` | 19,067 (78%) | 19,779 (81%) |

**No gain.** The mean is flat, the spread widened, the pass count is unchanged,
and it cost 712 LUTs. The best seed improved, but with a flat mean that is seed
luck, and pinning a change on one lucky seed is how the retracted sweep above
came to exist.

The LUT growth explains the result. Only 7 flops were added; the other 705 LUTs
are logic that yosys could previously share and constant-propagate *through*
those boundaries, which a register severs. The change bought shorter routes and
paid for them in duplicated logic.

**Where the depth actually is.** The measured path was

```
host.enumerator.ready -> [LED pad buffer] -> injection_plane.abort
  -> map_store.bank_valid -> sequence_class_stale -> ...
```

and roughly 6.5 ns of it lies *inside* `injection_plane`, crossing no subsystem
edge at all. Boundary work cannot reach that. Anyone attacking timing here
should shorten `injection_plane`'s internal logic depth -- the
`abort`/`bank_valid`/`sequence_class_stale` chain -- rather than repeat this
experiment.
