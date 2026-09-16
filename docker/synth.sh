#!/usr/bin/env bash
# Synthesise once. Emits top.json + top.lpf for the seed fan-out, and the
# pinned-seed bitstream if it closed.
#
# Synthesis is identical across seeds, so it runs exactly once and every seed
# in the sweep places the SAME netlist. Fanning out full builds instead would
# re-run synthesis N times for nothing.
set -euo pipefail

REPO="${REPO:-/work}"
OUT="${OUT:-/out}"
PLATFORM="${LUNA_PLATFORM:-cynthion.gateware.platform:CynthionPlatformRev1D4}"

mkdir -p "$OUT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# --keep-files writes into ./build of the CWD, IGNORING --output
# (luna/__init__.py: build_dir = "build" if args.keep_files else mkdtemp()).
# Run from scratch space so a repo's own build/ is never overwritten.
status=0
LUNA_PLATFORM="$PLATFORM" PYTHONPATH="$REPO/src" \
  python3 -m hurra_cynthion.gateware \
    --output "$WORK/hurra-cynthion.bit" --keep-files \
    >"$OUT/synth.log" 2>&1 || status=$?

for f in top.json top.lpf top.tim top.ys top.rpt; do
  if [ -f "build/$f" ]; then cp "build/$f" "$OUT/"; fi
done
for b in "build/top.bit" "$WORK/hurra-cynthion.bit"; do
  if [ -f "$b" ]; then cp "$b" "$OUT/hurra-cynthion.bit"; break; fi
done

if [ ! -f "$OUT/top.json" ]; then
  echo "SYNTHESIS FAILED: no top.json (exit $status)" >&2
  tail -40 "$OUT/synth.log" >&2
  exit 1
fi

echo "yosys:   $("$YOSYS" -V | head -1)"
echo "nextpnr: $("$NEXTPNR_ECP5" --version 2>&1 | head -1)"
echo "netlist: $OUT/top.json ($(wc -c <"$OUT/top.json") bytes)"
if [ -f "$OUT/hurra-cynthion.bit" ]; then
  echo "pinned-seed bitstream: PRESENT ($(wc -c <"$OUT/hurra-cynthion.bit") bytes)"
else
  echo "pinned-seed bitstream: ABSENT (pinned seed did not close)"
fi
