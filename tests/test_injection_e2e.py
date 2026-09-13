import zlib
from dataclasses import replace

from _descriptor_store_helpers import seed_descriptor
from amaranth import Elaboratable, Module
from amaranth.sim import Simulator

from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.gateware import ReportInjectionDataPlane
from hurra_cynthion.injection_map import MapError
from hurra_cynthion.injection_wire import (
    INJ_MAP_ENTRY_FLAG_RELATIVE,
    INJ_MAP_ENTRY_FLAG_SIGNED,
    INJ_MAP_ENTRY_FLAG_X,
    INJ_MAP_STATUS_ERROR_NONE,
    INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED,
    INJ_RELATIVE_FLAG_X,
    INJ_TYPE_DESCRIPTOR_FRAGMENT,
    INJ_TYPE_MAP_BEGIN,
    INJ_TYPE_MAP_COMMIT,
    INJ_TYPE_MAP_ENTRY,
    INJ_TYPE_MAP_STATUS,
    INJ_TYPE_RELATIVE,
    INJ_TYPE_REPORT_FRAGMENT,
    INJ_TYPE_TELEMETRY_CONFIG,
    DescriptorFragmentPayload,
    MapBeginPayload,
    MapCommitPayload,
    MapEntryPayload,
    MapStatusPayload,
    RelativePayload,
    ReportFragmentPayload,
)


class IntegrationHarness(Elaboratable):
    def __init__(self) -> None:
        self.store = DescriptorStore()
        self.plane = ReportInjectionDataPlane(self.store, max_fields=8)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.store = self.store
        m.submodules.plane = self.plane
        return m


def test_data_plane_exposes_only_addressed_spi_payload_seams() -> None:
    harness = IntegrationHarness()
    plane = harness.plane

    assert not hasattr(plane, "tx_payload")
    assert not hasattr(plane, "rx_payload")
    assert len(plane.tx_payload_request) == 1
    assert len(plane.tx_payload_address) == 5
    assert len(plane.tx_payload_response) == 1
    assert len(plane.tx_payload_data) == 8
    assert len(plane.rx_payload_address) == 5
    assert len(plane.rx_payload_data) == 8


def relative_x_entry(*, descriptor_generation: int, report_length: int = 1) -> MapEntryPayload:
    return MapEntryPayload(
        descriptor_generation=descriptor_generation,
        map_generation=1,
        entry_index=0,
        interface_number=0,
        endpoint_number=1,
        report_id=0,
        usage_page=0x01,
        usage=0x30,
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=report_length,
    )


async def initialize(ctx, plane, *, link_ready: int = 1) -> None:
    ctx.set(plane.session_active, 1)
    ctx.set(plane.link_ready, link_ready)
    ctx.set(plane.tx_ready, 1)
    await ctx.tick("usb")


async def send_rx(ctx, plane, type_: int, payload: bytes, *, sequence: int = 0) -> None:
    ctx.set(plane.rx_type, type_)
    ctx.set(plane.rx_sequence, sequence)
    ctx.set(plane.rx_payload_data, 0)
    ctx.set(plane.rx_valid, 1)
    next_data = None
    for _ in range(20_000):
        if next_data is not None:
            ctx.set(plane.rx_payload_data, next_data)
        next_data = (
            payload[ctx.get(plane.rx_payload_address)]
            if ctx.get(plane.rx_payload_read_enable)
            else None
        )
        if ctx.get(plane.rx_ready):
            await ctx.tick("usb")
            ctx.set(plane.rx_valid, 0)
            return
        await ctx.tick("usb")
    raise AssertionError(f"RX type 0x{type_:02x} was not accepted")


async def commit_map(
    ctx,
    harness,
    entry: MapEntryPayload,
    *,
    sequence_start: int = 1,
) -> None:
    plane = harness.plane
    canonical = entry.to_bytes()
    metadata = {
        "descriptor_generation": entry.descriptor_generation,
        "map_generation": entry.map_generation,
        "entry_count": 1,
        "layout_count": 1,
        "flags": 0,
        "entries_crc32": zlib.crc32(canonical),
    }
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_MAP_BEGIN,
        MapBeginPayload(**metadata).to_bytes(),
        sequence=sequence_start,
    )
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_MAP_ENTRY,
        canonical,
        sequence=(sequence_start + 1) & 0xFF,
    )
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_MAP_COMMIT,
        MapCommitPayload(**metadata).to_bytes(),
        sequence=(sequence_start + 2) & 0xFF,
    )
    for _ in range(8_000):
        await ctx.tick("usb")
        if ctx.get(plane.map_store.commit_ack):
            assert ctx.get(plane.map_store.commit_error) == MapError.NONE
            assert ctx.get(plane.map_store.active_valid)
            return
    raise AssertionError("decoded map commit did not complete")


