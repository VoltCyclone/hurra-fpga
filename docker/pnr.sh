#!/usr/bin/env bash
# Place and route ONE seed against an already-synthesised netlist.
# Writes <out>/seed<N>.{tim,config,json} and a one-line result to stdout.
set -euo pipefail

JSON="${1:?usage: pnr.sh <top.json> <top.lpf> <seed> <outdir>}"
LPF="${2:?}"
SEED="${3:?}"
OUT="${4:?}"
CONSTRAINT_MHZ="${CONSTRAINT_MHZ:-60.00}"

mkdir -p "$OUT"
LOG="$OUT/seed${SEED}.tim"

"$NEXTPNR_ECP5" --12k --package CABGA256 --speed 8 \
  --json "$JSON" --lpf "$LPF" \
  --placer-heap-timingweight 60 --seed "$SEED" \
  --textcfg "$OUT/seed${SEED}.config" \
  --log "$LOG" --timing-allow-fail >/dev/null 2>&1 || true

# The log carries FOUR "Max frequency" lines (two clocks x placement-estimate
# and post-routing). The verdict is the LAST one naming the usb clock; the
# earlier one for that clock is a placement estimate reading ~10 MHz low.
# Match prefix-agnostically: it prints Info:/Warning:/ERROR: depending on
# --timing-allow-fail.
LINE="$(grep 'Max frequency for clock' "$LOG" 2>/dev/null | grep 'aux_phy_0__clk__o' | tail -1 || true)"
if [ -z "$LINE" ]; then
  echo "seed=${SEED} fmax=NONE verdict=ERROR bitstream=no"
  exit 1
fi
FMAX="$(printf '%s' "$LINE" | sed -E 's/.*: ([0-9.]+) MHz.*/\1/')"
VERDICT="$(printf '%s' "$LINE" | sed -E 's/.*\((PASS|FAIL) at.*/\1/')"

# ecppack only on a real pass, so "a .bit exists" stays a truthful signal --
# note nextpnr writes its --textcfg even when timing fails, so that file
# existing proves nothing.
BIT=no
if [ "$VERDICT" = "PASS" ]; then
  "$ECPPACK" --compress --freq 38.8 \
    --input "$OUT/seed${SEED}.config" --bit "$OUT/seed${SEED}.bit" >/dev/null 2>&1
  BIT=yes
fi

printf 'seed=%s fmax=%s verdict=%s bitstream=%s\n' "$SEED" "$FMAX" "$VERDICT" "$BIT"
printf '{"seed":%s,"fmax":%s,"verdict":"%s","bitstream":"%s","constraint":%s}\n' \
  "$SEED" "$FMAX" "$VERDICT" "$BIT" "$CONSTRAINT_MHZ" > "$OUT/seed${SEED}.result.json"
