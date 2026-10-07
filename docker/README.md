# ECP5 toolchain image

The Dockerfile here builds an image holding the exact toolchain this design is tested on:
oss-cad-suite 2026-09-01 (yosys 0.68+136, nextpnr-ecp5 0.11.1-19, ecppack 1.4-82) and the
Python packages pinned in `pyproject.toml` (amaranth 0.5.7, cynthion 0.2.3, luna-usb 0.2.2,
usb-protocol 0.9.1). Use it to build the bitstream on a machine without the toolchain, or to
sweep placer seeds after an RTL change.

The yosys version matters more than anything else in the flow. From the same source and the
same nextpnr, yosys 0.48+47 closed timing on 3 of 12 placer seeds and yosys 0.68+136 on 12 of
12. The image build fails if its yosys is older than 0.60.

The suite in the image is the linux-x64 build. On an arm64 host Docker runs it under emulation,
which works but is several times slower; an x86-64 machine is the right place for a sweep.

## With make

From the repository root:

```sh
make container-image   # build the image
make container-synth   # build the image, then synthesise once into out/
make container         # both, then place and route seeds 1..12
```

`OUT` sets the output directory (default `out`), `SEEDS` the seeds to sweep, and `IMAGE` the
image tag (default `ecp5-toolchain:2026-09-01`):

```sh
make container SEEDS="2 4 6" OUT=out-check
```

## By hand

```sh
docker build -t ecp5-toolchain:2026-09-01 docker/

# synthesise once
docker run --rm -v "$PWD:/work" -v "$PWD/out:/out" \
  -e REPO=/work -e OUT=/out ecp5-toolchain:2026-09-01 synth.sh

# sweep seeds 1..12 inside one container
docker run --rm -v "$PWD/out:/out" ecp5-toolchain:2026-09-01 \
  sweep.sh /out/top.json /out/top.lpf /out/sweep

# or run one container per seed
for s in $(seq 1 12); do
  docker run -d --rm --cpus=1.5 -v "$PWD/out:/out" ecp5-toolchain:2026-09-01 \
    pnr.sh /out/top.json /out/top.lpf "$s" /out/sweep
done
```

`sweep.sh` takes a seed list after the output directory, as in
`sweep.sh /out/top.json /out/top.lpf /out/sweep 3 7 9`, and runs every seed at once unless
`JOBS` is set (`docker run -e JOBS=4 ...`). `synth.sh` builds for
`cynthion.gateware.platform:CynthionPlatformRev1D4` unless `LUNA_PLATFORM` is set.

## Output

`synth.sh` runs the full LUNA flow once, placing at the pinned seed (`DEFAULT_PLACER_SEED` in
`src/hurra_cynthion/build_env.py`), and writes to `out/`:

| File | Contents |
|---|---|
| `top.json`, `top.lpf` | netlist and constraints; every seed in a sweep places these |
| `top.ys`, `top.rpt`, `top.tim` | yosys script, yosys log, nextpnr log for the pinned seed |
| `top.sha256` | sha256 of `top.json` |
| `synth.log` | everything the flow printed |
| `hurra-cynthion.bit` | the bitstream, present only if the pinned seed closed timing |

It ends by printing the yosys version, the nextpnr version, the netlist sha256 and whether the
pinned-seed bitstream is present. If no netlist appears it prints `SYNTHESIS FAILED` and the
last 40 lines of `synth.log`.

Each seed writes to `out/sweep/`:

| File | Contents |
|---|---|
| `seed<N>.tim` | nextpnr log |
| `seed<N>.config` | nextpnr text config, written even when timing fails |
| `seed<N>.bit` | bitstream, written only when the seed passes |
| `seed<N>.result.json` | seed, fmax, verdict and whether a bitstream was written |

`sweep.sh` also writes `results.txt`, one `seed=N fmax=... verdict=PASS|FAIL bitstream=yes|no`
line per seed, and ends with a `--- P/T seeds pass ---` line.

## Reading a sweep

A `.bit` file is the pass signal. `pnr.sh` runs `ecppack` only on a PASS verdict, while the
`.config` file appears either way. The verdict comes from the last `Max frequency` line that
names `$glbnet$aux_phy_0__clk__o`, the 60 MHz `usb` clock. Each log has four `Max frequency`
lines, two per clock, and the earlier one for that clock is a placement estimate that reads
about 10 MHz low. Read the last one when you check a log by hand.

Synthesis is reproducible, so `top.sha256` names the netlist a sweep measured. yosys embeds
source paths in the netlist, so the same logic built from another directory gets a different
sha. Compare shas only between builds from the same path; the container always builds from
`/work`.

Any RTL change produces a new netlist and needs a new sweep. Pin the seed with the best margin
in `DEFAULT_PLACER_SEED` and record the sweep in the comment above it.

## Settings the scripts rely on

The image sets `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` and
`EIGEN_DONT_PARALLELIZE` to 1. Leave them alone. nextpnr's `--threads` does not reach its
placer's Eigen solver, and unpinned, the same netlist and seed measured 68.75 MHz built alone
and 63.59 MHz built beside six other jobs. Pinned, a 12-seed sweep run 4 at a time and 12 at a
time on 12 cores gave bit-identical results, so seeds can run in parallel at any count.

Synthesis runs once and every seed places that netlist. It does not depend on the seed, so a
full build per seed only repeats yosys.

The suite's `bin/` directory is appended to `PATH`; prepending it would put the suite's own
`python3` ahead of the one with amaranth installed. `YOSYS`, `NEXTPNR_ECP5` and `ECPPACK` hold
absolute paths to the suite's binaries, and LUNA's build script runs those, so the build uses
the pinned tools whatever `PATH` holds.
