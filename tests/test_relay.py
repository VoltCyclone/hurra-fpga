import pytest
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.descriptors import MAX_RELAY_ENDPOINT_NUMBER, RELAY_ENDPOINT_NUMBERS
from hurra_cynthion.relay import ReportRelay


def _relay_ports(dut):
    ports = [
        dut.report_valid,
        dut.report_ready,
        dut.report_data,
        dut.report_last,
        dut.report_endpoint,
    ]
    for stream in dut.streams:
        ports.extend([stream.valid, stream.ready, stream.payload, stream.last])
    return ports


async def _push_report(ctx, dut, endpoint, payload):
    ctx.set(dut.report_endpoint, endpoint)
    for i, byte in enumerate(payload):
        ctx.set(dut.report_data, byte)
        ctx.set(dut.report_first, 1 if i == 0 else 0)
        ctx.set(dut.report_last, 1 if i == len(payload) - 1 else 0)
        ctx.set(dut.report_valid, 1)
        while not ctx.get(dut.report_ready):
            await ctx.tick("usb")
        await ctx.tick("usb")
    ctx.set(dut.report_valid, 0)


async def _push_byte(ctx, dut, endpoint, byte, *, last=False):
    # Deliberately never sets ``report_first``: whole-report admission keys on
    # the relay's own ``in_report`` tracking (derived from ``report_last``),
    # not on ``report_first``. The natural reading is that this helper should
    # set it; it must not, or the tests would stop exercising that path.
    ctx.set(dut.report_endpoint, endpoint)
    ctx.set(dut.report_data, byte)
    ctx.set(dut.report_last, last)
    ctx.set(dut.report_valid, 1)
    for _ in range(300):
        if ctx.get(dut.report_ready):
            await ctx.tick("usb")
            ctx.set(dut.report_valid, 0)
            return
        await ctx.tick("usb")
    raise AssertionError("relay did not accept byte within its bounded budget")


async def _drain_stream(ctx, stream, count):
    ctx.set(stream.ready, 1)
    out = []
    saw_last = False
    for _ in range(200):
        if ctx.get(stream.valid):
            out.append(ctx.get(stream.payload))
            if ctx.get(stream.last):
                saw_last = True
            if len(out) == count:
                break
        await ctx.tick("usb")
    return out, saw_last


async def _pop_stream(ctx, stream):
    ctx.set(stream.ready, 1)
    for _ in range(20):
        if ctx.get(stream.valid):
            value = (ctx.get(stream.payload), ctx.get(stream.last))
            await ctx.tick("usb")
            ctx.set(stream.ready, 0)
            return value
        await ctx.tick("usb")
    raise AssertionError("relay stream did not produce a byte")


def test_relay_uses_exactly_four_independent_9x128_memories():
    """Four independent per-endpoint rings, held in LUTRAM.

    The invariant is one memory primitive per endpoint -- not a shared FIFO
    and not an unrolled register array. The storage *class* is deliberately
    distributed: at 9x128 each ring fills 6% of an 18,432-bit EBR, so four
    of them burned four whole blocks. In LUTRAM they cost 24 DPR16X4 each
    (ceil(9/4) x ceil(128/16)), which is far under the break-even where a
    block is worth spending. See docs/BRAM_BUDGET.md.
    """
    dut = ReportRelay(endpoint_numbers=(1, 2, 3, 4), fifo_depth=128)
    converted = rtlil.convert(dut, ports=_relay_ports(dut))

    assert converted.count("memory width 9 size 128") == 4
    assert converted.count('attribute \\ram_style "distributed"') == 4
    assert "memory width 9 size 127" not in converted
    assert "SyncFIFO" not in converted
    assert "\\top.fifo_" not in converted
    assert "DPR16X4" not in converted


