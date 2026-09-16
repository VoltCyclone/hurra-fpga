"""Build-environment composition for the Cynthion bitstream.

The ECP5 place-and-route step needs ``--placer-heap-timingweight 60`` to close
timing on ``$glbnet$aux_phy_0__clk__o`` (the 60 MHz ``usb`` domain, which is
also the ULPI clock the FPGA sources).

Why this option, and why it is mandatory rather than advisory:

* Without it the design places badly and fails. Measured on the delivery path:
  55.65 MHz against a 60.00 MHz constraint, so no bitstream is produced at all.
* With it, six clean ``rm -rf build && make build`` runs produced a bitstream
  6/6, at 61.75 MHz.
* It is a *placement quality* lever, not margin.

**Both numbers above predate the 2026-09-12 cone work and the determinism fix
below; treat them as historical.** They were measured without pinned solver
threading, so they are not reproducible. See ``DETERMINISM_VARS``.

See ``docs/TIMING_CLOSURE.md``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import MutableMapping

NEXTPNR_OPTS_VAR = "AMARANTH_nextpnr_opts"

#: The flag name alone, used for presence checks independent of its value.
REQUIRED_NEXTPNR_FLAG = "--placer-heap-timingweight"

#: Placement seed pinned to the best of a measured sweep.
#:
#: This is now an *optimisation*, not a requirement. Every seed 1-6 closes
#: timing since the cross-region cones were cut on 2026-09-12; before that,
#: seed choice decided whether a bitstream existed at all.
#:
#: Sweep on the current netlist -- host-side High Speed plus the byte-wide
#: template cache -- measured with the four-line caveat below understood:
#:
#:     seed 1 -> 57.12 FAIL    seed 3 -> 62.40 PASS    seed 5 -> 59.80 FAIL
#:     seed 2 -> 62.87 PASS    seed 4 -> 61.96 PASS    seed 6 -> 63.52 PASS
#:
#: 4/6 pass. Seed 6 is pinned as the best of them. Seed 5, the previous pin,
#: now misses by 0.2 MHz -- so the pin is load-bearing again, as it was before
#: the cone cuts, and any RTL edit needs the sweep redone.
#:
#: **An earlier sweep recorded here did not reproduce and has been removed.** It
#: claimed 6/6 with best 68.75 MHz and 5.5% margin on the worst seed. Rebuilt
#: later the same day, same toolchain and flags and with ``DETERMINISM_VARS``
#: set, the commit it described measures **60.03 MHz at seed 5** -- a passing
#: build with essentially no margin, not a comfortable one.
#:
#: The likely cause is a misread report rather than a flaky tool: ``top.tim``
#: carries four ``Max frequency`` lines (two clocks x placement/routing) and
#: only the third is the post-routing usb-domain result. See
#: ``docs/TIMING_CLOSURE.md`` for the re-measurement and the correct way to
#: extract it.
#:
#: Practical consequence: treat this design as having **no timing headroom**.
#: Any change that adds logic must be built and checked, and the presence of
#: ``build/top.bit`` -- not a frequency parsed out of the log -- is the pass
#: signal, because nextpnr writes no bitstream when it fails.
#:
#: What changed, and what the previous note here got wrong
#: ------------------------------------------------------
#: This comment used to name the binding path as a cone running
#: ``descriptor_generation`` -> ``map_store.bank_valid`` ->
#: ``sequence_class_allowed`` -> ``map_receiving`` -> ``map_store.crc.CE``.
#: That was stale. The measured path was:
#:
#:     host.enumerator.fsm_state -> [6 LUTs] -> clear
#:       -> [3.17 ns route, tile (21,46) -> (61,23)]
#:       -> injection_plane TX arbitration -> spi_link.tx_memory.WRE
#:     22 levels, 4.49 ns logic against 11.84 ns routing
#:
#: One wire, ``clear``, carried 3.17 ns -- 19% of the whole 60 MHz period --
#: because the enumerator sits at one end of the die and the SPI link at the
#: other. Two edits removed that whole class of path:
#:
#: * ``descriptor_export`` no longer masks its outputs with ``store.clear``
#: * ``DescriptorStore`` publishes ``descriptor_generation_changed`` instead of
#:   consumers re-deriving it with a 16-bit comparator on a cross-die bus
#:
#: **Neither edit works alone.** Measured separately they are both *worse* than
#: doing nothing -- 3/6 and 1/6 seeds respectively, against a 5/6 baseline --
#: because removing one long cone leaves the other binding while still
#: reshuffling placement. Together they give 6/6. Anyone tempted to revert half
#: of this should sweep, not build once.
#:
#: Seed-sensitivity is *not* caused by block-RAM congestion: moving 25 of 52
#: EBRs to LUTRAM took occupancy from 92% to 48% and left the distribution
#: essentially unchanged. See docs/BRAM_BUDGET.md.
#:
#: Still valid only while the netlist is unchanged: any RTL edit reshuffles
#: placement and the sweep should be redone. That is now cheap and meaningful,
#: because the result is finally reproducible end to end -- see
#: ``device.py``'s ``_RELAY_ENDPOINT_CLASSES``. Until 2026-09-15 it was not:
#: LUNA named three of the four relay endpoints after ``id(endpoint)``, so
#: every build synthesised a *different* netlist and no sweep described the
#: design rather than one throwaway netlist.
#:
#: **Every sweep recorded here before 2026-09-15 measured a netlist that no
#: longer exists, and could not have been reproduced even at the time.** The
#: superseded 2026-09-13 entry read 8 of 12 passing, seed 9 best at 65.45 MHz.
#:
#: Re-swept 2026-09-15 on the first reproducible netlist, sha 3c1c3404c085e7c2,
#: oss-cad-suite 2026-09-01 (yosys 0.68+136). 12 seeds run directly on the
#: synthesised top.json (~70 s each, synthesis is identical across seeds),
#: **12 of 12 passing**:
#:
#:     7: 69.05   2: 67.64   4: 65.98   12: 65.63   1: 65.42   3: 64.82
#:     5: 63.35   8: 62.20   10: 61.74  11: 61.58   6: 61.07   9: 60.95
#:
#: Do not read 12/12 as the fix having *improved* timing. It did not: it froze
#: a netlist that was previously redrawn every build, and this draw is a good
#: one. Sweeps of earlier random draws returned 8, 10 and 11 of 12, so 12/12
#: sits at the top of the observed range rather than outside it. What changed
#: is that the number is now a property of the design instead of a coin toss.
#:
#: The pin moved 9 -> 7 for the same reason. Seed 9 was inherited from a sweep
#: of a different netlist, and on this one it is the *worst* of the twelve at
#: 60.95 MHz (+1.58%), where seed 7 has +15.1%. Pinning the worst passing seed
#: was costing the design its entire margin for no reason.
#:
#: Read the verdict nextpnr prints on the frequency line, not the presence of
#: its --textcfg output: nextpnr writes the textcfg even when timing fails, so
#: "the file exists" is not a pass signal. (In the full LUNA flow no *bitstream*
#: appears, because ecppack never runs -- that one is a real signal.)
DEFAULT_PLACER_SEED = 7

#: The full option, including the weight and seed that were actually measured.
#: A caller-supplied ``--seed`` is composed after this one and wins, because
#: nextpnr takes the last occurrence of a repeated option.
REQUIRED_NEXTPNR_OPTS = f"{REQUIRED_NEXTPNR_FLAG} 60 --seed {DEFAULT_PLACER_SEED}"


#: Thread-count variables pinned so that placement is reproducible.
#:
#: nextpnr's own ``--threads`` does not reach the HeAP placer's linear solver,
#: which is Eigen and parallelises through OpenMP. Left unpinned, the analytic
#: placer diverges at its *first* iteration depending on how many cores happen
#: to be free, so the same netlist and the same seed produce a different
#: bitstream on a busy machine than on an idle one.
#:
#: Measured on this design: one configuration reported 68.75 MHz built on its
#: own and 63.59 MHz built alongside six others -- identical input, 5 MHz
#: apart, either side of the constraint. With these pinned it reports 68.75 MHz
#: under both loads.
#:
#: A pinned seed is meaningless without this, and so is any seed sweep: the
#: sweep recorded below is only reproducible because it was run with these set.
#: They are set unconditionally rather than defaulted, for the same reason
#: ``require_nextpnr_opts`` exists -- a developer with ``OMP_NUM_THREADS``
#: exported for an unrelated reason would otherwise silently lose determinism.
DETERMINISM_VARS = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "EIGEN_DONT_PARALLELIZE": "1",
}


#: Minimum yosys version that closes timing on this design.
#:
#: **The most load-bearing pin in this file, and it did not exist until
#: 2026-09-15.** From byte-identical source at ``3ba7d8b``, measured full-flow
#: at seed 9 with only the synthesis binary varied:
#:
#:     yosys 0.48+47  -> 56.57 MHz, no bitstream   (was the CI pin)
#:     yosys 0.53+15  -> 53.82 MHz, no bitstream
#:     yosys 0.60     -> 65.45 MHz, bitstream
#:     yosys 0.68+136 -> 68.71 MHz, bitstream, 10/12 seeds
#:
#: A 2x2 cross against nextpnr 0.7 vs 0.11.1 puts ~+9-10 MHz on the yosys axis
#: and only ~+2-3 MHz on the router axis -- newer yosys closes timing even on
#: the *old* pinned nextpnr. Synthesis is the half that matters.
#:
#: This file pinned nextpnr options and solver threads for a long time while
#: saying nothing about the synthesis tool. That is how one commit produced four
#: different answers on four machines, why every run of the CI bitstream job was
#: red, and why a toolchain artefact was misdiagnosed as an RTL regression.
#:
#: The *reason* newer yosys wins is unexplained. A 7.6x LUT7 macro difference
#: (197 vs 26) is a real correlate but is refuted as the cause: a netlist with
#: zero wide muxes still fails, and the passing 0.68 netlist keeps 291 L6MUX21.
#: Do not build reasoning on it. See docs/handoffs/TIMING_CLOSURE_HANDOFF.md.
MINIMUM_YOSYS_VERSION = (0, 60)

_YOSYS_VERSION_RE = re.compile(r"Yosys\s+(\d+)\.(\d+)")


def parse_yosys_version(text: str) -> tuple[int, int] | None:
    """Return ``(major, minor)`` from ``yosys -V`` output, or None if absent.

    ``yosys -V`` prints e.g. ``Yosys 0.68+136 (git sha1 c30457480-dirty, ...)``.
    Only the leading ``major.minor`` is compared; the ``+N`` commit count and the
    git suffix are deliberately ignored, because the measured cliff is between
    0.53 and 0.60 rather than at any particular build.
    """
    match = _YOSYS_VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def detect_yosys_version() -> tuple[int, int] | None:
    """Return the version of the ``yosys`` on PATH, or None if undeterminable.

    Returns None rather than raising when yosys is absent, unparseable or fails
    to run. The pure-Python test suite runs on machines with no FPGA toolchain,
    and a genuinely missing yosys fails later in the build with a clearer error
    than this guard could produce.
    """
    executable = shutil.which("yosys")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-V"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_yosys_version(completed.stdout or completed.stderr or "")


def require_yosys_version(version: tuple[int, int] | None) -> None:
    """Abort the build if `version` is below `MINIMUM_YOSYS_VERSION`.

    An unknown version (None) is permitted rather than fatal -- see
    `detect_yosys_version`. This mirrors `require_nextpnr_opts`: fail loudly up
    front rather than spend ten minutes producing no bitstream.
    """
    if version is None or version >= MINIMUM_YOSYS_VERSION:
        return
    have = ".".join(str(part) for part in version)
    want = ".".join(str(part) for part in MINIMUM_YOSYS_VERSION)
    raise SystemExit(
        f"yosys {have} is too old: this design does not close timing below "
        f"{want} and no bitstream is produced. Measured at 3ba7d8b from "
        f"identical source: 0.53 -> 53.82 MHz FAIL, 0.68 -> 68.71 MHz PASS. "
        "Install oss-cad-suite 2026-09-01 or newer, or put a newer yosys first "
        "on PATH. See docs/handoffs/TIMING_CLOSURE_HANDOFF.md."
    )


def compose_nextpnr_opts(existing: str | None) -> str:
    """Return nextpnr options with the required timing weight present.

    A caller-supplied value is preserved and placed *after* ours, so a
    deliberate later flag (``--seed 4``) still wins where nextpnr takes the
    last occurrence. Composition is idempotent: a value that already carries
    the flag is returned unchanged rather than accumulating duplicates.
    """
    if existing is None or not existing.strip():
        return REQUIRED_NEXTPNR_OPTS
    existing = existing.strip()
    if REQUIRED_NEXTPNR_FLAG in existing:
        return existing
    return f"{REQUIRED_NEXTPNR_OPTS} {existing}"


def require_nextpnr_opts(value: str) -> None:
    """Abort the build if `value` does not carry the required timing weight.

    This guards the composition itself. A build that silently drops the flag
    does not fail loudly — it produces no bitstream, or worse, a marginal one,
    which is exactly how a failing build went unnoticed on this branch before.
    """
    if REQUIRED_NEXTPNR_FLAG not in value:
        raise SystemExit(
            f"{NEXTPNR_OPTS_VAR}={value!r} is missing {REQUIRED_NEXTPNR_FLAG}. "
            "The ECP5 build does not close timing without it; see "
            "docs/TIMING_CLOSURE.md."
        )


def apply_build_environment(
    env: MutableMapping[str, str] | None = None, *, enforce_yosys: bool = False
) -> str:
    """Install the composed nextpnr options into `env` and return them.

    `env` is explicit so tests never mutate the real process environment.

    `enforce_yosys` is opt-in rather than on by default because this function is
    exercised throughout the pure-Python test suite, which must not shell out to
    a toolchain: CI's test job has no yosys at all, and defaulting to enforcement
    would make `pytest` abort on any developer machine whose PATH happens to
    resolve to an older yosys. The bitstream build passes it.
    """
    target = os.environ if env is None else env
    composed = compose_nextpnr_opts(target.get(NEXTPNR_OPTS_VAR))
    require_nextpnr_opts(composed)
    if enforce_yosys:
        require_yosys_version(detect_yosys_version())
    target[NEXTPNR_OPTS_VAR] = composed
    target.update(DETERMINISM_VARS)
    return composed