async def commit_empty_map(
    ctx,
    harness,
    *,
    descriptor_generation: int,
    map_generation: int,
    sequence_start: int = 1,
) -> None:
    """Commit a zero-entry, zero-layout map through the real SPI decode path.

    ``InjectionMapStore`` accepts this deliberately: MAP_BEGIN diverts straight to
    CLEAR_OCCUPANCY and then ACCEPT, with no MAP_ENTRY frames in between.
    """
    plane = harness.plane
    metadata = {
        "descriptor_generation": descriptor_generation,
        "map_generation": map_generation,
        "entry_count": 0,
        "layout_count": 0,
        "flags": 0,
        # zlib.crc32(b"") == 0 == CRC32_INIT ^ CRC32_XOROUT.
        "entries_crc32": zlib.crc32(b""),
    }
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_MAP_BEGIN,
        MapBeginPayload(**metadata).to_bytes(),
        sequence=sequence_start,
    )
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_MAP_COMMIT,
        MapCommitPayload(**metadata).to_bytes(),
        sequence=(sequence_start + 1) & 0xFF,
    )
    for _ in range(8_000):
        await ctx.tick("usb")
        if ctx.get(plane.map_store.commit_ack):
            assert ctx.get(plane.map_store.commit_error) == MapError.NONE
            assert ctx.get(plane.map_store.active_valid)
            assert ctx.get(plane.map_store.active_layout_count) == 0
            return
    raise AssertionError("empty map commit did not complete")


async def drive_relative(
    ctx,
    plane,
    *,
    x: int,
    sequence: int = 1,
    rx_sequence: int | None = None,
    map_generation: int = 1,
) -> None:
    payload = RelativePayload(
        lease_generation=1,
        map_generation=map_generation,
        command_sequence=sequence,
        target_frame=0,
        interface_number=0,
        endpoint_number=1,
        report_id=0,
        flags=INJ_RELATIVE_FLAG_X,
        x=x,
        y=0,
        wheel=0,
        pan=0,
        hold_reports=0,
    ).to_bytes()
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_RELATIVE,
        payload,
        sequence=sequence & 0xFF if rx_sequence is None else rx_sequence,
    )


async def push_report(
    ctx,
    plane,
    payload: bytes,
    *,
    interface: int = 0,
    endpoint: int = 1,
) -> None:
    ctx.set(plane.report_interface, interface)
    ctx.set(plane.report_endpoint, endpoint)
    for index, byte in enumerate(payload):
        ctx.set(plane.report_valid, 1)
        ctx.set(plane.report_data, byte)
        ctx.set(plane.report_first, index == 0)
        ctx.set(plane.report_last, index == len(payload) - 1)
        for _ in range(20_000):
            if ctx.get(plane.report_ready):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("native report was not accepted")
        await ctx.tick("usb")
    ctx.set(plane.report_valid, 0)
    ctx.set(plane.report_first, 0)
    ctx.set(plane.report_last, 0)


async def request_tx_byte(ctx, plane, address: int) -> int:
    ctx.set(plane.tx_payload_address, address)
    ctx.set(plane.tx_payload_request, 1)
    assert not ctx.get(plane.tx_payload_response)
    await ctx.tick("usb")
    ctx.set(plane.tx_payload_request, 0)
    for _ in range(8):
        if ctx.get(plane.tx_payload_response):
            value = ctx.get(plane.tx_payload_data)
            await ctx.tick("usb")
            assert not ctx.get(plane.tx_payload_response)
            return value
        await ctx.tick("usb")
    raise AssertionError("TX payload response did not arrive")


async def wait_for_output(ctx, plane) -> None:
    for _ in range(20_000):
        if ctx.get(plane.output_valid):
            return
        await ctx.tick("usb")
    raise AssertionError("integrated output did not become valid")


async def drain_output(ctx, plane, length: int) -> bytes:
    result = bytearray()
    ctx.set(plane.output_ready, 1)
    for _ in range(20_000):
        if ctx.get(plane.output_valid):
            assert ctx.get(plane.output_first) == (len(result) == 0)
            result.append(ctx.get(plane.output_data))
            if len(result) == length:
                assert ctx.get(plane.output_last)
                await ctx.tick("usb")
                ctx.set(plane.output_ready, 0)
                return bytes(result)
            assert not ctx.get(plane.output_last)
        await ctx.tick("usb")
    raise AssertionError("integrated output report did not complete")