def test_report_routed_to_matching_endpoint_stream_with_last():
    dut = ReportRelay(endpoint_numbers=(1, 2, 3, 4))
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")
    payload = [0x01, 0x10, 0x20, 0x00]

    async def bench(ctx):
        # endpoint 2 -> streams[1]
        await _push_report(ctx, dut, endpoint=2, payload=payload)
        out, saw_last = await _drain_stream(ctx, dut.streams[1], len(payload))
        assert out == payload
        assert saw_last
        # endpoint 1's stream got nothing
        assert ctx.get(dut.streams[0].valid) == 0

    sim.add_testbench(bench)
    sim.run()


def test_empty_enqueue_has_at_most_one_synchronous_read_cycle_delay():
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=128)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        stream = dut.streams[0]
        ctx.set(stream.ready, 0)
        assert not ctx.get(stream.valid)
        await _push_byte(ctx, dut, 1, 0xA5, last=True)

        for _ in range(2):
            if ctx.get(stream.valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("empty enqueue exceeded one read-cycle delay")

        assert ctx.get(stream.payload) == 0xA5
        assert ctx.get(stream.last)

    sim.add_testbench(bench)
    sim.run()


def test_full_128_byte_capacity_wraps_without_losing_order():
    # max_report_bytes=1: this test drives raw bytes rather than whole reports,
    # and the production 64-byte reservation would refuse admission above
    # level 64, which is not the property under test here.
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=128, max_report_bytes=1)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        stream = dut.streams[0]
        ctx.set(stream.ready, 0)
        for value in range(128):
            await _push_byte(ctx, dut, 1, value, last=value == 127)

        # A full queue drops the next report rather than deasserting ready.
        ctx.set(dut.report_endpoint, 1)
        ctx.set(dut.report_valid, 1)
        ctx.set(dut.report_data, 0xEE)
        assert ctx.get(dut.report_ready)
        assert ctx.get(dut.congested_report)
        await ctx.tick("usb")
        ctx.set(dut.report_valid, 0)

        first = [await _pop_stream(ctx, stream) for _ in range(64)]
        assert [value for value, _last in first] == list(range(64))
        assert not any(last for _value, last in first)

        for value in range(128, 192):
            await _push_byte(ctx, dut, 1, value & 0xFF, last=value == 191)

        remaining = [await _pop_stream(ctx, stream) for _ in range(128)]
        assert [value for value, _last in remaining] == [value & 0xFF for value in range(64, 192)]
        assert [index for index, (_value, last) in enumerate(remaining) if last] == [63, 127]

    sim.add_testbench(bench)
    sim.run()


def test_full_queue_refuses_a_new_report_even_while_the_same_endpoint_dequeues():
    # Was ``test_full_queue_accepts_enqueue_when_same_endpoint_dequeues``. The
    # ``| dequeue`` term it pinned squeezed one more *byte* into a full queue,
    # which cannot be reconciled with whole-report admission: admission is
    # decided at the report boundary against the level, and a level that is
    # about to fall by one says nothing about whether the whole report fits.
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=4, max_report_bytes=1)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        stream = dut.streams[0]
        ctx.set(stream.ready, 0)
        for value in range(4):
            await _push_byte(ctx, dut, 1, value, last=value == 3)

        for _ in range(2):
            if ctx.get(stream.valid):
                break
            await ctx.tick("usb")
        assert ctx.get(stream.valid)
        assert ctx.get(stream.payload) == 0

        ctx.set(dut.report_endpoint, 1)
        ctx.set(dut.report_data, 4)
        ctx.set(dut.report_last, 1)
        ctx.set(dut.report_valid, 1)
        ctx.set(stream.ready, 1)
        # Ready stays high -- the relay never backpressures -- but the report
        # is refused and counted rather than partially enqueued.
        assert ctx.get(dut.report_ready)
        assert ctx.get(dut.congested_report)
        assert ctx.get(stream.payload) == 0
        await ctx.tick("usb")
        ctx.set(dut.report_valid, 0)
        ctx.set(stream.ready, 0)

        remaining = [await _pop_stream(ctx, stream) for _ in range(3)]
        assert [value for value, _last in remaining] == [1, 2, 3]
        assert [last for _value, last in remaining] == [False, False, True]
        # The refused byte left nothing behind.
        ctx.set(stream.ready, 1)
        for _ in range(4):
            await ctx.tick("usb")
        assert not ctx.get(stream.valid)

    sim.add_testbench(bench)
    sim.run()


