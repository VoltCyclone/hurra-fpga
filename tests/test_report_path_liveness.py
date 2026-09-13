"""Liveness of the shared injection engine against every relay condition.

The engine's ``OUTPUT`` state (``injection.py``) has exactly two exits:
``output_ready`` and ``snapshot_lost``. For an *unmapped* report
``transaction_mapped`` is 0, so ``snapshot_lost`` is identically 0 and
``output_ready`` -- which is ``ReportRelay.report_ready`` -- is the only exit
that exists. Anything that can hold that signal low forever wedges the whole
report path for every endpoint, not just the one that stalled.

These tests wire the production ``ReportInjectionDataPlane`` to the production
``ReportRelay`` with exactly the connections ``gateware.py`` makes, so the
liveness claim is asserted against the real modules rather than a model.
"""

from amaranth import Elaboratable, Module
from amaranth.sim import Simulator

from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.gateware import ReportInjectionDataPlane
from hurra_cynthion.relay import ReportRelay

# Anything longer than the engine needs to walk CAPTURE -> ... -> OUTPUT and
# drain one report. Generous, but bounded: the defect under test is an
# unbounded hang, so any finite budget distinguishes it.
ACCEPT_BUDGET = 2000


class LivenessHarness(Elaboratable):
    """``DescriptorStore`` + data plane + relay, wired as ``gateware.py`` wires them."""

    def __init__(self) -> None:
        self.store = DescriptorStore()
        self.plane = ReportInjectionDataPlane(self.store, max_fields=8)
        self.relay = ReportRelay()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.store = self.store
        m.submodules.plane = self.plane
        m.submodules.relay = self.relay

        plane = self.plane
        relay = self.relay
        # gateware.py's injection_plane -> device -> relay chain, collapsed.
        # device.py forwards these five straight through to the relay and
        # returns relay.report_ready unmodified.
        m.d.comb += [
            plane.output_ready.eq(relay.report_ready),
            relay.report_valid.eq(plane.output_valid),
            relay.report_data.eq(plane.output_data),
            relay.report_first.eq(plane.output_first),
            relay.report_last.eq(plane.output_last),
            relay.report_endpoint.eq(plane.output_endpoint),
        ]
        return m


async def _initialize(ctx, harness: LivenessHarness) -> None:
    """Passthrough mode: enumerated session, no MCU attached.

    Every clone stream starts un-ready and is drained explicitly by
    ``_collect``, so a report cannot be consumed before the test looks at it.
    The relay must accept reports regardless -- that is the property here.
    """
    plane = harness.plane
    ctx.set(plane.session_active, 1)
    ctx.set(plane.link_ready, 0)
    ctx.set(plane.tx_ready, 1)
    for stream in harness.relay.streams:
        ctx.set(stream.ready, 0)
    await ctx.tick("usb")


async def _push_report(ctx, harness: LivenessHarness, endpoint: int, payload: bytes) -> None:
    """Offer one whole report at the data plane's *input*.

    Raises with the endpoint named if the input never accepts, which is the
    shape the wedge takes: the report *after* the one that wedged the engine
    is the one that cannot get in.
    """
    plane = harness.plane
    ctx.set(plane.report_interface, 0)
    ctx.set(plane.report_endpoint, endpoint)
    for index, byte in enumerate(payload):
        ctx.set(plane.report_data, byte)
        ctx.set(plane.report_first, 1 if index == 0 else 0)
        ctx.set(plane.report_last, 1 if index == len(payload) - 1 else 0)
        ctx.set(plane.report_valid, 1)
        for _ in range(ACCEPT_BUDGET):
            if ctx.get(plane.report_ready):
                break
            await ctx.tick("usb")
        else:
            ctx.set(plane.report_valid, 0)
            raise AssertionError(
                f"report for endpoint {endpoint} was never accepted "
                f"(engine wedged in OUTPUT): output_valid="
                f"{ctx.get(plane.output_valid)} relay_report_ready="
                f"{ctx.get(harness.relay.report_ready)}"
            )
        await ctx.tick("usb")
    ctx.set(plane.report_valid, 0)
    ctx.set(plane.report_first, 0)
    ctx.set(plane.report_last, 0)


async def _collect(ctx, stream, count: int, *, budget: int = ACCEPT_BUDGET, hold: bool = False):
    """Drain up to ``count`` bytes, raising ``ready`` for the duration."""
    ctx.set(stream.ready, 1)
    out = []
    for _ in range(budget):
        if ctx.get(stream.valid):
            out.append((ctx.get(stream.payload), ctx.get(stream.last)))
            if len(out) == count:
                break
        await ctx.tick("usb")
    if not hold:
        ctx.set(stream.ready, 0)
    return out


def test_report_for_an_unserved_endpoint_number_does_not_wedge_the_shared_engine() -> None:
    harness = LivenessHarness()
    sim = Simulator(harness)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await _initialize(ctx, harness)
        # A device declaring bEndpointAddress = 0x85 tags its reports endpoint 5.
        await _push_report(ctx, harness, 5, bytes([0xE5, 0x01, 0x02, 0x03]))
        # The engine must still serve every endpoint it *can* serve.
        await _push_report(ctx, harness, 1, bytes([0xA1, 0x11, 0x12, 0x13]))
        await _push_report(ctx, harness, 1, bytes([0xA2, 0x21, 0x22, 0x23]))

        collected = await _collect(ctx, harness.relay.streams[0], 8)
        assert [byte for byte, _ in collected] == [
            0xA1,
            0x11,
            0x12,
            0x13,
            0xA2,
            0x21,
            0x22,
            0x23,
        ], f"endpoint 1 bytes were lost or reordered: {collected}"

    sim.add_testbench(bench)
    sim.run()