def run_simulation(bench) -> None:
    harness = IntegrationHarness()
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        await bench(ctx, harness)

    simulation.add_testbench(wrapped)
    simulation.run()


def test_no_mcu_native_report_is_forwarded_byte_for_byte() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane, link_ready=0)
        native = bytes([0xA5, 0x5A, 0xC3])
        await push_report(ctx, plane, native, interface=2, endpoint=3)
        emitted = await drain_output(ctx, plane, len(native))
        assert emitted == native
        assert ctx.get(plane.output_interface) == 2
        assert ctx.get(plane.output_endpoint) == 3
        assert not ctx.get(plane.map_store.active_valid)

    run_simulation(bench)


def test_committed_map_and_relative_command_mutate_exactly_one_accepted_report() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        await drive_relative(ctx, plane, x=10, sequence=0x21)
        await push_report(ctx, plane, bytes([5]))
        emitted = await drain_output(ctx, plane, 1)
        assert emitted == bytes([15])
        ctx.set(plane.rx_valid, 0)
        assert ctx.get(plane.accepted_report_count) == 1
        assert ctx.get(plane.command_commit_count) == 1

        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([5])

    run_simulation(bench)


def test_duplicate_relative_transport_sequence_executes_exactly_once() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        await drive_relative(ctx, plane, x=10, sequence=0x21)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([15])
        await drive_relative(ctx, plane, x=10, sequence=0x21)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([5])
        assert ctx.get(plane.duplicate_rx_count) == 1
        assert ctx.get(plane.command_commit_count) == 1
        assert ctx.get(plane.last_rx_sequence) == 0x21

    run_simulation(bench)


def test_stale_relative_transport_sequence_is_rejected_without_blocking_native() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        await drive_relative(ctx, plane, x=10, sequence=0x40)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([15])
        await drive_relative(ctx, plane, x=20, sequence=0x3F)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([5])
        assert ctx.get(plane.stale_rx_count) == 1
        assert ctx.get(plane.command_commit_count) == 1
        assert ctx.get(plane.last_rx_sequence) == 0x40

    run_simulation(bench)


def test_forward_sequence_gap_is_accepted_once_and_counted() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        await drive_relative(ctx, plane, x=10, sequence=0x04)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([15])
        await drive_relative(ctx, plane, x=20, sequence=0x07)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([25])
        assert ctx.get(plane.sequence_gap_count) == 1
        assert ctx.get(plane.command_commit_count) == 2
        assert ctx.get(plane.last_rx_sequence) == 0x07

    run_simulation(bench)


def test_transport_sequence_wrap_from_ff_to_zero_is_forward() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry, sequence_start=0xFC)

        await drive_relative(ctx, plane, x=10, sequence=0xFF)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([15])
        await drive_relative(ctx, plane, x=20, sequence=0x00)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([25])
        assert ctx.get(plane.duplicate_rx_count) == 0
        assert ctx.get(plane.stale_rx_count) == 0
        assert ctx.get(plane.sequence_gap_count) == 0
        assert ctx.get(plane.last_rx_sequence) == 0

    run_simulation(bench)


def test_link_loss_resets_sequence_window_and_first_recovered_map_frame_reseeds() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)
        await drive_relative(ctx, plane, x=10, sequence=0x70)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([15])
        assert ctx.get(plane.rx_sequence_valid)

        ctx.set(plane.link_ready, 0)
        await ctx.tick("usb")
        assert not ctx.get(plane.rx_sequence_valid)
        ctx.set(plane.link_ready, 1)
        await ctx.tick("usb")

        await commit_map(ctx, harness, entry, sequence_start=0x02)
        await drive_relative(ctx, plane, x=20, sequence=0x05)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([25])
        assert ctx.get(plane.last_rx_sequence) == 0x05

    run_simulation(bench)


def test_duplicate_map_entry_transport_sequence_is_drained_without_republication() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        canonical = entry.to_bytes()
        metadata = {
            "descriptor_generation": entry.descriptor_generation,
            "map_generation": entry.map_generation,
            "entry_count": 1,
            "layout_count": 1,
            "flags": 0,
            "entries_crc32": zlib.crc32(canonical),
        }

        await send_rx(
            ctx,
            plane,
            INJ_TYPE_MAP_BEGIN,
            MapBeginPayload(**metadata).to_bytes(),
            sequence=1,
        )
        await send_rx(ctx, plane, INJ_TYPE_MAP_ENTRY, canonical, sequence=2)
        await send_rx(ctx, plane, INJ_TYPE_MAP_ENTRY, canonical, sequence=2)
        await send_rx(
            ctx,
            plane,
            INJ_TYPE_MAP_COMMIT,
            MapCommitPayload(**metadata).to_bytes(),
            sequence=3,
        )
        for _ in range(8_000):
            await ctx.tick("usb")
            if ctx.get(plane.map_store.commit_ack):
                assert ctx.get(plane.map_store.commit_error) == MapError.NONE
                assert ctx.get(plane.map_store.active_valid)
                assert ctx.get(plane.map_store.active_entry_count) == 1
                assert ctx.get(plane.duplicate_rx_count) == 1
                return
        raise AssertionError("sequence-filtered map commit did not complete")

    run_simulation(bench)


