import importlib
import math

from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.injection_wire import INJ_TYPE_REPORT_FRAGMENT, ReportFragmentPayload


def _report_monitor_type():
    try:
        module = importlib.import_module("hurra_cynthion.report_monitor")
    except ModuleNotFoundError:
        return None
    return getattr(module, "ReportMonitor", None)


async def _push_report(
    ctx,
    dut,
    payload: bytes,
    *,
    descriptor_generation: int,
    interface: int,
    endpoint: int,
    report_id: int,
    stall_each_byte: bool = False,
) -> bytes:
    ctx.set(dut.descriptor_generation, descriptor_generation)
    ctx.set(dut.report_interface, interface)
    ctx.set(dut.report_endpoint, endpoint)
    ctx.set(dut.report_id, report_id)
    forwarded = bytearray()

    for index, byte in enumerate(payload):
        ctx.set(dut.report_valid, 1)
        ctx.set(dut.report_data, byte)
        ctx.set(dut.report_first, index == 0)
        ctx.set(dut.report_last, index == len(payload) - 1)

        if stall_each_byte:
            ctx.set(dut.output_ready, 0)
            assert not ctx.get(dut.report_ready)
            assert ctx.get(dut.output_valid)
            assert ctx.get(dut.output_data) == byte
            assert ctx.get(dut.output_first) == (index == 0)
            assert ctx.get(dut.output_last) == (index == len(payload) - 1)
            await ctx.tick("usb")

        ctx.set(dut.output_ready, 1)
        assert ctx.get(dut.report_ready)
        assert ctx.get(dut.output_valid) == ctx.get(dut.report_valid)
        assert ctx.get(dut.output_data) == byte
        assert ctx.get(dut.output_first) == (index == 0)
        assert ctx.get(dut.output_last) == (index == len(payload) - 1)
        assert ctx.get(dut.output_interface) == interface
        assert ctx.get(dut.output_endpoint) == endpoint
        forwarded.append(ctx.get(dut.output_data))
        await ctx.tick("usb")

    ctx.set(dut.report_valid, 0)
    ctx.set(dut.report_first, 0)
    ctx.set(dut.report_last, 0)
    await ctx.tick("usb")
    return bytes(forwarded)


async def _drain_fragments(ctx, dut, count: int) -> list[ReportFragmentPayload]:
    fragments = []
    ctx.set(dut.message_ready, 0)
    for _ in range(1000):
        if ctx.get(dut.message_valid):
            assert ctx.get(dut.message_type) == INJ_TYPE_REPORT_FRAGMENT
            payload = await _read_message_payload(ctx, dut)
            fragments.append(ReportFragmentPayload.from_bytes(payload))
            ctx.set(dut.message_ready, 1)
            await ctx.tick("usb")
            ctx.set(dut.message_ready, 0)
        else:
            await ctx.tick("usb")
        if len(fragments) == count:
            break
    else:
        raise AssertionError("report fragments did not drain within their bounded budget")
    ctx.set(dut.message_ready, 0)
    return fragments


async def _read_message_payload(ctx, dut) -> bytes:
    payload = bytearray()
    for address in range(26):
        assert ctx.get(dut.message_valid)
        ctx.set(dut.message_payload_address, address)
        ctx.set(dut.message_payload_request, 1)
        assert not ctx.get(dut.message_payload_response)
        await ctx.tick("usb")
        assert ctx.get(dut.message_payload_response)
        payload.append(ctx.get(dut.message_payload_data))
        ctx.set(dut.message_payload_request, 0)
        await ctx.tick("usb")
        assert not ctx.get(dut.message_payload_response)
    return bytes(payload)


