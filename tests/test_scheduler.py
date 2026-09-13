from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.scheduler import FrameScheduler
from hurra_cynthion.timing import HostTiming


def simulate(bench) -> None:
    dut = FrameScheduler(HostTiming.simulation())
    simulation = Simulator(dut)
    simulation.add_clock(1e-6)

    async def wrapped(ctx) -> None:
        await bench(ctx, dut)

    simulation.add_testbench(wrapped)
    simulation.run()


def test_frame_tick_occurs_at_each_configured_interval() -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.enable, 1)
        ctx.set(dut.token_ready, 1)

        ticks = []
        starts = []
        for cycle in range(HostTiming.simulation().frame_cycles * 3 + 2):
            await ctx.tick()
            if ctx.get(dut.frame_tick):
                ticks.append(cycle + 1)
            if ctx.get(dut.sof_start):
                starts.append((cycle + 1, ctx.get(dut.frame_number)))

        frame_cycles = HostTiming.simulation().frame_cycles
        assert ticks == [frame_cycles, frame_cycles * 2, frame_cycles * 3]
        assert starts == [
            (frame_cycles, 0),
            (frame_cycles * 2, 1),
            (frame_cycles * 3, 2),
        ]

    simulate(bench)


def test_sof_start_remains_pending_until_token_is_ready() -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.enable, 1)
        ctx.set(dut.token_ready, 0)

        for _ in range(HostTiming.simulation().frame_cycles):
            await ctx.tick()

        assert ctx.get(dut.frame_tick)
        assert ctx.get(dut.sof_start)
        assert ctx.get(dut.frame_number) == 0

        for _ in range(3):
            await ctx.tick()
            assert not ctx.get(dut.frame_tick)
            assert ctx.get(dut.sof_start)
            assert ctx.get(dut.frame_number) == 0

        ctx.set(dut.token_ready, 1)
        await ctx.tick()
        assert not ctx.get(dut.sof_start)
        assert ctx.get(dut.frame_number) == 1

    simulate(bench)


def test_disable_cancels_pending_sof_and_restarts_interval() -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.enable, 1)
        for _ in range(HostTiming.simulation().frame_cycles):
            await ctx.tick()
        assert ctx.get(dut.sof_start)

        ctx.set(dut.enable, 0)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.sof_start)
        await ctx.tick()
        assert not ctx.get(dut.frame_tick)
        assert not ctx.get(dut.sof_start)

        ctx.set(dut.enable, 1)
        for _ in range(HostTiming.simulation().frame_cycles - 1):
            await ctx.tick()
            assert not ctx.get(dut.frame_tick)
        await ctx.tick()
        assert ctx.get(dut.frame_tick)
        assert ctx.get(dut.sof_start)

    simulate(bench)


def test_scheduler_has_bounded_counter_logic() -> None:
    dut = FrameScheduler(HostTiming.hardware())
    netlist = rtlil.convert(
        dut,
        ports=[
            dut.enable,
            dut.token_ready,
            dut.frame_tick,
            dut.sof_start,
            dut.frame_number,
        ],
    )
    assert len(netlist) < 50_000


def simulate_high_speed(bench) -> None:
    dut = FrameScheduler(HostTiming.simulation())
    simulation = Simulator(dut)
    simulation.add_clock(1e-6)

    async def wrapped(ctx) -> None:
        ctx.set(dut.high_speed, 1)
        await bench(ctx, dut)

    simulation.add_testbench(wrapped)
    simulation.run()


def test_high_speed_emits_a_sof_every_microframe() -> None:
    """At High Speed the SOF cadence is the 125 us microframe, not the 1 ms frame."""

    async def bench(ctx, dut) -> None:
        ctx.set(dut.enable, 1)
        ctx.set(dut.token_ready, 1)

        timing = HostTiming.simulation()
        ticks = []
        # No slop on the loop bound: at microframe_cycles=2 a spare cycle or
        # two is a whole extra period, unlike the 16-cycle Full Speed frame.
        for cycle in range(timing.microframe_cycles * 4):
            await ctx.tick()
            if ctx.get(dut.frame_tick):
                ticks.append(cycle + 1)

        microframe = timing.microframe_cycles
        assert ticks == [microframe, microframe * 2, microframe * 3, microframe * 4]

    simulate_high_speed(bench)


def test_high_speed_repeats_each_frame_number_across_eight_microframes() -> None:
    """Eight consecutive High Speed SOFs carry the same frame number.

    USB 2.0 section 8.4.3: the SOF packet carries an 11-bit frame number and
    nothing else -- the microframe index is never transmitted, it is implicit in
    position. The frame number advances once per 1 ms frame, so at High Speed it
    holds steady across eight SOFs rather than incrementing on each.

    Incrementing per microframe would run the frame number eight times too fast
    and put it permanently out of step with what the device counts.
    """

    async def bench(ctx, dut) -> None:
        ctx.set(dut.enable, 1)
        ctx.set(dut.token_ready, 1)

        timing = HostTiming.simulation()
        seen = []
        for _ in range(timing.microframe_cycles * 17):
            await ctx.tick()
            if ctx.get(dut.sof_start):
                seen.append(ctx.get(dut.frame_number))

        assert len(seen) >= 16, f"expected at least 16 SOFs, saw {len(seen)}"
        assert seen[0:8] == [0] * 8, seen
        assert seen[8:16] == [1] * 8, seen

    simulate_high_speed(bench)


def test_high_speed_frame_number_advances_once_per_eight_microframes() -> None:
    """The frame number and the microframe cadence stay locked at exactly 8:1."""

    async def bench(ctx, dut) -> None:
        ctx.set(dut.enable, 1)
        ctx.set(dut.token_ready, 1)

        timing = HostTiming.simulation()
        sof_count = 0
        for _ in range(timing.microframe_cycles * 25):
            await ctx.tick()
            if ctx.get(dut.sof_start):
                sof_count += 1
                assert ctx.get(dut.frame_number) == (sof_count - 1) // 8

    simulate_high_speed(bench)


def test_full_speed_is_unchanged_when_high_speed_is_deasserted() -> None:
    """The default path stays exactly as it was: one SOF and one increment per frame."""

    async def bench(ctx, dut) -> None:
        ctx.set(dut.enable, 1)
        ctx.set(dut.token_ready, 1)

        timing = HostTiming.simulation()
        seen = []
        for _ in range(timing.frame_cycles * 3 + 2):
            await ctx.tick()
            if ctx.get(dut.sof_start):
                seen.append(ctx.get(dut.frame_number))

        assert seen == [0, 1, 2], seen

    simulate(bench)