def test_continuous_primed_drain_produces_one_byte_per_clock():
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=128)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")
    payload = list(range(32))

    async def bench(ctx):
        stream = dut.streams[0]
        ctx.set(stream.ready, 0)
        await _push_report(ctx, dut, 1, payload)

        ctx.set(stream.ready, 1)
        received = []
        started = False
        for _ in range(40):
            if ctx.get(stream.valid):
                started = True
                received.append(ctx.get(stream.payload))
                if len(received) == len(payload):
                    assert ctx.get(stream.last)
                    break
            elif started:
                raise AssertionError("primed relay drain inserted a bubble")
            await ctx.tick("usb")
        assert received == payload

    sim.add_testbench(bench)
    sim.run()


def test_stalled_head_payload_and_last_are_stable():
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=128)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        stream = dut.streams[0]
        ctx.set(stream.ready, 0)
        await _push_byte(ctx, dut, 1, 0xC3, last=True)
        for _ in range(2):
            if ctx.get(stream.valid):
                break
            await ctx.tick("usb")
        assert ctx.get(stream.valid)
        held = (ctx.get(stream.payload), ctx.get(stream.last))
        for _ in range(12):
            assert ctx.get(stream.valid)
            assert (ctx.get(stream.payload), ctx.get(stream.last)) == held
            await ctx.tick("usb")
        assert held == (0xC3, 1)

    sim.add_testbench(bench)
    sim.run()


def test_two_endpoint_streams_drain_independently_on_the_same_clocks():
    dut = ReportRelay(endpoint_numbers=(1, 2), fifo_depth=128)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await _push_report(ctx, dut, 1, [0x10, 0x11, 0x12, 0x13])
        await _push_report(ctx, dut, 2, [0x20, 0x21, 0x22, 0x23])
        for stream in dut.streams:
            ctx.set(stream.ready, 1)

        outputs = [[], []]
        simultaneous = 0
        for _ in range(20):
            valid = [bool(ctx.get(stream.valid)) for stream in dut.streams]
            simultaneous += valid[0] and valid[1]
            for index, stream in enumerate(dut.streams):
                if valid[index]:
                    outputs[index].append(ctx.get(stream.payload))
            if all(len(output) == 4 for output in outputs):
                break
            await ctx.tick("usb")

        assert outputs == [[0x10, 0x11, 0x12, 0x13], [0x20, 0x21, 0x22, 0x23]]
        assert simultaneous == 4

    sim.add_testbench(bench)
    sim.run()


def test_full_endpoint_a_does_not_block_endpoint_b_enqueue():
    dut = ReportRelay(endpoint_numbers=(1, 2), fifo_depth=4, max_report_bytes=1)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        for stream in dut.streams:
            ctx.set(stream.ready, 0)
        for value in range(4):
            await _push_byte(ctx, dut, 1, value, last=value == 3)

        # Endpoint 1 is full: a fresh report there is counted as congested,
        # while ready stays high so the shared engine keeps draining.
        ctx.set(dut.report_endpoint, 1)
        ctx.set(dut.report_data, 0xB1)
        ctx.set(dut.report_last, 1)
        ctx.set(dut.report_valid, 1)
        assert ctx.get(dut.report_ready)
        assert ctx.get(dut.congested_report)
        await ctx.tick("usb")
        ctx.set(dut.report_valid, 0)

        # Endpoint 2 is untouched by endpoint 1's congestion.
        ctx.set(dut.report_endpoint, 2)
        assert ctx.get(dut.report_ready)
        await _push_byte(ctx, dut, 2, 0xB2, last=True)
        assert await _pop_stream(ctx, dut.streams[1]) == (0xB2, 1)

    sim.add_testbench(bench)
    sim.run()