def test_accepted_reports_fragment_at_17_bytes_without_changing_native_timing() -> None:
    monitor_type = _report_monitor_type()
    assert monitor_type is not None, "ReportMonitor is not implemented"

    dut = monitor_type(depth=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    reports = [
        bytes((length + index) & 0xFF for index in range(length)) for length in (1, 19, 20, 64)
    ]

    async def bench(ctx):
        for report_index, report in enumerate(reports):
            generation = 0x1200 + report_index
            interface = report_index
            endpoint = report_index + 1
            report_id = 0x40 + report_index
            forwarded = await _push_report(
                ctx,
                dut,
                report,
                descriptor_generation=generation,
                interface=interface,
                endpoint=endpoint,
                report_id=report_id,
                stall_each_byte=True,
            )
            assert forwarded == report

            fragments = await _drain_fragments(ctx, dut, math.ceil(len(report) / 17))
            rebuilt = bytearray()
            for fragment_index, fragment in enumerate(fragments):
                expected_offset = fragment_index * 17
                useful = min(17, len(report) - expected_offset)
                assert fragment.descriptor_generation == generation
                assert fragment.interface_number == interface
                assert fragment.endpoint_number == endpoint
                assert fragment.report_id == report_id
                assert fragment.offset == expected_offset
                assert fragment.total == len(report)
                rebuilt.extend(fragment.data[:useful])
                assert fragment.data[useful:] == bytes(17 - useful)
            assert bytes(rebuilt) == report

    simulation.add_testbench(bench)
    simulation.run()


def test_unaccepted_or_incomplete_native_reports_are_not_monitored() -> None:
    monitor_type = _report_monitor_type()
    assert monitor_type is not None, "ReportMonitor is not implemented"

    dut = monitor_type(depth=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        # A valid byte that the relay does not accept is not a report transfer.
        ctx.set(dut.output_ready, 0)
        ctx.set(dut.report_valid, 1)
        ctx.set(dut.report_first, 1)
        ctx.set(dut.report_last, 1)
        ctx.set(dut.report_data, 0xA5)
        await ctx.tick("usb")
        ctx.set(dut.report_valid, 0)
        assert not ctx.get(dut.message_valid)

        # An accepted first byte without an accepted last byte is incomplete.
        ctx.set(dut.output_ready, 1)
        ctx.set(dut.report_valid, 1)
        ctx.set(dut.report_first, 1)
        ctx.set(dut.report_last, 0)
        await ctx.tick("usb")
        ctx.set(dut.report_valid, 0)
        for _ in range(4):
            await ctx.tick("usb")
            assert not ctx.get(dut.message_valid)

    simulation.add_testbench(bench)
    simulation.run()


def test_full_two_report_queue_drops_only_monitoring_copy_and_counter_saturates() -> None:
    monitor_type = _report_monitor_type()
    assert monitor_type is not None, "ReportMonitor is not implemented"

    # A narrow counter makes saturation practical to prove in simulation. The
    # production default remains the frozen 32-bit telemetry width.
    dut = monitor_type(depth=2, drop_counter_bits=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    queued = (b"\x01\x02\x03", b"\x11\x12")

    async def bench(ctx):
        ctx.set(dut.message_ready, 0)
        for index, report in enumerate(queued):
            assert (
                await _push_report(
                    ctx,
                    dut,
                    report,
                    descriptor_generation=7,
                    interface=index,
                    endpoint=index + 1,
                    report_id=index,
                )
                == report
            )

        # The queue is full, but every later report still reaches the relay.
        for drop_index in range(5):
            report = bytes((0x80 + drop_index, 0x90 + drop_index))
            assert (
                await _push_report(
                    ctx,
                    dut,
                    report,
                    descriptor_generation=7,
                    interface=3,
                    endpoint=4,
                    report_id=9,
                )
                == report
            )
        assert ctx.get(dut.monitoring_drops) == 3

        fragments = await _drain_fragments(ctx, dut, count=2)
        assert [fragment.data[: fragment.total] for fragment in fragments] == list(queued)

    simulation.add_testbench(bench)
    simulation.run()


def test_fragment_bytes_are_addressed_one_cycle_and_stable_when_stalled() -> None:
    monitor_type = _report_monitor_type()
    assert monitor_type is not None, "ReportMonitor is not implemented"

    dut = monitor_type(depth=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")
    report = bytes(range(34))

    async def bench(ctx):
        ctx.set(dut.message_ready, 0)
        await _push_report(
            ctx,
            dut,
            report,
            descriptor_generation=0x3344,
            interface=2,
            endpoint=3,
            report_id=4,
        )

        for _ in range(8):
            if ctx.get(dut.message_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("first fragment did not become valid")

        first_payload = await _read_message_payload(ctx, dut)
        for _ in range(8):
            assert ctx.get(dut.message_valid)
            assert ctx.get(dut.message_type) == INJ_TYPE_REPORT_FRAGMENT
            await ctx.tick("usb")

        assert await _read_message_payload(ctx, dut) == first_payload
        first = ReportFragmentPayload.from_bytes(first_payload)
        assert first.offset == 0
        assert first.total == 34
        assert first.data == report[:17]

        ctx.set(dut.message_ready, 1)
        await ctx.tick("usb")
        ctx.set(dut.message_ready, 0)

        for _ in range(8):
            if ctx.get(dut.message_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("second fragment did not become valid")
        second = ReportFragmentPayload.from_bytes(await _read_message_payload(ctx, dut))
        assert second.offset == 17
        assert second.total == 34
        assert second.data == report[17:]

    simulation.add_testbench(bench)
    simulation.run()


def test_queue_payload_is_one_bounded_synchronous_memory() -> None:
    """One bounded memory primitive for the payload, never per-slot registers.

    Held in LUTRAM: 8x128 is 1,024 bits, which occupied 6% of a whole EBR.
    The ``capture_data_0``/``queue_0_data_0`` guards below are the real
    invariant -- the payload must not decay into an unrolled register array.
    """
    monitor_type = _report_monitor_type()
    assert monitor_type is not None, "ReportMonitor is not implemented"

    dut = monitor_type(depth=2)
    assert not hasattr(dut, "message_payload")
    assert len(dut.message_payload_request) == 1
    assert len(dut.message_payload_address) == 5
    assert len(dut.message_payload_response) == 1
    assert len(dut.message_payload_data) == 8
    assert len(dut.message_payload_cancel) == 1
    converted = rtlil.convert(
        dut,
        ports=[
            dut.report_valid,
            dut.report_ready,
            dut.output_valid,
            dut.output_ready,
            dut.message_valid,
            dut.message_ready,
            dut.message_payload_request,
            dut.message_payload_address,
            dut.message_payload_response,
            dut.message_payload_data,
            dut.message_payload_cancel,
        ],
    )

    assert converted.count("memory width 8 size 128") == 1
    assert converted.count('attribute \\ram_style "distributed"') == 1
    assert "capture_data_0" not in converted
    assert "queue_0_data_0" not in converted
    assert "fragment_data_0" not in converted


def test_full_queue_final_fragment_dequeue_and_one_byte_enqueue_preserves_order() -> None:
    monitor_type = _report_monitor_type()
    assert monitor_type is not None, "ReportMonitor is not implemented"

    dut = monitor_type(depth=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        ctx.set(dut.message_ready, 0)
        await _push_report(
            ctx,
            dut,
            b"\x11",
            descriptor_generation=1,
            interface=1,
            endpoint=2,
            report_id=3,
        )
        await _push_report(
            ctx,
            dut,
            b"\x18",
            descriptor_generation=7,
            interface=7,
            endpoint=8,
            report_id=9,
        )
        for _ in range(8):
            if ctx.get(dut.message_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("first fragment did not become valid")

        first = ReportFragmentPayload.from_bytes(await _read_message_payload(ctx, dut))
        assert first.data[: first.total] == b"\x11"

        # The queue is full. Accept the final fragment on the same edge as a
        # new complete native report. The freed slot is writable immediately,
        # occupancy stays constant, and FIFO order remains old-before-new.
        ctx.set(dut.message_ready, 1)
        ctx.set(dut.descriptor_generation, 2)
        ctx.set(dut.report_interface, 4)
        ctx.set(dut.report_endpoint, 5)
        ctx.set(dut.report_id, 6)
        ctx.set(dut.report_valid, 1)
        ctx.set(dut.report_data, 0x22)
        ctx.set(dut.report_first, 1)
        ctx.set(dut.report_last, 1)
        ctx.set(dut.output_ready, 1)
        assert ctx.get(dut.report_ready)
        assert ctx.get(dut.message_valid)
        await ctx.tick("usb")

        ctx.set(dut.message_ready, 0)
        ctx.set(dut.report_valid, 0)
        ctx.set(dut.report_first, 0)
        ctx.set(dut.report_last, 0)
        assert ctx.get(dut.message_valid)

        fragments = await _drain_fragments(ctx, dut, count=2)
        assert fragments[0].descriptor_generation == 7
        assert fragments[0].interface_number == 7
        assert fragments[0].endpoint_number == 8
        assert fragments[0].report_id == 9
        assert fragments[0].data[: fragments[0].total] == b"\x18"
        assert fragments[1].descriptor_generation == 2
        assert fragments[1].interface_number == 4
        assert fragments[1].endpoint_number == 5
        assert fragments[1].report_id == 6
        assert fragments[1].data[: fragments[1].total] == b"\x22"

    simulation.add_testbench(bench)
    simulation.run()


def test_full_at_first_multi_byte_report_stays_dropped_after_later_dequeue() -> None:
    monitor_type = _report_monitor_type()
    assert monitor_type is not None, "ReportMonitor is not implemented"

    dut = monitor_type(depth=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        ctx.set(dut.message_ready, 0)
        await _push_report(
            ctx,
            dut,
            b"\x11",
            descriptor_generation=1,
            interface=1,
            endpoint=2,
            report_id=3,
        )
        retained = b"\x21\x22"
        await _push_report(
            ctx,
            dut,
            retained,
            descriptor_generation=4,
            interface=5,
            endpoint=6,
            report_id=7,
        )
        for _ in range(8):
            if ctx.get(dut.message_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("first fragment did not become valid")

        dropped = bytes(range(0x80, 0x94))
        ctx.set(dut.descriptor_generation, 8)
        ctx.set(dut.report_interface, 9)
        ctx.set(dut.report_endpoint, 10)
        ctx.set(dut.report_id, 11)
        ctx.set(dut.output_ready, 1)
        ctx.set(dut.report_valid, 1)

        for index, byte in enumerate(dropped):
            ctx.set(dut.report_data, byte)
            ctx.set(dut.report_first, index == 0)
            ctx.set(dut.report_last, index == len(dropped) - 1)
            # The first byte sees a full queue. Freeing a slot on the following
            # byte must not admit the rest of this already-dropped report.
            ctx.set(dut.message_ready, index == 1)
            assert ctx.get(dut.report_ready)
            await ctx.tick("usb")

        ctx.set(dut.report_valid, 0)
        ctx.set(dut.report_first, 0)
        ctx.set(dut.report_last, 0)
        ctx.set(dut.message_ready, 0)
        await ctx.tick("usb")
        assert ctx.get(dut.monitoring_drops) == 1

        direct = bytes(range(0xC0, 0xD7))
        await _push_report(
            ctx,
            dut,
            direct,
            descriptor_generation=0x1234,
            interface=12,
            endpoint=13,
            report_id=14,
        )

        fragments = await _drain_fragments(ctx, dut, count=3)
        assert fragments[0].data[: fragments[0].total] == retained
        assert fragments[1].descriptor_generation == 0x1234
        assert fragments[1].offset == 0
        assert fragments[1].total == len(direct)
        assert fragments[1].data == direct[:17]
        assert fragments[2].descriptor_generation == 0x1234
        assert fragments[2].offset == 17
        assert fragments[2].total == len(direct)
        assert fragments[2].data[: len(direct) - 17] == direct[17:]
        assert fragments[2].data[len(direct) - 17 :] == bytes(34 - len(direct))

    simulation.add_testbench(bench)
    simulation.run()
