# Block RAM budget

How the 56 EBRs on the LFE5U-12F are spent, why 25 of them were reclaimed on
2026-09-12, and what that did and did not buy.

## The sizing rule

An ECP5 EBR (`DP16KD`/`PDPW16KD`) holds 18,432 bits but is only configurable to
widths of 1/2/4/9/18/36. A memory wider than 36 bits must be **split across
parallel blocks**, and each of those blocks then holds only as many rows as the
memory is deep. A 368-bit-wide, 16-deep memory therefore occupies
`ceil(368/36) = 11` blocks and uses 16 of each block's 512 rows — **3.2%** of
the silicon it reserves.

LUTRAM (`TRELLIS_DPR16X4`) is 4 bits wide and 16 deep, so:

```
DPR16X4 count = ceil(width / 4) x ceil(depth / 16)
```

Cost is linear in *bits*, with no width cliff and no depth rounding beyond 16.
That makes LUTRAM strictly better for anything wide-and-shallow, and block RAM
better for anything deep, regardless of width.

## What was reclaimed

Measured with `yosys 0.53+15` (`synth_ecp5`), attributing every `DP16KD` /
`PDPW16KD` cell in the post-synthesis netlist back to its source memory.

| Memory | shape x depth | bits | EBR before | after | LUTRAM cost |
|---|---|---|---|---|---|
| `injection.layout_state` | 368 x 16 | 5,888 | 11 | 0 | 92 |
| `descriptors.serve_directory` | 64 x 12 | 768 | 2 | 0 | 16 |
| `descriptors.admin_directory` | 64 x 12 | 768 | 2 | 0 | 16 |
| `injection_map.layout_directory` | 31 x 32 | 992 | 2 | 0 | 32 |
| `relay.queue_memory_0..3` | 9 x 128 | 1,152 ea | 4 | 0 | 96 |
| `report_monitor.queue_memory` | 8 x 128 | 1,024 | 1 | 0 | 16 |
| `injection.report_buffer` | 16 x 64 | 1,024 | 1 | 0 | 16 |
| `spi_link.rx_memory` | 8 x 64 | 512 | 1 | 0 | 8 |
| `spi_link.tx_memory` | 8 x 32 | 256 | 1 | 0 | 4 |
| **total** | | | **25** | **0** | **~296** |

Net effect on the whole design, post-place-and-route (`nextpnr 0.8`, seed 12):

| | before | after | delta |
|---|---|---|---|
| `DP16KD` | 52/56 (**93%**) | 27/56 (**48%**) | **-25** |
| `TRELLIS_COMB` | 18,132 (75%) | 19,943 (**82%**) | +1,811 |
| `TRELLIS_FF` | 7,997 (33%) | 7,433 (30%) | -564 |
| `TRELLIS_RAMW` | 232 | 521 | +289 |
| `aux_phy` Fmax | 63.34 MHz | 62.68 MHz | -0.66 |

At the synthesis stage `LUT4` is flat (12,836 -> 12,877) and flip-flops *fall*,
because the wide block-RAM arrays needed address decode and output muxing that
LUTRAM does not. The +1,811 `TRELLIS_COMB` appears at packing: each
`TRELLIS_DPR16X4` occupies a slice that could otherwise hold logic, and yosys'
read-during-write emulation adds some glue.

**The trade is 25 EBRs (-45pp) for 1,811 LUTs (+7pp).** Per-memory it ranges
from 8.4 LUTRAM/EBR (`layout_state`) to 24 (`relay` queues) -- all well inside
the break-even.

Yosys preserves read-during-write semantics across the change by inserting
`emulate_transparency` / `emulate_read_first` logic; that cost is included.

Yosys preserves read-during-write semantics across the change by inserting
`emulate_transparency` / `emulate_read_first` logic; that cost is included in
the numbers above.

## What was deliberately left in block RAM

| Memory | shape x depth | EBR | why |
|---|---|---|---|
| `map_store.entry_bank_0`/`_1` | 208 x 64 | 11 each | LUTRAM would cost ~208 DPR16X4 *per read port*, ~832 total (~3,300 `TRELLIS_COMB`, 14% of the device). Not worth 22 EBRs while 29 are free. |
| `descriptor_store.descriptor_memory` | 8 x 4096 | 4 | Deep and narrow -- 44% block utilisation, exactly what EBRs are for. LUTRAM would cost 512 DPR16X4 per replica. |
| `map_store.validation_occupancy` | 16 x 512 | 1 | 128 DPR16X4 to free one block is a bad trade. |

