# Containerised ECP5 build

Reproducible synthesis and a parallel seed sweep for the Cynthion bitstream.

## Why this exists

The yosys version decides whether this design closes timing. From byte-identical
source, measured full-flow at seed 9:

| yosys | fmax | bitstream |
|---|---|---|
| 0.48+47 (the old CI pin) | 56.57 MHz | none |
| 0.53+15 | 53.82 MHz | none |
| 0.68+136 (pinned here) | 68.71 MHz | yes |

Nothing in the repo pinned the synthesis tool, so one commit produced different
answers on every machine and the CI bitstream job was red from the day it was
written. This image pins the toolchain by tarball date so that cannot recur.

There is **no official YosysHQ container image** — the suite ships as dated
tarballs and YosysHQ uses Docker only to build it. Community images carry
unpinned yosys versions, which is the exact failure this image prevents.

## Layout

- `Dockerfile` — oss-cad-suite 2026-09-01 (yosys 0.68+136, nextpnr-ecp5
  0.11.1-19, ecppack 1.4-82) plus the pinned Python deps. Self-checks at build
  time that yosys clears the 0.60 floor and that amaranth imports.
- `synth.sh` — synthesise **once**, emitting `top.json` + `top.lpf`.
- `pnr.sh` — place and route **one** seed against that netlist.
- `sweep.sh` — fan `pnr.sh` out across seeds in parallel.

## Usage

```sh
docker build -t ecp5-toolchain:2026-09-01 docker/

# synthesise once (synthesis is identical across seeds)
docker run --rm -v "$PWD:/work" -v "$PWD/out:/out" \
  -e REPO=/work -e OUT=/out ecp5-toolchain:2026-09-01 synth.sh

# sweep seeds, parallel, inside one container
docker run --rm -v "$PWD/out:/out" ecp5-toolchain:2026-09-01 \
  sweep.sh /out/top.json /out/top.lpf /out/sweep

# or one container per seed, fanned out horizontally
for s in $(seq 1 12); do
  docker run -d --rm --cpus=1.5 -v "$PWD/out:/out" ecp5-toolchain:2026-09-01 \
    pnr.sh /out/top.json /out/top.lpf "$s" /out/sweep
done
```

## Invariants — do not break these

- **Synthesis runs once.** It is identical across seeds; every seed places the
  same netlist. Fanning out full builds re-runs yosys N times for nothing.
- **The four threading vars stay pinned to 1.** nextpnr's own `--threads` does
  not reach the HeAP placer's Eigen solver, which diverges at its first
  iteration with core availability. Pinned, a 12-seed sweep run 4-at-a-time and
  the same sweep run 12-at-a-time on 12 cores give bit-identical results — that
  is what makes the fan-out meaningful rather than noise. Unpinned, the same
  netlist and seed measured 68.75 and 63.59 MHz.
- **`PATH` is appended, never prepended.** The bundle ships its own `python3`
  beside `yosys`; prepending shadows the interpreter carrying amaranth. The
  binaries are also pinned absolutely via `YOSYS`/`NEXTPNR_ECP5`/`ECPPACK`,
  which is what LUNA's generated `build_top.sh` reads.
- **A `.bit` means it passed.** `ecppack` runs only on a PASS verdict. nextpnr
  writes its `--textcfg` output even when timing fails, so that file existing
  proves nothing.
- **Read the last frequency line.** Each log carries four `Max frequency` lines
  (two clocks × placement-estimate and post-routing). The verdict is the last
  one naming `$glbnet$aux_phy_0__clk__o`; the earlier one for that clock is a
  placement estimate reading ~10 MHz low, and misreading it has already
  produced one retracted sweep in this repo's history.

## Expected results

Seed 9 is pinned (`DEFAULT_PLACER_SEED`). On this toolchain 10 of 12 seeds
pass; seeds 7 and 10 fail at ~58.8 MHz, and the worst passing seed is 4 at
60.98 MHz (+1.63%). The seed pin is load-bearing — any RTL edit invalidates the
sweep and it must be redone.