def test_sequence_classification_is_stable_while_command_stalls_and_clears_on_loss() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        # Occupy the engine's bounded command slot without supplying a report.
        await drive_relative(ctx, plane, x=10, sequence=0x04)
        stalled_payload = RelativePayload(
            lease_generation=1,
            map_generation=1,
            command_sequence=0x05,
            target_frame=0,
            interface_number=0,
            endpoint_number=1,
            report_id=0,
            flags=INJ_RELATIVE_FLAG_X,
            x=20,
            y=0,
            wheel=0,
            pan=0,
            hold_reports=0,
        ).to_bytes()
        ctx.set(plane.rx_type, INJ_TYPE_RELATIVE)
        ctx.set(plane.rx_sequence, 0x05)
        ctx.set(plane.rx_payload_data, 0)
        ctx.set(plane.rx_valid, 1)
        next_data = None
        for _ in range(20_000):
            if next_data is not None:
                ctx.set(plane.rx_payload_data, next_data)
            next_data = (
                stalled_payload[ctx.get(plane.rx_payload_address)]
                if ctx.get(plane.rx_payload_read_enable)
                else None
            )
            if ctx.get(plane.sequence_class_valid):
                break
            assert not ctx.get(plane.rx_ready)
            await ctx.tick("usb")
        else:
            raise AssertionError("stalled command was not classified")

        snapshot = (
            ctx.get(plane.sequence_class_allowed),
            ctx.get(plane.sequence_class_duplicate),
            ctx.get(plane.sequence_class_stale),
            ctx.get(plane.sequence_class_gap),
        )
        assert snapshot == (1, 0, 0, 0)
        for _ in range(8):
            await ctx.tick("usb")
            assert ctx.get(plane.sequence_class_valid)
            assert (
                ctx.get(plane.sequence_class_allowed),
                ctx.get(plane.sequence_class_duplicate),
                ctx.get(plane.sequence_class_stale),
                ctx.get(plane.sequence_class_gap),
            ) == snapshot
            assert not ctx.get(plane.rx_ready)

        ctx.set(plane.link_ready, 0)
        await ctx.tick("usb")
        assert not ctx.get(plane.sequence_class_valid)
        assert not ctx.get(plane.sequence_class_allowed)
        assert not ctx.get(plane.sequence_class_duplicate)
        assert not ctx.get(plane.sequence_class_stale)
        assert not ctx.get(plane.sequence_class_gap)
        ctx.set(plane.rx_valid, 0)

    run_simulation(bench)


def test_link_loss_clears_pending_command_and_old_map_without_blocking_native() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        await drive_relative(ctx, plane, x=100, sequence=0x22)
        await push_report(ctx, plane, bytes([100]))
        await wait_for_output(ctx, plane)
        assert not ctx.get(plane.rx_ready)
        assert ctx.get(plane.command_commit_count) == 0

        ctx.set(plane.link_ready, 0)
        await ctx.tick("usb")
        assert not ctx.get(plane.map_store.active_valid)
        ctx.set(plane.rx_valid, 0)
        assert await drain_output(ctx, plane, 1) == bytes([100])
        assert ctx.get(plane.command_commit_count) == 0
        assert ctx.get(plane.engine.pending_x) == 0

        ctx.set(plane.link_ready, 1)
        await ctx.tick("usb")
        assert not ctx.get(plane.map_store.active_valid)
        await push_report(ctx, plane, bytes([7]))
        assert await drain_output(ctx, plane, 1) == bytes([7])

    run_simulation(bench)


def test_descriptor_generation_change_invalidates_old_map_and_residual_state() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        await drive_relative(ctx, plane, x=200, sequence=0x23)
        await push_report(ctx, plane, bytes([0]))
        assert await drain_output(ctx, plane, 1) == bytes([127])
        ctx.set(plane.rx_valid, 0)
        assert ctx.get(plane.engine.pending_x) == 73

        ctx.set(harness.store.clear, 1)
        await ctx.tick("usb")
        ctx.set(harness.store.clear, 0)
        assert not ctx.get(plane.map_store.active_valid)
        ctx.set(plane.sof_tick, 1)
        await ctx.tick("usb")
        ctx.set(plane.sof_tick, 0)
        for _ in range(32):
            await ctx.tick("usb")
            assert not ctx.get(plane.output_valid)

        await push_report(ctx, plane, bytes([9]))
        assert await drain_output(ctx, plane, 1) == bytes([9])

    run_simulation(bench)