def test_report_for_an_unserved_endpoint_number_is_dropped_whole() -> None:
    harness = LivenessHarness()
    sim = Simulator(harness)
    sim.add_clock(1e-6, domain="usb")

    pulses = []

    async def monitor(ctx):
        async for _clk, _rst, unmatched in ctx.tick("usb").sample(harness.relay.unmatched_report):
            if unmatched:
                pulses.append(1)

    async def bench(ctx):
        await _initialize(ctx, harness)
        await _push_report(ctx, harness, 5, bytes([0xE5, 0x01, 0x02, 0x03]))
        await _push_report(ctx, harness, 1, bytes([0xA1, 0x11, 0x12, 0x13]))

        collected = await _collect(ctx, harness.relay.streams[0], 4)
        assert [byte for byte, _ in collected] == [0xA1, 0x11, 0x12, 0x13]
        assert 0xE5 not in [byte for byte, _ in collected]
        assert len(pulses) == 1, f"expected exactly one unmatched-report pulse, saw {len(pulses)}"

    sim.add_process(monitor)
    sim.add_testbench(bench)
    sim.run()


def test_congested_endpoint_does_not_stall_other_endpoints() -> None:
    harness = LivenessHarness()
    sim = Simulator(harness)
    sim.add_clock(1e-6, domain="usb")

    congested = []

    async def monitor(ctx):
        async for _clk, _rst, pulse in ctx.tick("usb").sample(harness.relay.congested_report):
            if pulse:
                congested.append(1)

    async def bench(ctx):
        # Endpoint 2's stream (index 1) is never drained: a composite interface
        # the host enumerated but never bound.
        await _initialize(ctx, harness)
        for index in range(40):
            await _push_report(ctx, harness, 2, bytes([0xB0 | (index & 0x0F), 0x00, 0x00, 0x00]))
            if congested:
                break
        assert congested, "endpoint 2's queue never reported congestion"

        for index in range(4):
            await _push_report(ctx, harness, 1, bytes([0xA0 + index, 0x11, 0x22, 0x33]))
        collected = await _collect(ctx, harness.relay.streams[0], 16)
        first_bytes = [byte for byte, _ in collected][::4]
        assert first_bytes == [0xA0, 0xA1, 0xA2, 0xA3], (
            f"only {len(first_bytes)}/4 endpoint 1 reports relayed after endpoint 2 "
            f"filled: {collected}"
        )

    sim.add_process(monitor)
    sim.add_testbench(bench)
    sim.run()


def test_congested_endpoint_recovers_when_the_pc_resumes_polling() -> None:
    harness = LivenessHarness()
    sim = Simulator(harness)
    sim.add_clock(1e-6, domain="usb")

    congested = []

    async def monitor(ctx):
        async for _clk, _rst, pulse in ctx.tick("usb").sample(harness.relay.congested_report):
            congested.append(bool(pulse))

    async def bench(ctx):
        await _initialize(ctx, harness)
        for index in range(40):
            await _push_report(ctx, harness, 2, bytes([0xB0 | (index & 0x0F), 0, 0, 0]))
            if any(congested):
                break
        assert any(congested), "endpoint 2's queue never reported congestion"

        # The PC binds the interface and starts polling.
        await _collect(ctx, harness.relay.streams[1], 512, budget=4000)

        congested.clear()
        await _push_report(ctx, harness, 2, bytes([0xC7, 0xAA, 0xBB, 0xCC]))
        collected = await _collect(ctx, harness.relay.streams[1], 4)
        assert [byte for byte, _ in collected] == [
            0xC7,
            0xAA,
            0xBB,
            0xCC,
        ], f"endpoint 2 did not resume after draining: {collected}"
        assert not any(congested), "congestion drop is sticky after the queue drained"

    sim.add_process(monitor)
    sim.add_testbench(bench)
    sim.run()


def test_relay_report_ready_is_unconditionally_high() -> None:
    """The structural liveness invariant that replaces a watchdog in ``OUTPUT``.

    If a future change reintroduces backpressure at this seam, this fails
    before any deadlock can be observed on hardware.
    """
    relay = ReportRelay(endpoint_numbers=(1, 2), fifo_depth=8, max_report_bytes=4)
    sim = Simulator(relay)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        for stream in relay.streams:
            ctx.set(stream.ready, 0)
        ctx.set(relay.report_data, 0x5A)
        # Sweep fill level from empty to well past full, and at every level
        # sweep the whole 4-bit endpoint field.
        for fill in range(12):
            for endpoint in range(16):
                ctx.set(relay.report_endpoint, endpoint)
                ctx.set(relay.report_valid, 0)
                await ctx.tick("usb")
                assert (
                    ctx.get(relay.report_ready) == 1
                ), f"report_ready dropped at fill={fill} endpoint={endpoint}"
            # Advance the fill level by pushing one whole report at endpoint 1.
            ctx.set(relay.report_endpoint, 1)
            for byte_index in range(4):
                ctx.set(relay.report_first, 1 if byte_index == 0 else 0)
                ctx.set(relay.report_last, 1 if byte_index == 3 else 0)
                ctx.set(relay.report_valid, 1)
                assert ctx.get(relay.report_ready) == 1
                await ctx.tick("usb")
            ctx.set(relay.report_valid, 0)
            ctx.set(relay.report_last, 0)

    sim.add_testbench(bench)
    sim.run()