The break-even is roughly: convert when `ceil(w/4) * ceil(d/16)` is under a few
hundred DPR16X4 per block freed. Everything above was well under; everything
left is well over.

## What this did *not* fix

It was predicted that relieving 92% BRAM occupancy would relieve placement
congestion and improve timing on the 60 MHz `aux_phy` domain. **That prediction
was wrong.** A 12-seed sweep before and after:

| | pre-LUTRAM (5 seeds) | post-LUTRAM (12 seeds) |
|---|---|---|
| best | 63.34 MHz | 62.68 MHz |
| pass rate | 1/5 | 4/12 |
| spread | 54.8 - 63.3 | 54.0 - 62.7 |

The distribution is unchanged. The binding path never touched a block RAM:

```
host.enumerator.descriptor_store.descriptor_generation[15]  (FF)
  -> injection_plane.map_store.bank_valid_0
  -> injection_plane.sequence_class_allowed
  -> injection_plane.map_receiving      (x several)
  -> injection_plane.engine.stationary_decision_last
  -> device.relay.report_valid
  -> injection_plane.engine.transaction_invalidated
  -> injection_plane.map_store.crc_TRELLIS_FF_Q_22.CE   (setup)

22 LUT levels, 16.04 ns: 3.98 ns logic, 12.06 ns routing (75%)
```

This is a cross-module *control* cone -- a chain of validity and admission
predicates ANDed across four modules and terminating on a clock enable. It is
the cone `build_env.py` has named as the durable fix since 2026-07-25, and it
remains the only thing standing between this design and timing margin.

## Two ECP5 levers that do *not* apply here

- **`REGMODE=OUTREG` on block RAMs.** Not a flag: OUTREG *is* an extra pipeline
  register, so enabling it adds a cycle of read latency and requires every
  reading FSM to be adjusted. It would also buy nothing today -- no block RAM
  appears on the critical path.
- **`io.FFBuffer` on the ULPI pins.** Unsafe, not merely unhelpful. LUNA drives
  `ulpi.data.oe` combinationally from `~ulpi.dir.i` (`luna/gateware/interface/
  ulpi.py:873`) because ULPI requires the FPGA to release the data bus in the
  same cycle the PHY asserts `dir`. An IOFF on `dir` delays that release by a
  cycle and puts the PHY and FPGA on the bus simultaneously. The absence of
  IOFFs on this interface is required by the protocol, not an oversight.

## The measured critical path has moved

This file previously carried the falsified timing hypothesis for the EBR
reclaim. The binding path has since been measured properly and cut; see
[TIMING_CLOSURE.md](TIMING_CLOSURE.md). Two findings there bear on memory
choices:

- `stationary_template_cache` is 512x16 but byte-in/byte-out on both ports.
  Flattened to 8x1024 it costs 1 EBR instead of 128 `TRELLIS_DPR16X4`
  (-1,071 LUT4) -- but measured **3.55 MHz slower**, so it was not taken. It is
  the obvious lever if HS work runs out of LUTs rather than margin.
- Builds were not reproducible until solver threading was pinned. Any
  LUTRAM-vs-EBR comparison made by building once is suspect.

## Headroom for High Speed

**29 free EBRs (52%) and 4,345 free `TRELLIS_COMB` (18%).**

HS needs EBRs far more than LUTs: packet buffering scales 8x (64 -> 512 bytes)
while the new *logic* -- chirp negotiation FSM, 125 us microframe cadence -- is
small. So trading LUTs for EBRs is the right direction, and converting
`entry_bank_*` (which would spend ~3,300 more LUTs) is the wrong one.

**Caveat: some of these conversions should be revisited when buffers grow.**
LUTRAM cost is linear in bits, so a memory that was cheap at 128 deep is not at
512. The `relay` queues are the clearest case:

| relay queue geometry | LUTRAM | EBR | better |
|---|---|---|---|
| 9 x 128 (today, FS) | 24 each | 1 each | LUTRAM |
| 9 x 512 (HS, 512-byte packets) | 96 each | 1 each | **EBR** |

At 4 x 96 = 384 LUTRAM (~1,500 COMB) against 4 EBRs, block RAM wins again. The
rule does not change -- wide-and-shallow to LUTRAM, deep to block RAM -- but
which side of it a given memory sits on does, once its depth grows.