def test_endpoint_backpressure_defers_command_and_residual_commit_until_retry() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        await drive_relative(ctx, plane, x=100, sequence=0x24)
        await push_report(ctx, plane, bytes([100]))
        await wait_for_output(ctx, plane)
        held = (
            ctx.get(plane.output_data),
            ctx.get(plane.output_first),
            ctx.get(plane.output_last),
            ctx.get(plane.output_interface),
            ctx.get(plane.output_endpoint),
        )
        for _ in range(8):
            assert (
                ctx.get(plane.output_data),
                ctx.get(plane.output_first),
                ctx.get(plane.output_last),
                ctx.get(plane.output_interface),
                ctx.get(plane.output_endpoint),
            ) == held
            assert not ctx.get(plane.rx_ready)
            assert ctx.get(plane.engine.pending_x) == 0
            assert ctx.get(plane.command_commit_count) == 0
            assert ctx.get(plane.accepted_report_count) == 0
            await ctx.tick("usb")

        assert await drain_output(ctx, plane, 1) == bytes([127])
        ctx.set(plane.rx_valid, 0)
        assert ctx.get(plane.engine.pending_x) == 73
        assert ctx.get(plane.command_commit_count) == 1
        assert ctx.get(plane.accepted_report_count) == 1

    run_simulation(bench)


def test_monitoring_congestion_never_changes_authoritative_report_stream() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        ctx.set(plane.tx_ready, 0)

        for value in (0x11, 0x22, 0x33):
            await push_report(ctx, plane, bytes([value]), interface=3, endpoint=4)
            assert await drain_output(ctx, plane, 1) == bytes([value])

        assert ctx.get(plane.monitor.monitoring_drops) == 1

        await push_report(ctx, plane, bytes([0x44]), interface=2, endpoint=3)
        await wait_for_output(ctx, plane)
        held = (
            ctx.get(plane.output_valid),
            ctx.get(plane.output_data),
            ctx.get(plane.output_first),
            ctx.get(plane.output_last),
            ctx.get(plane.output_interface),
            ctx.get(plane.output_endpoint),
        )
        for tx_ready in (0, 1, 0, 1):
            ctx.set(plane.tx_ready, tx_ready)
            ctx.set(plane.output_ready, 0)
            assert ctx.get(plane.monitor.report_ready) == 0
            assert (
                ctx.get(plane.output_valid),
                ctx.get(plane.output_data),
                ctx.get(plane.output_first),
                ctx.get(plane.output_last),
                ctx.get(plane.output_interface),
                ctx.get(plane.output_endpoint),
            ) == held
            await ctx.tick("usb")

        assert await drain_output(ctx, plane, 1) == bytes([0x44])

    run_simulation(bench)


