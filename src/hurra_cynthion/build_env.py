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
#: Re-swept 2026-09-15 on the first reproducible netlist, sha 3c1c3404c085e7c2
#: as built inside the container from ``/work``, oss-cad-suite 2026-09-01
#: (yosys 0.68+136). That sha is **path-sensitive** -- yosys embeds source
#: paths in ``src`` attributes, so the same logic built elsewhere hashes
#: differently; a native build from a different directory gave a different sha
#: with identical logic (98 modules, 24249 cells) and the same fmax on all
#: twelve seeds. Compare shas only across builds from the same path. 12 seeds
#: run directly on the synthesised top.json (~70 s each, synthesis is identical
#: across seeds), **12 of 12 passing**:
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
#:
#: **Re-swept 2026-09-23 after the control relay landed: 8 of 12 pass.** Netlist
#: sha 05a170f1eca3e53a, built natively from ``~/hurra-work`` on the build
#: server (so not comparable with the container sha above -- see the
#: path-sensitivity note), same yosys 0.68+136 / nextpnr 0.11.1-19:
#:
#:     2: 63.83   1: 63.67   3: 63.57   10: 62.79  9: 62.13   8: 61.64
#:     6: 61.63   12: 60.08  |  7: 59.23  11: 58.75  4: 58.21   5: 54.85
#:
#: The pin moved 7 -> 2: seed 7 is now the third-worst and FAILS. This is a
#: real loss of margin, not only a redraw, and it is worth reading carefully:
#:
#: - The relay itself is not on any critical path. It adds about 1% logic
#:   (+221 LUTs, 79% -> 80% of the device; +16 distributed-RAM slices; no
#:   BRAM), and every failing seed's path runs through pre-existing
#:   injection-plane logic on 1.5 ns cross-die routes.
#: - Registering ``host.enumerated`` -- the head of the worst path -- moved
#:   the critical path to ``injection_plane.active_bank`` / ``link_ready``
#:   and swept 7 of 12, no better. There is a *family* of near-critical
#:   paths in the injection plane, and at 80% utilisation any added logic
#:   perturbs placement enough to surface one. It was reverted.
#: - So a single-signal fix will not recover 12/12. Doing so needs a
#:   timing pass over the injection plane's cross-die control signals.
#:
#: Note this is the -8 speed grade every build assumes. The BOM part is -6,
#: which closed 0 of 12 even before this change.
#:
#: **Re-swept 2026-09-23 after the relay's correctness fixes: 5 of 12 pass.**
#: Netlist sha 2ddb88fa23288f6a (native, ``~/hurra-work``):
#:
#:     4: 62.29   5: 61.72   1: 61.12   11: 60.38  9: 60.36  |  8: 59.96
#:     6: 59.60   3: 59.48   12: 58.51  7: 57.79   2: 57.73  10: 55.09
#:
#: The pin moved 2 -> 4: seed 2 now fails. Two review waves fixed real
#: defects in the relay's AUX handler (a permanent poller starvation, stale
#: replies, a STANDARD request forwarded to the real device) and added about
#: 550 LUTs doing it -- 19,448 -> 19,998, 80% -> 82% of the device. Seed 2
#: was already failing on the intermediate netlist (10/12, sha 69356e96),
#: which is the point: every netlist edit redraws this lottery.
#:
#: The critical path is still entirely pre-existing logic -- 26 hops in
#: injection_plane.engine plus copy_enable -- on every failing seed; no
#: relay or handler logic is on it. Placement pressure, not new slow logic.
#: Margin at the pin is +3.82%; the next netlist change should expect to
#: re-pin, and recovering a comfortable distribution needs the injection-plane
#: timing pass described above, or area back.
#:
#: **Re-swept 2026-09-23 after the /code-review fixes: 2 of 12 pass.** Netlist
#: sha 1d28ff1e6c316110 (native), 20,261 LUTs = 83% of the device:
#:
#:     8: 63.70   5: 62.68  |  12: 59.73  4: 59.22  10: 59.19  9: 59.02
#:     6: 58.52   11: 58.21  7: 57.72   2: 57.64   1: 56.88   3: 55.10
#:
#: The pin moved 4 -> 8: seed 4 now fails. The critical path is STILL entirely
#: pre-existing injection-plane logic (injection_plane.engine, map_receiving,
#: map_store) on every failing seed -- the arbiter, relay and handler appear
#: on none. But the trend across this branch is 12 -> 8 -> 10 -> 5 -> 2 of 12
#: as utilisation rose 79% -> 83%, and at 2/12 the next netlist edit is more
#: likely than not to leave the pin failing. The injection-plane timing pass
#: is no longer optional background work; it gates further RTL changes.
#:
#: **Re-swept 2026-09-24 after the injection-plane timing pass: 12 of 12
#: pass.** Netlist sha ad125c63565eb8de (native), 19,407 LUTs = 79%:
#:
#:     1: 70.16   10: 68.38  12: 67.65  4: 67.52   7: 67.21   9: 66.73
#:     11: 65.81  6: 65.59   2: 65.21   5: 64.45   3: 63.27   8: 63.20
#:
#: Median 66.2 MHz, worst +5.3%. Every failing path of the 2/12 netlist was
#: one of two 15-22 LUT combinational cones ending on map-store clock
#: enables, both ~80% routing delay:
#:
#: - ``poller.failed -> host.enumerated -> session_active -> invalidate``,
#:   cut by registering ``session_active`` / ``link_ready`` at the plane's
#:   edge (gateware.py). Registering it inside the host, as tried above, also
#:   delayed device.connect and broke the disconnect tests.
#: - ``map_store.active_bank -> engine output handshake -> *_ready ->
#:   command_ready -> rx_accept -> begin/entry/commit_accept``, cut by
#:   giving map frames their own accept term without ``command_ready`` --
#:   logically identical for them, since a map frame is never a command.
#:
#: Fixing only the first is the 7/12 attempt above: the second surfaced.
#: The new limiters are ``map_store`` bank state -> ``engine.working_*`` (the
#: snapshot path) and, on 3 seeds, ``control_relay.response_length`` ->
#: descriptor memory. The pin moved 8 -> 1; seed 8 is now the worst.
#:
#: **Re-swept 2026-09-24 after the zero-length-IN status fix: 12 of 12.**
#: Netlist sha 3227b06bb4985a69 (native), 19,370 LUTs = 79%:
#:
#:     6: 72.91   10: 71.64  4: 70.20   9: 69.81   2: 69.27   3: 69.18
#:     8: 69.06   1: 68.92   5: 68.75   11: 68.14  7: 66.02   12: 65.01
#:
#: A one-term handler change redrew placement and the whole distribution
#: moved up (median 66.2 -> 69.1, worst +5.3% -> +8.4%) -- noise, not an
#: improvement, but noise now well clear of the constraint. Pin 1 -> 6.
#:
#: EP0 packetisation (device-class gaps G2) then cost margin without being on
#: any failing path: netlist 9d2a5243 swept 12/12 but median 66.5, worst
#: +1.3%, every path in the injection plane. Three families were cut in turn,
#: each surfacing as the last one went: plane_link_ready -> map_store
#: active_valid (``invalidate`` taken out of its cone); rx_staged_type and
#: map_store active_bank -> engine working_* (the engine registers its
#: command decisions one state early, in BASE_CAPTURE); active_bank ->
#: the clock enables of two diagnostic counters (counted a cycle late).
#: Netlist sha 4209e4773f42b74e (native), 18,610 LUTs = 76%:
#:
#:     3: 74.91   9: 74.59   1: 74.24   8: 72.79   5: 72.57   11: 70.95
#:     12: 70.94  4: 70.14   2: 69.45   6: 68.45   7: 68.33   10: 68.32
#:
#: Median 70.9, worst 68.32 (+13.9%). Pin 6 -> 3.
#:
#: The interrupt-OUT relay (G3: an OUT writer on the shared engine, a runtime
#: OUT endpoint on the clone) added about 800 LUTs and stayed 12/12. Netlist
#: sha bb129738439180a6 (native), 19,410 LUTs = 79%:
#:
#:     8: 73.31   7: 72.07   6: 71.58   1: 71.43   10: 71.07  12: 70.38
#:     2: 70.23   11: 70.11  4: 69.95   5: 69.04   3: 68.15   9: 67.23
#:
#: Median 70.3, worst 67.23 (+12.1%). Pin 3 -> 8.
#:
#: Boot-protocol forwarding (G4: SET_IDLE/SET_PROTOCOL forwarded, a tracker and
#: replay beside the control relay, engine/plane stand-down gates) added about
#: 250 LUTs and stayed 12/12, but drew a worse placement. Netlist sha
#: b621337b76cf612d (native), 19,656 LUTs = 80%:
#:
#:     10: 69.50  7: 68.12   6: 67.82   5: 67.54   9: 67.11   3: 66.57
#:     4: 64.25   12: 64.18  2: 63.70   11: 63.66  1: 62.81   8: 62.59
#:
#: Median 65.4, worst 62.59 (+4.3%). No G4 logic is on any critical path: 10
#: of 12 seeds end in a newly surfaced family, ``host.enumerator`` capture
#: counters (report_cursor, descriptor_position) -> descriptor_store capture /
#: admin scanner -> the clone's GET_DESCRIPTOR streamer clock enables; the
#: other two are the known map_store.active_bank -> engine / report relay
#: family. Pin 8 -> 10.
#:
#: Both families cut together: the enumerator registers its store control
#: strobes (start/commit/abort/clear shift together, so their order holds),
#: and the top registers the plane-to-clone report stream (exact, since the
#: report relay never backpressures). Netlist sha 24744ceea70a6d98 (native),
#: 19,820 LUTs = 81%:
#:
#:     7: 76.28   6: 75.82   9: 74.77   1: 74.26   8: 74.01   12: 73.83
#:     10: 73.28  5: 72.71   2: 72.22   11: 71.93  3: 71.56   4: 70.75
#:
#: Median 73.6, worst 70.75 (+17.9%) -- the best distribution recorded. The
#: next family, on 8 of 12 seeds: the clone's token_detector timer -> its
#: GET_DESCRIPTOR streamer -> descriptor_memory's read address. Pin 10 -> 7.
#:
#: Endpoint numbers 1..15 (G5: the clone's relay IN endpoints and the report
#: relay's queues bound to runtime numbers, a duplicate-number refusal, a
#: 16-bit boot mask) added about 100 LUTs. Netlist sha 8da526b3afff261c
#: (native), 19,920 LUTs = 82%:
#:
#:     6: 74.32   12: 73.55  8: 71.96   9: 71.46   7: 71.45   4: 71.44
#:     2: 71.24   11: 71.17  1: 70.05   10: 69.80  3: 67.62   5: 67.35
#:
#: Median 71.3, worst 67.35 (+12.3%). The leading family, on 11 of 12 seeds,
#: is G5's own: the clone's token timer -> a relay IN endpoint's runtime
#: number compare -> its tx_manager data enables. Pin 7 -> 6.
#:
#: A PC session reset (bus reset or SET_CONFIGURATION) now flushes the report
#: relay and resets every relay IN endpoint -- a reset mux on each of their
#: registers. Netlist sha 634f5786c6dd5183 (native), 19,869 LUTs = 81%:
#:
#:     10: 72.16  8: 71.48   4: 71.39   2: 70.15   1: 69.36   6: 69.35
#:     7: 69.21   9: 68.64   12: 68.64  3: 66.80   5: 65.04   11: 65.02
#:
#: Median 69.3, worst 65.02 (+8.4%). 11 of 12 seeds end in the clone ->
#: descriptor_store.descriptor_memory read-address family (handshake, timer
#: or setup decoder -> the GET_DESCRIPTOR streamer); the twelfth in a relay IN
#: endpoint's tx_manager. None ends in the flush logic. Pin 6 -> 10.
#:
#: Injection on a still device: the map store's ``activated`` pulse and
#: indexed directory read, the engine's SEED_* walk, button/mask commands
#: synthesised when unchanged, a 64-bit ``click_restore`` in the state record.
#: About 330 LUTs. Netlist sha 0e9f458c3177d88e (native), 20,196 LUTs = 83%:
#:
#:     4: 71.15   11: 71.06  12: 70.20  7: 70.07   1: 70.01   8: 68.54
#:     2: 68.25   9: 67.89   5: 67.12   3: 66.99   10: 65.45  6: 65.15
#:
#: Median 68.4, worst 65.15 (+8.6%). A first draft of the same logic (one
#: more FSM state, three more muxes) drew median 72.7, worst +16.3% -- the
#: spread of placement, not of the logic. 10 of 12 seeds end in the clone's
#: control endpoint -> a relay IN endpoint's tx_manager buffer read (the G5
#: family); 2 in map_store.active_bank -> engine.layout_state data / its
#: write enable (the bank -> commit family), both at +13%. Pin 10 -> 4.
DEFAULT_PLACER_SEED = 4

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