def test_interleaved_endpoints_preserve_per_endpoint_order_and_last():
    dut = ReportRelay(endpoint_numbers=(1, 2), fifo_depth=128)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        for endpoint, byte, last in (
            (1, 0x11, False),
            (2, 0x21, True),
            (1, 0x12, True),
            (2, 0x22, False),
            (1, 0x13, True),
            (2, 0x23, True),
        ):
            await _push_byte(ctx, dut, endpoint, byte, last=last)

        endpoint_1 = [await _pop_stream(ctx, dut.streams[0]) for _ in range(3)]
        endpoint_2 = [await _pop_stream(ctx, dut.streams[1]) for _ in range(3)]
        assert endpoint_1 == [(0x11, 0), (0x12, 1), (0x13, 1)]
        assert endpoint_2 == [(0x21, 1), (0x22, 0), (0x23, 1)]

    sim.add_testbench(bench)
    sim.run()


def test_unmatched_endpoint_is_absorbed_without_backpressure():
    # Was ``test_unmatched_endpoint_never_asserts_ready``, whose name asserted
    # the defect: deasserting ready for an endpoint number the relay cannot
    # serve wedges the shared injection engine in OUTPUT forever.
    dut = ReportRelay(endpoint_numbers=(1, 2, 3, 4), fifo_depth=128)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        for stream in dut.streams:
            ctx.set(stream.ready, 0)
        ctx.set(dut.report_data, 0x5A)
        ctx.set(dut.report_last, 1)
        for endpoint in (0, 5, 15):
            ctx.set(dut.report_endpoint, endpoint)
            ctx.set(dut.report_valid, 1)
            assert ctx.get(dut.report_ready), f"endpoint {endpoint} backpressured the engine"
            assert ctx.get(dut.unmatched_report)
            await ctx.tick("usb")
        ctx.set(dut.report_valid, 0)

        # Nothing reached any queue.
        for _ in range(4):
            await ctx.tick("usb")
        for index, stream in enumerate(dut.streams):
            ctx.set(stream.ready, 1)
            assert not ctx.get(stream.valid), f"stream {index} enqueued an unmatched report"

    sim.add_testbench(bench)
    sim.run()


def test_full_endpoint_drops_reports_instead_of_deasserting_ready():
    # Was ``test_report_ready_deasserts_when_fifo_full``. fifo_depth=4 with
    # max_report_bytes=1 keeps the original admission arithmetic while the
    # assertion moves from backpressure to a counted drop.
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=4, max_report_bytes=1)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    congested = []

    async def monitor(ctx):
        async for _clk, _rst, pulse in ctx.tick("usb").sample(dut.congested_report):
            if pulse:
                congested.append(1)

    async def bench(ctx):
        ctx.set(dut.streams[0].ready, 0)
        ctx.set(dut.report_endpoint, 1)
        ctx.set(dut.report_data, 0xAB)
        ctx.set(dut.report_last, 1)
        ctx.set(dut.report_valid, 1)
        # Never drain streams[0]; fill the FIFO and observe drops, not stalls.
        for _ in range(20):
            await ctx.tick("usb")
            assert ctx.get(dut.report_ready), "relay deasserted ready on a full queue"
        assert congested, "a full queue neither accepted nor counted a dropped report"

    sim.add_process(monitor)
    sim.add_testbench(bench)
    sim.run()


def test_report_is_admitted_whole_or_not_at_all():
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=8, max_report_bytes=4)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    congested = []

    async def monitor(ctx):
        async for _clk, _rst, pulse in ctx.tick("usb").sample(dut.congested_report):
            if pulse:
                congested.append(1)

    async def bench(ctx):
        stream = dut.streams[0]
        ctx.set(stream.ready, 0)
        # Level 5 of 8: a 4-byte report no longer fits.
        for value in range(5):
            await _push_byte(ctx, dut, 1, 0xF0 | value, last=value == 4)
        assert not congested

        await _push_report(ctx, dut, 1, [0xD0, 0xD1, 0xD2, 0xD3])
        assert len(congested) == 1, f"expected one congestion pulse, saw {len(congested)}"

        # Drain to level 4, where a 4-byte report fits exactly.
        drained = [await _pop_stream(ctx, stream) for _ in range(5)]
        assert [value for value, _last in drained] == [0xF0, 0xF1, 0xF2, 0xF3, 0xF4]
        assert 0xD0 not in [value for value, _last in drained], "a refused report leaked bytes"

        await _push_report(ctx, dut, 1, [0xE0, 0xE1, 0xE2, 0xE3])
        admitted = [await _pop_stream(ctx, stream) for _ in range(4)]
        assert [value for value, _last in admitted] == [0xE0, 0xE1, 0xE2, 0xE3]
        assert [last for _value, last in admitted] == [False, False, False, True]
        assert len(congested) == 1

    sim.add_process(monitor)
    sim.add_testbench(bench)
    sim.run()