def test_tx_fill_locks_one_producer_and_only_releases_it_after_safe_queue() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        ctx.set(plane.link_ready, 1)
        ctx.set(plane.session_active, 0)
        ctx.set(plane.tx_ready, 0)
        ctx.set(plane.output_ready, 1)
        await ctx.tick("usb")

        await seed_descriptor(
            ctx,
            harness.store,
            dtype=0x22,
            index=0,
            w_index=2,
            data=bytes(range(19)),
        )
        await push_report(ctx, plane, bytes([0xA5, 0x5A]), interface=3, endpoint=4)
        for _ in range(128):
            if ctx.get(plane.tx_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("monitor producer never raised tx_valid")
        assert ctx.get(plane.tx_type) == INJ_TYPE_REPORT_FRAGMENT

        ctx.set(plane.tx_fill_start, 1)
        await ctx.tick("usb")
        ctx.set(plane.tx_fill_start, 0)
        assert not ctx.get(plane.monitor.message_ready)

        monitor_payload = bytes(
            [await request_tx_byte(ctx, plane, address) for address in range(26)]
        )
        assert (
            monitor_payload
            == ReportFragmentPayload(
                descriptor_generation=0,
                interface_number=3,
                endpoint_number=4,
                report_id=0,
                offset=0,
                total=2,
                data=b"\xa5\x5a" + bytes(15),
            ).to_bytes()
        )

        # A higher-priority descriptor becomes valid after the monitor frame
        # has started filling. It must not splice into the addressed payload.
        ctx.set(plane.session_active, 1)
        for _ in range(2_000):
            assert ctx.get(plane.tx_type) == INJ_TYPE_REPORT_FRAGMENT
            assert not ctx.get(plane.monitor.message_ready)
            assert not ctx.get(plane.descriptor_export.message_ready)
            if ctx.get(plane.descriptor_export.message_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("descriptor producer never became valid")

        for address, expected in enumerate(monitor_payload):
            assert ctx.get(plane.tx_type) == INJ_TYPE_REPORT_FRAGMENT
            assert await request_tx_byte(ctx, plane, address) == expected

        ctx.set(plane.tx_ready, 1)
        assert ctx.get(plane.monitor.message_ready)
        assert not ctx.get(plane.descriptor_export.message_ready)
        await ctx.tick("usb")
        ctx.set(plane.tx_ready, 0)
        await ctx.tick("usb")
        assert ctx.get(plane.tx_valid)
        assert ctx.get(plane.tx_type) == INJ_TYPE_DESCRIPTOR_FRAGMENT
        ctx.set(plane.tx_fill_start, 1)
        await ctx.tick("usb")
        ctx.set(plane.tx_fill_start, 0)
        descriptor_payload = bytes(
            [await request_tx_byte(ctx, plane, address) for address in range(26)]
        )
        assert (
            descriptor_payload
            == DescriptorFragmentPayload(
                descriptor_generation=0,
                interface_number=2,
                offset=0,
                total=19,
                data=bytes(range(18)),
            ).to_bytes()
        )

    run_simulation(bench)


def test_map_status_addressed_payload_is_exact_and_zero_filled() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        for _ in range(128):
            if ctx.get(plane.tx_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("MAP_STATUS never became valid")
        assert ctx.get(plane.tx_type) == INJ_TYPE_MAP_STATUS

        ctx.set(plane.tx_ready, 0)
        ctx.set(plane.tx_fill_start, 1)
        await ctx.tick("usb")
        ctx.set(plane.tx_fill_start, 0)
        payload = bytes([await request_tx_byte(ctx, plane, address) for address in range(26)])
        expected = MapStatusPayload(
            descriptor_generation=entry.descriptor_generation,
            map_generation=entry.map_generation,
            active_map_generation=entry.map_generation,
            entry_index=0xFF,
            status=INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED,
            error=INJ_MAP_STATUS_ERROR_NONE,
            flags=0,
            entries_crc32=zlib.crc32(entry.to_bytes()),
        ).to_bytes()
        assert payload == expected
        assert payload[14:] == bytes(12)

    run_simulation(bench)


def test_link_loss_cancels_pending_descriptor_response_before_retry() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        ctx.set(plane.link_ready, 1)
        ctx.set(plane.session_active, 0)
        ctx.set(plane.tx_ready, 0)
        await ctx.tick("usb")
        descriptor = bytes(range(19))
        await seed_descriptor(
            ctx,
            harness.store,
            dtype=0x22,
            index=0,
            w_index=2,
            data=descriptor,
        )
        ctx.set(plane.session_active, 1)
        await ctx.tick("usb")
        for _ in range(2_000):
            if ctx.get(plane.tx_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("descriptor producer never became valid")

        ctx.set(plane.tx_fill_start, 1)
        await ctx.tick("usb")
        ctx.set(plane.tx_fill_start, 0)
        ctx.set(plane.tx_payload_address, 8)
        ctx.set(plane.tx_payload_request, 1)
        await ctx.tick("usb")
        ctx.set(plane.tx_payload_request, 0)
        assert not ctx.get(plane.tx_payload_response)

        ctx.set(plane.link_ready, 0)
        await ctx.tick("usb")
        assert not ctx.get(plane.tx_valid)
        assert not ctx.get(plane.tx_payload_response)
        for _ in range(4):
            await ctx.tick("usb")
            assert not ctx.get(plane.tx_payload_response)

        ctx.set(plane.link_ready, 1)
        for _ in range(2_000):
            if ctx.get(plane.tx_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("descriptor producer did not recover after link loss")
        assert ctx.get(plane.tx_type) == INJ_TYPE_DESCRIPTOR_FRAGMENT
        ctx.set(plane.tx_fill_start, 1)
        await ctx.tick("usb")
        ctx.set(plane.tx_fill_start, 0)
        payload = bytes([await request_tx_byte(ctx, plane, address) for address in range(26)])
        assert (
            payload
            == DescriptorFragmentPayload(
                descriptor_generation=0,
                interface_number=2,
                offset=0,
                total=len(descriptor),
                data=descriptor[:18],
            ).to_bytes()
        )

    run_simulation(bench)


def test_link_loss_aborts_partial_rx_staging_before_decode_and_keeps_native_fail_open() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        payload = RelativePayload(
            lease_generation=1,
            map_generation=1,
            command_sequence=0x7777,
            target_frame=0,
            interface_number=0,
            endpoint_number=1,
            report_id=0,
            flags=INJ_RELATIVE_FLAG_X,
            x=-10,
            y=0,
            wheel=0,
            pan=0,
            hold_reports=0,
        ).to_bytes()
        ctx.set(plane.rx_type, INJ_TYPE_RELATIVE)
        ctx.set(plane.rx_sequence, 0x77)
        ctx.set(plane.rx_valid, 1)
        next_data = None
        reads = 0
        for _ in range(128):
            if next_data is not None:
                ctx.set(plane.rx_payload_data, next_data)
            if ctx.get(plane.rx_payload_read_enable):
                next_data = payload[ctx.get(plane.rx_payload_address)]
                reads += 1
            if reads >= 10:
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("partial RX staging never started")
        assert reads < 26
        assert not ctx.get(plane.rx_ready)

        ctx.set(plane.link_ready, 0)
        assert ctx.get(plane.rx_ready)
        await ctx.tick("usb")
        ctx.set(plane.rx_valid, 0)
        for _ in range(4):
            await ctx.tick("usb")

        assert not ctx.get(plane.map_store.active_valid)
        assert not ctx.get(plane.engine.relative_valid)
        assert ctx.get(plane.engine.pending_x) == 0
        assert ctx.get(plane.command_commit_count) == 0
        assert ctx.get(plane.last_rx_sequence) != 0x77

        await push_report(ctx, plane, bytes([0x35]))
        assert await drain_output(ctx, plane, 1) == bytes([0x35])

    run_simulation(bench)


def test_exact_byte_staging_preserves_signed_relative_and_map_entry_fields() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry)

        assert ctx.get(plane.map_store.active_valid)
        assert ctx.get(plane.map_store.active_generation) == entry.map_generation
        await drive_relative(ctx, plane, x=-10, sequence=0x31)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([0xFB])
        assert ctx.get(plane.command_commit_count) == 1

    run_simulation(bench)


def test_empty_map_commit_keeps_native_report_relay_alive() -> None:
    # Reachability through the real SPI decode path: a MAP_BEGIN/MAP_COMMIT pair
    # with no MAP_ENTRY frames in between is accepted with MapError.NONE. If the
    # stationary scan then fails to terminate, report_ready stays low and the
    # passthrough stops relaying entirely.
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        descriptor_generation = ctx.get(harness.store.descriptor_generation)
        entry = relative_x_entry(descriptor_generation=descriptor_generation)
        await commit_map(ctx, harness, entry)

        # Leave a valid state record behind, so the scan has a slot to walk.
        await drive_relative(ctx, plane, x=10, sequence=0x21)
        await push_report(ctx, plane, bytes([5]))
        assert await drain_output(ctx, plane, 1) == bytes([15])
        ctx.set(plane.rx_valid, 0)

        await commit_empty_map(
            ctx,
            harness,
            descriptor_generation=descriptor_generation,
            map_generation=entry.map_generation + 1,
            sequence_start=0x30,
        )

        ctx.set(plane.sof_tick, 1)
        await ctx.tick("usb")
        ctx.set(plane.sof_tick, 0)

        for index in range(3):
            native = bytes([0x40 + index])
            await push_report(ctx, plane, native)
            assert await drain_output(ctx, plane, 1) == native

    run_simulation(bench)


async def send_in_window_rejected(
    ctx,
    plane,
    *,
    rx_sequence: int,
    map_generation: int = 9,
    command_sequence: int = 0x11,
) -> dict[str, int]:
    """Send a frame that is in window but whose content is rejected.

    A map generation the FPGA never activated makes ``command_fresh`` false, so
    the frame is drained as ``invalid_rx`` without executing. Returns the
    classification sampled on the acceptance cycle, then advances one cycle so
    the registered window and counter updates have landed on return.
    """
    await drive_relative(
        ctx,
        plane,
        x=10,
        sequence=command_sequence,
        rx_sequence=rx_sequence,
        map_generation=map_generation,
    )
    ctx.set(plane.rx_valid, 0)
    classification = {
        "allowed": ctx.get(plane.sequence_class_allowed),
        "duplicate": ctx.get(plane.sequence_class_duplicate),
        "stale": ctx.get(plane.sequence_class_stale),
        "gap": ctx.get(plane.sequence_class_gap),
    }
    await ctx.tick("usb")
    return classification


def test_in_window_rejected_frames_advance_the_sequence_window() -> None:
    # A frame whose content is rejected still consumed a transport slot, so the
    # window must move past it. "Act on the payload" and "advance the window"
    # are distinct predicates; the field is named last_rx_sequence.
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry, sequence_start=0x0D)
        assert ctx.get(plane.last_rx_sequence) == 0x0F

        # delta 3 from 0x0F, so this is also a counted forward gap.
        classification = await send_in_window_rejected(ctx, plane, rx_sequence=0x12)

        assert classification == {"allowed": 1, "duplicate": 0, "stale": 0, "gap": 1}
        assert ctx.get(plane.last_rx_sequence) == 0x12
        assert ctx.get(plane.invalid_rx_count) == 1
        assert ctx.get(plane.sequence_gap_count) == 1
        assert ctx.get(plane.command_commit_count) == 0

    run_simulation(bench)


def test_long_burst_of_rejected_frames_never_locks_out_the_sequence_window() -> None:
    # The regression test. Previously the window froze at the last *accepted*
    # frame while the sender kept incrementing, so frame #128 reached delta 0x80
    # and every frame after it - including recovery frames - classified STALE.
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry, sequence_start=0x01)
        sequence = ctx.get(plane.last_rx_sequence)

        for index in range(200):
            sequence = (sequence + 1) & 0xFF
            classification = await send_in_window_rejected(ctx, plane, rx_sequence=sequence)
            assert classification["allowed"] == 1, f"locked out at frame {index}"
            assert classification["stale"] == 0, f"stale at frame {index}"
            assert ctx.get(plane.last_rx_sequence) == sequence, f"window froze at frame {index}"

        assert ctx.get(plane.stale_rx_count) == 0
        assert ctx.get(plane.invalid_rx_count) == 200
        assert ctx.get(plane.command_commit_count) == 0

        # And the link is still usable afterwards: a full map upload still commits.
        recovery = replace(
            relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation)),
            map_generation=2,
        )
        await commit_map(ctx, harness, recovery, sequence_start=(sequence + 1) & 0xFF)

    run_simulation(bench)


