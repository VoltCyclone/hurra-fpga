#!/usr/bin/env bash
# Fan the seed sweep out in parallel against one netlist.
#
# Safe to parallelise because the image pins OMP/OPENBLAS/MKL/EIGEN threading
# to 1: a 12-seed sweep run 4-at-a-time and the same sweep run 12-at-a-time on
# 12 cores produced bit-identical results on all twelve seeds. Without those
# pinned, concurrency silently changes placement and the sweep is noise.
set -euo pipefail

JSON="${1:?usage: sweep.sh <top.json> <top.lpf> <outdir> [seeds...]}"
LPF="${2:?}"
OUT="${3:?}"
shift 3
SEEDS=("$@")
if [ "${#SEEDS[@]}" -eq 0 ]; then SEEDS=(1 2 3 4 5 6 7 8 9 10 11 12); fi

# Run every seed concurrently by default. Deriving this from nproc was a bug:
# nproc honours cgroup/affinity limits and reports 1 inside a container, which
# silently collapsed the whole fan-out to sequential. Concurrency is safe at any
# level because the image pins the Eigen/OpenMP thread counts to 1, so placement
# does not depend on how many jobs are in flight -- only wall-clock does.
JOBS="${JOBS:-${#SEEDS[@]}}"
if [ "$JOBS" -lt 1 ]; then JOBS=1; fi
echo "sweeping ${#SEEDS[@]} seeds, ${JOBS} at a time (nproc reports $(nproc))" >&2

mkdir -p "$OUT"
printf '%s\n' "${SEEDS[@]}" \
  | xargs -P "$JOBS" -I{} /usr/local/bin/pnr.sh "$JSON" "$LPF" {} "$OUT" \
  | sort -t= -k2 -n | tee "$OUT/results.txt"

PASS="$(grep -c 'verdict=PASS' "$OUT/results.txt" || true)"
TOTAL="${#SEEDS[@]}"
echo "--- ${PASS}/${TOTAL} seeds pass ---"