def test_dropped_report_does_not_leave_an_unterminated_packet():
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=8, max_report_bytes=4)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        stream = dut.streams[0]
        ctx.set(stream.ready, 0)
        await _push_report(ctx, dut, 1, [0xA0, 0xA1, 0xA2, 0xA3])
        # Level 4 of 8; the next whole 4-byte report still fits, the one after
        # it does not and must be refused entirely.
        await _push_report(ctx, dut, 1, [0xB0, 0xB1, 0xB2, 0xB3])
        await _push_report(ctx, dut, 1, [0xC0, 0xC1, 0xC2, 0xC3])

        produced = [await _pop_stream(ctx, stream) for _ in range(8)]
        assert [value for value, _last in produced] == [
            0xA0,
            0xA1,
            0xA2,
            0xA3,
            0xB0,
            0xB1,
            0xB2,
            0xB3,
        ]
        # The queue must not end mid-packet: USBStreamInEndpoint never
        # terminates a packet whose final byte carries last == 0.
        assert produced[-1][1], "queue ends on a byte with last == 0 after a congestion drop"

    sim.add_testbench(bench)
    sim.run()


def test_unmatched_and_congested_strobes_are_one_pulse_per_report():
    dut = ReportRelay(endpoint_numbers=(1,), fifo_depth=8, max_report_bytes=4)
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    unmatched = []
    congested = []

    async def monitor(ctx):
        async for _clk, _rst, un, co in ctx.tick("usb").sample(
            dut.unmatched_report, dut.congested_report
        ):
            if un:
                unmatched.append(1)
            if co:
                congested.append(1)

    async def bench(ctx):
        ctx.set(dut.streams[0].ready, 0)
        # Four bytes for an endpoint the relay does not serve: one pulse.
        await _push_report(ctx, dut, 7, [0x70, 0x71, 0x72, 0x73])
        assert len(unmatched) == 1, f"{len(unmatched)} unmatched pulses for one 4-byte report"

        # Fill so the next whole report cannot be admitted, then push one.
        for value in range(5):
            await _push_byte(ctx, dut, 1, value, last=value == 4)
        await _push_report(ctx, dut, 1, [0xD0, 0xD1, 0xD2, 0xD3])
        assert len(congested) == 1, f"{len(congested)} congestion pulses for one 4-byte report"

    sim.add_process(monitor)
    sim.add_testbench(bench)
    sim.run()


def test_fifo_depth_below_max_report_bytes_is_rejected():
    with pytest.raises(ValueError):
        ReportRelay(endpoint_numbers=(1,), fifo_depth=32, max_report_bytes=64)


def test_relay_endpoint_numbers_are_contiguous_from_one():
    # enumerator.py rejects with `ep_addr[:4] > MAX_RELAY_ENDPOINT_NUMBER`, a
    # comparison that is exact only while this tuple is contiguous from 1. A
    # non-contiguous set would leave a silently admitted hole.
    assert tuple(range(1, len(RELAY_ENDPOINT_NUMBERS) + 1)) == RELAY_ENDPOINT_NUMBERS
    assert max(RELAY_ENDPOINT_NUMBERS) == MAX_RELAY_ENDPOINT_NUMBER


def test_relay_default_endpoint_numbers_match_the_shared_constant():
    assert ReportRelay().endpoint_numbers == RELAY_ENDPOINT_NUMBERS