def test_unsupported_message_type_advances_window_without_action() -> None:
    # TELEMETRY_CONFIG is legal on the wire (design spec 8.2) but absent from
    # supported_rx, so it is drained as invalid. It must still move the window.
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry, sequence_start=0x0D)
        before = ctx.get(plane.invalid_rx_count)

        await send_rx(ctx, plane, INJ_TYPE_TELEMETRY_CONFIG, bytes(26), sequence=0x10)
        ctx.set(plane.rx_valid, 0)
        await ctx.tick("usb")

        assert ctx.get(plane.invalid_rx_count) == before + 1
        assert ctx.get(plane.map_store.busy) == 0
        assert ctx.get(plane.last_rx_sequence) == 0x10

    run_simulation(bench)


def test_stale_and_duplicate_sequences_cannot_move_the_window_backwards() -> None:
    # Boundary-delta coverage for the gateware classifier, standing in for the
    # third leg of the cross-language equivalence testing in Task 4.
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        entry = relative_x_entry(descriptor_generation=ctx.get(harness.store.descriptor_generation))
        await commit_map(ctx, harness, entry, sequence_start=0x0D)

        # Advance the window using a frame whose content is rejected.
        await send_in_window_rejected(ctx, plane, rx_sequence=0x10)
        assert ctx.get(plane.last_rx_sequence) == 0x10

        # delta 0xFF and delta 0x81: both stale, neither may pull the window back.
        for rx_sequence in (0x0F, 0x91):
            stale_before = ctx.get(plane.stale_rx_count)
            classification = await send_in_window_rejected(ctx, plane, rx_sequence=rx_sequence)
            assert classification["stale"] == 1, hex(rx_sequence)
            assert classification["allowed"] == 0, hex(rx_sequence)
            assert ctx.get(plane.stale_rx_count) == stale_before + 1, hex(rx_sequence)
            assert ctx.get(plane.last_rx_sequence) == 0x10, hex(rx_sequence)

        # delta 0x00 is a duplicate: not executed and window unmoved, even though
        # this replay carries a now-valid command.
        duplicate_before = ctx.get(plane.duplicate_rx_count)
        await drive_relative(ctx, plane, x=10, sequence=0x11, rx_sequence=0x10)
        ctx.set(plane.rx_valid, 0)
        assert ctx.get(plane.sequence_class_duplicate) == 1
        await ctx.tick("usb")
        assert ctx.get(plane.duplicate_rx_count) == duplicate_before + 1
        assert ctx.get(plane.command_commit_count) == 0
        assert ctx.get(plane.last_rx_sequence) == 0x10

    run_simulation(bench)