def resolve_yosys_executable() -> str | None:
    """Return the yosys binary the *build* will run, or None if there is none.

    LUNA's generated ``build_top.sh`` starts with ``: ${YOSYS:=yosys}``, so the
    build honours ``$YOSYS`` and only falls back to a PATH lookup. This guard
    has to resolve the same way or it is not guarding the build: the container
    pins ``YOSYS`` to an absolute path inside the oss-cad-suite bundle while a
    different, older yosys can sit earlier on PATH, and checking the wrong one
    can either reject the toolchain that would have worked or bless the one
    that will not.
    """
    override = os.environ.get("YOSYS")
    if override:
        # Honour it whether it is an absolute path or a bare name, exactly as
        # the shell would; shutil.which() on an absolute path validates that it
        # exists and is executable, which is what we want either way.
        return shutil.which(override) or (override if os.path.isfile(override) else None)
    return shutil.which("yosys")


def detect_yosys_version() -> tuple[int, int] | None:
    """Return the version of the yosys the build will use, or None if unknown.

    Returns None rather than raising when yosys is absent, unparseable or fails
    to run. The pure-Python test suite runs on machines with no FPGA toolchain,
    and a genuinely missing yosys fails later in the build with a clearer error
    than this guard could produce.
    """
    executable = resolve_yosys_executable()
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
        f"yosys {have} is too old: below {want} this design closes on only a "
        "small minority of placer seeds, so a build is a coin toss rather than "
        "a result. Measured on deterministic netlists, same source, same "
        "nextpnr, twelve seeds each: 0.48 -> 3 of 12 pass; 0.68 -> 12 of 12. "
        f"{want} is a reliability floor, not the point at which a bitstream "
        "first becomes possible. Install oss-cad-suite 2026-09-01 or newer, or "
        "set YOSYS to a newer binary. See CLAUDE.md, 'The yosys floor, "
        "measured properly'."
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
