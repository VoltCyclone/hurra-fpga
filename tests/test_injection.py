import random
import zlib

from amaranth import Elaboratable, Module
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.injection import ReportInjectionEngine
from hurra_cynthion.injection_map import InjectionMapStore, MapError
from hurra_cynthion.injection_wire import (
    INJ_CLEAR_FLAG_MOTION,
    INJ_MAP_ENTRY_FLAG_BUTTON,
    INJ_MAP_ENTRY_FLAG_RELATIVE,
    INJ_MAP_ENTRY_FLAG_SIGNED,
    INJ_MAP_ENTRY_FLAG_X,
    INJ_MAP_ENTRY_FLAG_Y,
    INJ_RELATIVE_FLAG_X,
    INJ_RELATIVE_FLAG_Y,
    MapEntryPayload,
)

CASES = [
    # (offset, width, logical_min, logical_max, physical, injected, emitted, residual)
    (8, 12, -2048, 2047, 1000, 1500, 2047, 453),
    (3, 5, -16, 15, -10, -10, -16, -4),
    (17, 16, -32768, 32767, 0, -32768, -32768, 0),
]


class InjectionHarness(Elaboratable):
    def __init__(self, *, max_fields: int = 8) -> None:
        self.map_store = InjectionMapStore(max_fields=max_fields)
        self.engine = ReportInjectionEngine(self.map_store)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.map_store = self.map_store
        m.submodules.engine = self.engine
        return m


def map_entry(
    *,
    bit_offset: int,
    bit_width: int,
    flags: int,
    logical_minimum: int,
    logical_maximum: int,
    report_length: int,
    usage: int = 0x30,
    report_id: int = 0,
    entry_index: int = 0,
    generation: int = 1,
) -> MapEntryPayload:
    return MapEntryPayload(
        descriptor_generation=1,
        map_generation=generation,
        entry_index=entry_index,
        interface_number=0,
        endpoint_number=1,
        report_id=report_id,
        usage_page=0x09 if flags & INJ_MAP_ENTRY_FLAG_BUTTON else 0x01,
        usage=usage,
        bit_offset=bit_offset,
        bit_width=bit_width,
        flags=flags,
        logical_minimum=logical_minimum,
        logical_maximum=logical_maximum,
        report_length=report_length,
    )


async def commit_map(ctx, store, entries: list[MapEntryPayload], *, generation: int = 1) -> None:
    canonical = b"".join(entry.to_bytes() for entry in entries)
    layouts = {
        (entry.interface_number, entry.endpoint_number, entry.report_id) for entry in entries
    }
    ctx.set(store.descriptor_generation, 1)
    ctx.set(store.candidate_descriptor_generation, 1)
    ctx.set(store.candidate_map_generation, generation)
    ctx.set(store.candidate_entry_count, len(entries))
    ctx.set(store.candidate_layout_count, len(layouts))
    ctx.set(store.candidate_entries_crc32, zlib.crc32(canonical))
    ctx.set(store.begin, 1)
    await ctx.tick("usb")
    ctx.set(store.begin, 0)

    for entry in entries:
        ctx.set(store.entry.as_value(), int.from_bytes(entry.to_bytes(), "little"))
        ctx.set(store.entry_valid, 1)
        await ctx.tick("usb")
    ctx.set(store.entry_valid, 0)
    ctx.set(store.commit, 1)
    await ctx.tick("usb")
    ctx.set(store.commit, 0)

    for _ in range(8_000):
        await ctx.tick("usb")
        if ctx.get(store.commit_ack):
            assert ctx.get(store.commit_error) == MapError.NONE
            return
    raise AssertionError("map commit did not complete")


async def push_report(
    ctx,
    engine,
    payload: bytes,
    *,
    interface: int = 0,
    endpoint: int = 1,
) -> None:
    ctx.set(engine.report_interface, interface)
    ctx.set(engine.report_endpoint, endpoint)
    for index, byte in enumerate(payload):
        ctx.set(engine.report_valid, 1)
        ctx.set(engine.report_data, byte)
        ctx.set(engine.report_first, index == 0)
        ctx.set(engine.report_last, index == len(payload) - 1)
        for _ in range(2_000):
            if ctx.get(engine.report_ready):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("input report was not accepted")
        await ctx.tick("usb")
    ctx.set(engine.report_valid, 0)
    ctx.set(engine.report_first, 0)
    ctx.set(engine.report_last, 0)


async def drain_report(ctx, engine, length: int) -> tuple[bytes, set[str]]:
    result = bytearray()
    acknowledgements = set()
    ctx.set(engine.output_ready, 1)
    for _ in range(20_000):
        if ctx.get(engine.output_valid):
            if not result:
                assert ctx.get(engine.output_first)
            else:
                assert not ctx.get(engine.output_first)
            result.append(ctx.get(engine.output_data))
            if ctx.get(engine.relative_ready):
                acknowledgements.add("relative")
            if ctx.get(engine.button_ready):
                acknowledgements.add("button")
            if ctx.get(engine.mask_ready):
                acknowledgements.add("mask")
            if ctx.get(engine.clear_ready):
                acknowledgements.add("clear")
            if len(result) == length:
                assert ctx.get(engine.output_last)
                await ctx.tick("usb")
                ctx.set(engine.output_ready, 0)
                return bytes(result), acknowledgements
            assert not ctx.get(engine.output_last)
        await ctx.tick("usb")
    raise AssertionError("output report did not complete")


async def pulse_sof(ctx, engine) -> None:
    ctx.set(engine.sof_tick, 1)
    await ctx.tick("usb")
    ctx.set(engine.sof_tick, 0)


async def wait_for_output(ctx, engine) -> None:
    for _ in range(20_000):
        if ctx.get(engine.output_valid):
            return
        await ctx.tick("usb")
    raise AssertionError("output report did not become available")


def insert_bits(payload: bytearray, offset: int, width: int, value: int) -> None:
    encoded = value & ((1 << width) - 1)
    for bit_index in range(width):
        absolute_bit = offset + bit_index
        mask = 1 << (absolute_bit & 7)
        if encoded & (1 << bit_index):
            payload[absolute_bit >> 3] |= mask
        else:
            payload[absolute_bit >> 3] &= ~mask


def extract_signed(payload: bytes, offset: int, width: int) -> int:
    value = 0
    for bit_index in range(width):
        absolute_bit = offset + bit_index
        value |= ((payload[absolute_bit >> 3] >> (absolute_bit & 7)) & 1) << bit_index
    sign = 1 << (width - 1)
    return (value ^ sign) - sign


def field_mask(length: int, offset: int, width: int) -> bytes:
    mask = bytearray(length)
    insert_bits(mask, offset, width, (1 << width) - 1)
    return bytes(mask)


def test_injection_disabled_preserves_every_report_length_byte_for_byte() -> None:
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        random_source = random.Random(0xC17A10)
        for length in range(1, 65):
            payload = random_source.randbytes(length)
            await push_report(ctx, engine, payload, interface=length & 3, endpoint=1)
            emitted, acknowledgements = await drain_report(ctx, engine, length)
            assert emitted == payload
            assert not acknowledgements

    simulation.add_testbench(bench)
    simulation.run()


def test_sixty_four_byte_capture_and_output_are_contiguous_and_stall_stable() -> None:
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")
    payload = bytes(range(64))

    async def bench(ctx) -> None:
        ctx.set(engine.report_interface, 3)
        ctx.set(engine.report_endpoint, 1)
        for index, byte in enumerate(payload):
            ctx.set(engine.report_valid, 1)
            ctx.set(engine.report_data, byte)
            ctx.set(engine.report_first, index == 0)
            ctx.set(engine.report_last, index == len(payload) - 1)
            assert ctx.get(engine.report_ready)
            await ctx.tick("usb")
        ctx.set(engine.report_valid, 0)
        ctx.set(engine.report_first, 0)
        ctx.set(engine.report_last, 0)

        await wait_for_output(ctx, engine)
        for index, byte in enumerate(payload):
            assert ctx.get(engine.output_valid)
            assert ctx.get(engine.output_data) == byte
            assert ctx.get(engine.output_first) == (index == 0)
            assert ctx.get(engine.output_last) == (index == len(payload) - 1)

            if index in {0, 15, 16, 31, 32, 62, 63}:
                held = (
                    ctx.get(engine.output_data),
                    ctx.get(engine.output_first),
                    ctx.get(engine.output_last),
                    ctx.get(engine.output_interface),
                    ctx.get(engine.output_endpoint),
                )
                for _ in range(3):
                    assert ctx.get(engine.output_valid)
                    assert (
                        ctx.get(engine.output_data),
                        ctx.get(engine.output_first),
                        ctx.get(engine.output_last),
                        ctx.get(engine.output_interface),
                        ctx.get(engine.output_endpoint),
                    ) == held
                    await ctx.tick("usb")

            ctx.set(engine.output_ready, 1)
            await ctx.tick("usb")
            ctx.set(engine.output_ready, 0)

    simulation.add_testbench(bench)
    simulation.run()


def test_signed_32_bit_field_crossing_five_bytes_near_buffer_end() -> None:
    bit_offset = 59 * 8 + 7
    entry = map_entry(
        bit_offset=bit_offset,
        bit_width=32,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-(1 << 31),
        logical_maximum=(1 << 31) - 1,
        report_length=64,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        native = bytearray([0xA5] * 64)
        insert_bits(native, bit_offset, 32, -1_000_000)

        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 123_456)
        ctx.set(engine.relative_command_sequence, 0x7732)
        await push_report(ctx, engine, bytes(native))
        emitted, acknowledgements = await drain_report(ctx, engine, 64)
        ctx.set(engine.relative_valid, 0)

        assert acknowledgements == {"relative"}
        assert extract_signed(emitted, bit_offset, 32) == -876_544
        assert ctx.get(engine.pending_x) == 0
        assert ctx.get(engine.last_committed_command_sequence) == 0x7732
        untouched_mask = field_mask(64, bit_offset, 32)
        for actual, original, mutable in zip(emitted, native, untouched_mask, strict=True):
            assert (actual & ~mutable) == (original & ~mutable)

    simulation.add_testbench(bench)
    simulation.run()


def run_relative_case(
    case_index: int,
    case: tuple[int, int, int, int, int, int, int, int],
) -> None:
    (
        offset,
        width,
        logical_minimum,
        logical_maximum,
        physical,
        injected,
        expected_emitted,
        expected_residual,
    ) = case
    report_length = (offset + width + 7) // 8
    entry = map_entry(
        bit_offset=offset,
        bit_width=width,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=logical_minimum,
        logical_maximum=logical_maximum,
        report_length=report_length,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        native = bytearray([0xA5] * report_length)
        insert_bits(native, offset, width, physical)
        untouched_mask = field_mask(report_length, offset, width)

        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, injected)
        ctx.set(engine.relative_command_sequence, case_index)
        await push_report(ctx, engine, bytes(native))
        emitted, acknowledgements = await drain_report(ctx, engine, report_length)
        ctx.set(engine.relative_valid, 0)

        assert acknowledgements == {"relative"}
        assert extract_signed(emitted, offset, width) == expected_emitted
        assert ctx.get(engine.pending_x) == expected_residual
        assert ctx.get(engine.last_committed_command_sequence) == case_index
        for actual, original, mutable in zip(emitted, native, untouched_mask, strict=True):
            assert (actual & ~mutable) == (original & ~mutable)

    simulation.add_testbench(bench)
    simulation.run()


def test_arbitrary_signed_relative_fields_clamp_and_retain_residual() -> None:
    for case_index, case in enumerate(CASES, start=1):
        run_relative_case(case_index, case)


def test_field_pipeline_snapshots_metadata_and_commits_registered_total() -> None:
    entry = map_entry(
        bit_offset=8,
        bit_width=12,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-2048,
        logical_maximum=2047,
        report_length=3,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        native = bytearray(3)
        insert_bits(native, 8, 12, 1000)
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 1500)
        await push_report(ctx, engine, bytes(native))

        stages = []
        for _ in range(20_000):
            if ctx.get(engine._entry_snapshot_active):
                stages.append("snapshot")
            if ctx.get(engine._entry_dispatch_active):
                stages.append("dispatch")
                assert ctx.get(engine._entry_matches_q)
                assert ctx.get(engine._entry_class_q) == 2
                assert ctx.get(engine._entry_bit_offset_q) == 8
                assert ctx.get(engine._entry_bit_width_q) == 12
                assert ctx.get(engine._entry_logical_minimum_q) == -2048
                assert ctx.get(engine._entry_logical_maximum_q) == 2047
            if ctx.get(engine._field_total_capture_active):
                stages.append("total")
            if ctx.get(engine._field_commit_active):
                stages.append("commit")
                assert ctx.get(engine._field_total_q) == 2500
            if ctx.get(engine.output_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("field pipeline did not produce an output")

        assert stages == ["snapshot", "dispatch", "total", "commit"]
        emitted, acknowledgements = await drain_report(ctx, engine, 3)
        ctx.set(engine.relative_valid, 0)
        assert acknowledgements == {"relative"}
        assert extract_signed(emitted, 8, 12) == 2047
        assert ctx.get(engine.pending_x) == 453

    simulation.add_testbench(bench)
    simulation.run()


def test_button_mask_physical_priority_report_id_and_vendor_bits() -> None:
    report_id = 0x2A
    entries = [
        map_entry(
            bit_offset=8,
            bit_width=1,
            flags=INJ_MAP_ENTRY_FLAG_BUTTON,
            logical_minimum=0,
            logical_maximum=1,
            report_length=4,
            usage=1,
            report_id=report_id,
            entry_index=0,
        ),
        map_entry(
            bit_offset=9,
            bit_width=1,
            flags=INJ_MAP_ENTRY_FLAG_BUTTON,
            logical_minimum=0,
            logical_maximum=1,
            report_length=4,
            usage=2,
            report_id=report_id,
            entry_index=1,
        ),
        map_entry(
            bit_offset=20,
            bit_width=1,
            flags=INJ_MAP_ENTRY_FLAG_BUTTON,
            logical_minimum=0,
            logical_maximum=1,
            report_length=4,
            usage=9,
            report_id=report_id,
            entry_index=2,
        ),
    ]
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, entries)
        native = bytes([report_id, 0b1110_0011, 0b1011_0101, 0b0110_1101])

        # Suppress physical Button 1, leave physically held Button 2 alone,
        # and inject Button 9, whose mapped report bit lies in another byte.
        ctx.set(engine.button_valid, 1)
        ctx.set(engine.button_interface, 0)
        ctx.set(engine.button_endpoint, 1)
        ctx.set(engine.button_report_id, report_id)
        ctx.set(engine.button_buttons, 1 << 8)
        ctx.set(engine.button_command_sequence, 0x31)
        ctx.set(engine.mask_valid, 1)
        ctx.set(engine.mask_interface, 0)
        ctx.set(engine.mask_endpoint, 1)
        ctx.set(engine.mask_report_id, report_id)
        ctx.set(engine.mask_buttons, 1 << 0)
        ctx.set(engine.mask_command_sequence, 0x31)

        await push_report(ctx, engine, native)
        emitted, acknowledgements = await drain_report(ctx, engine, len(native))
        ctx.set(engine.button_valid, 0)
        ctx.set(engine.mask_valid, 0)

        assert acknowledgements == {"button", "mask"}
        assert emitted[0] == report_id
        assert not (emitted[1] & 0x01)  # masked physical Button 1
        assert emitted[1] & 0x02  # physical Button 2 survives injected release
        assert emitted[2] & 0x10  # injected Button 9 at full-wire bit offset 20

        mutable = field_mask(4, 8, 1)
        mutable = bytes(
            first | second for first, second in zip(mutable, field_mask(4, 9, 1), strict=True)
        )
        mutable = bytes(
            first | second for first, second in zip(mutable, field_mask(4, 20, 1), strict=True)
        )
        for actual, original, mapped in zip(emitted, native, mutable, strict=True):
            assert (actual & ~mapped) == (original & ~mapped)

    simulation.add_testbench(bench)
    simulation.run()


def test_output_report_id_is_zero_for_unmapped_reports_and_stable_for_mapped_ids() -> None:
    report_id = 0x2A
    entry = map_entry(
        bit_offset=8,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=2,
        report_id=report_id,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await push_report(ctx, engine, bytes([report_id, 0]))
        await wait_for_output(ctx, engine)
        assert ctx.get(engine.output_report_id) == 0
        unmapped, _ = await drain_report(ctx, engine, 2)
        assert unmapped == bytes([report_id, 0])

        await commit_map(ctx, harness.map_store, [entry])
        await push_report(ctx, engine, bytes([report_id, 0]))
        await wait_for_output(ctx, engine)
        ctx.set(engine.output_ready, 1)
        for index in range(2):
            assert ctx.get(engine.output_valid)
            assert ctx.get(engine.output_report_id) == report_id
            assert ctx.get(engine.output_first) == (index == 0)
            assert ctx.get(engine.output_last) == (index == 1)
            await ctx.tick("usb")
        ctx.set(engine.output_ready, 0)

    simulation.add_testbench(bench)
    simulation.run()


def test_report_id_query_falls_back_to_no_id_while_native_input_is_held() -> None:
    entry = map_entry(
        bit_offset=8,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=2,
        report_id=0,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 5)
        ctx.set(engine.relative_command_sequence, 0x88)

        ctx.set(engine.report_interface, 0)
        ctx.set(engine.report_endpoint, 1)
        ctx.set(engine.report_valid, 1)
        ctx.set(engine.report_data, 0x2A)
        ctx.set(engine.report_first, 1)
        ctx.set(engine.report_last, 0)
        assert ctx.get(engine.report_ready)
        await ctx.tick("usb")

        ctx.set(engine.report_data, 10)
        ctx.set(engine.report_first, 0)
        ctx.set(engine.report_last, 1)
        assert ctx.get(engine.report_ready)
        await ctx.tick("usb")

        # Keep the already accepted final byte presented while both bounded
        # directory queries run. The engine must not accept it a second time.
        for _ in range(4):
            assert not ctx.get(engine.report_ready)
            await ctx.tick("usb")
        ctx.set(engine.report_valid, 0)
        ctx.set(engine.report_last, 0)

        emitted, acknowledgements = await drain_report(ctx, engine, 2)
        ctx.set(engine.relative_valid, 0)
        assert emitted == bytes([0x2A, 15])
        assert acknowledgements == {"relative"}
        assert ctx.get(engine.last_committed_command_sequence) == 0x88

    simulation.add_testbench(bench)
    simulation.run()


def test_backpressure_defers_all_transactional_state_until_full_acceptance() -> None:
    entries = [
        map_entry(
            bit_offset=0,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
            entry_index=0,
        ),
        map_entry(
            bit_offset=8,
            bit_width=1,
            flags=INJ_MAP_ENTRY_FLAG_BUTTON,
            logical_minimum=0,
            logical_maximum=1,
            report_length=2,
            usage=1,
            entry_index=1,
        ),
    ]
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, entries)
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 100)
        ctx.set(engine.relative_command_sequence, 7)
        ctx.set(engine.button_valid, 1)
        ctx.set(engine.button_interface, 0)
        ctx.set(engine.button_endpoint, 1)
        ctx.set(engine.button_report_id, 0)
        ctx.set(engine.button_buttons, 1)
        ctx.set(engine.button_command_sequence, 7)
        await push_report(ctx, engine, bytes([100, 0]))

        for _ in range(20_000):
            if ctx.get(engine.output_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("mutated report did not become available")

        held = (
            ctx.get(engine.output_data),
            ctx.get(engine.output_first),
            ctx.get(engine.output_last),
            ctx.get(engine.output_interface),
            ctx.get(engine.output_endpoint),
        )
        for _ in range(8):
            assert (
                ctx.get(engine.output_data),
                ctx.get(engine.output_first),
                ctx.get(engine.output_last),
                ctx.get(engine.output_interface),
                ctx.get(engine.output_endpoint),
            ) == held
            assert ctx.get(engine.pending_x) == 0
            assert ctx.get(engine.injected_buttons) == 0
            assert ctx.get(engine.last_committed_command_sequence) == 0
            assert not ctx.get(engine.relative_ready)
            assert not ctx.get(engine.button_ready)
            await ctx.tick("usb")

        emitted, acknowledgements = await drain_report(ctx, engine, 2)
        ctx.set(engine.relative_valid, 0)
        ctx.set(engine.button_valid, 0)
        assert emitted == bytes([127, 1])
        assert acknowledgements == {"relative", "button"}
        assert ctx.get(engine.pending_x) == 73
        assert ctx.get(engine.injected_buttons) == 1
        assert ctx.get(engine.last_committed_command_sequence) == 7

        for _ in range(8):
            await ctx.tick("usb")
            assert ctx.get(engine.pending_x) == 73
            assert ctx.get(engine.injected_buttons) == 1
            assert ctx.get(engine.last_committed_command_sequence) == 7

    simulation.add_testbench(bench)
    simulation.run()


def test_map_replacement_during_field_scan_fails_open_without_hybrid_output() -> None:
    old_entries = [
        map_entry(
            bit_offset=index * 32,
            bit_width=32,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-32768,
            logical_maximum=32767,
            report_length=32,
            entry_index=index,
        )
        for index in range(8)
    ]
    replacement_entries = [
        map_entry(
            bit_offset=8,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=32,
            generation=2,
        )
    ]
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, old_entries)
        native = bytes(32)
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 100)
        ctx.set(engine.relative_command_sequence, 0x51)
        await push_report(ctx, engine, native)

        # The eight 32-bit fields keep the engine scanning the old bank while
        # the smaller replacement validates and atomically flips active_bank.
        await commit_map(ctx, harness.map_store, replacement_entries, generation=2)
        assert ctx.get(harness.map_store.active_generation) == 2
        assert not ctx.get(engine.output_valid)

        emitted, acknowledgements = await drain_report(ctx, engine, len(native))
        ctx.set(engine.relative_valid, 0)
        assert emitted == native
        assert not acknowledgements
        assert ctx.get(engine.pending_x) == 0
        assert ctx.get(engine.last_committed_command_sequence) == 0
        assert not ctx.get(engine.command_committed)

    simulation.add_testbench(bench)
    simulation.run()


def test_map_replacement_while_final_byte_blocked_discards_stale_commit() -> None:
    old_entries = [
        map_entry(
            bit_offset=0,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
        )
    ]
    replacement_entries = [
        map_entry(
            bit_offset=8,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
            generation=2,
        )
    ]
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, old_entries)
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 100)
        ctx.set(engine.relative_command_sequence, 0x52)
        await push_report(ctx, engine, bytes([100, 0]))

        for _ in range(20_000):
            if ctx.get(engine.output_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("mutated report did not become available")

        # Admit the first byte under generation 1, then block the final byte.
        ctx.set(engine.output_ready, 1)
        assert ctx.get(engine.output_data) == 127
        assert ctx.get(engine.output_first)
        await ctx.tick("usb")
        ctx.set(engine.output_ready, 0)
        assert ctx.get(engine.output_valid)
        assert ctx.get(engine.output_last)
        held_final_byte = ctx.get(engine.output_data)

        await commit_map(ctx, harness.map_store, replacement_entries, generation=2)
        assert ctx.get(harness.map_store.active_generation) == 2
        assert ctx.get(engine.output_valid)
        assert ctx.get(engine.output_last)
        assert ctx.get(engine.output_data) == held_final_byte
        assert not ctx.get(engine.relative_ready)

        ctx.set(engine.output_ready, 1)
        assert not ctx.get(engine.relative_ready)
        await ctx.tick("usb")
        ctx.set(engine.output_ready, 0)
        ctx.set(engine.relative_valid, 0)

        # The already-started report finishes from its frozen generation-1
        # buffer, but its scratch state is discarded and the command is not
        # acknowledged under generation 2.
        assert ctx.get(engine.pending_x) == 0
        assert ctx.get(engine.last_committed_command_sequence) == 0
        assert not ctx.get(engine.command_committed)

    simulation.add_testbench(bench)
    simulation.run()


def test_residual_carry_drains_on_next_stationary_report() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=2,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 100)
        ctx.set(engine.relative_command_sequence, 0x61)

        await push_report(ctx, engine, bytes([100, 0xA5]))
        emitted, acknowledgements = await drain_report(ctx, engine, 2)
        ctx.set(engine.relative_valid, 0)

        assert emitted == bytes([127, 0xA5])
        assert acknowledgements == {"relative"}
        assert ctx.get(engine.pending_x) == 73

        await pulse_sof(ctx, engine)
        stationary, acknowledgements = await drain_report(ctx, engine, 2)

        assert stationary == bytes([73, 0xA5])
        assert not acknowledgements
        assert ctx.get(engine.pending_x) == 0

    simulation.add_testbench(bench)
    simulation.run()


def test_stationary_synthesis_clears_cached_native_relative_motion() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=2,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 200)
        ctx.set(engine.relative_command_sequence, 0x62)

        # The cached native template contains -40. The first report therefore
        # leaves +33 pending after clamping -40 + 200 to +127.
        await push_report(ctx, engine, bytes([0xD8, 0x5A]))
        emitted, _ = await drain_report(ctx, engine, 2)
        ctx.set(engine.relative_valid, 0)
        assert emitted == bytes([127, 0x5A])
        assert ctx.get(engine.pending_x) == 33

        await pulse_sof(ctx, engine)
        stationary, _ = await drain_report(ctx, engine, 2)

        # Replaying the stale -40 would produce -7. Stationary synthesis must
        # first treat every mapped relative field as physical zero.
        assert stationary == bytes([33, 0x5A])
        assert ctx.get(engine.pending_x) == 0

    simulation.add_testbench(bench)
    simulation.run()


def test_stationary_report_remains_single_pending_item_across_sofs() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 100)
        await push_report(ctx, engine, bytes([100]))
        emitted, _ = await drain_report(ctx, engine, 1)
        ctx.set(engine.relative_valid, 0)
        assert emitted == bytes([127])
        assert ctx.get(engine.pending_x) == 73

        await pulse_sof(ctx, engine)
        await wait_for_output(ctx, engine)
        held = (
            ctx.get(engine.output_data),
            ctx.get(engine.output_first),
            ctx.get(engine.output_last),
        )

        for _ in range(4):
            await pulse_sof(ctx, engine)
            assert ctx.get(engine.output_valid)
            assert (
                ctx.get(engine.output_data),
                ctx.get(engine.output_first),
                ctx.get(engine.output_last),
            ) == held
            assert ctx.get(engine.pending_x) == 73

        stationary, _ = await drain_report(ctx, engine, 1)
        assert stationary == bytes([73])
        assert ctx.get(engine.pending_x) == 0

        for _ in range(4):
            await pulse_sof(ctx, engine)
            for _ in range(4):
                await ctx.tick("usb")
                assert not ctx.get(engine.output_valid)

    simulation.add_testbench(bench)
    simulation.run()


def test_click_release_waits_for_external_accepted_report_count() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=1,
        flags=INJ_MAP_ENTRY_FLAG_BUTTON,
        logical_minimum=0,
        logical_maximum=1,
        report_length=1,
        usage=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.accepted_report_count, 10)
        ctx.set(engine.button_valid, 1)
        ctx.set(engine.button_interface, 0)
        ctx.set(engine.button_endpoint, 1)
        ctx.set(engine.button_report_id, 0)
        ctx.set(engine.button_buttons, 1)
        ctx.set(engine.button_hold_reports, 2)
        ctx.set(engine.button_command_sequence, 0x63)

        await push_report(ctx, engine, bytes([0]))
        pressed, acknowledgements = await drain_report(ctx, engine, 1)
        ctx.set(engine.button_valid, 0)
        assert pressed == bytes([1])
        assert acknowledgements == {"button"}
        assert ctx.get(engine.injected_buttons) == 1

        ctx.set(engine.accepted_report_count, 11)
        await pulse_sof(ctx, engine)
        for _ in range(8):
            await ctx.tick("usb")
            assert not ctx.get(engine.output_valid)
        assert ctx.get(engine.injected_buttons) == 1

        ctx.set(engine.accepted_report_count, 12)
        await pulse_sof(ctx, engine)
        await wait_for_output(ctx, engine)
        assert ctx.get(engine.injected_buttons) == 1

        released, acknowledgements = await drain_report(ctx, engine, 1)
        assert released == bytes([0])
        assert not acknowledgements
        assert ctx.get(engine.injected_buttons) == 0

    simulation.add_testbench(bench)
    simulation.run()


def test_residual_overflow_rejects_command_without_wrapping_pending_state() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, (1 << 31) - 1)
        ctx.set(engine.relative_command_sequence, 0x64)
        await push_report(ctx, engine, bytes([127]))
        emitted, _ = await drain_report(ctx, engine, 1)
        assert emitted == bytes([127])
        assert ctx.get(engine.pending_x) == (1 << 31) - 1
        assert ctx.get(engine.last_committed_command_sequence) == 0x64

        ctx.set(engine.relative_x, 1)
        ctx.set(engine.relative_command_sequence, 0x65)
        await pulse_sof(ctx, engine)
        await wait_for_output(ctx, engine)

        # The overflowing +1 command must not wrap INT32_MAX to INT32_MIN,
        # and the existing residual remains untouched until report admission.
        assert ctx.get(engine.pending_x) == (1 << 31) - 1
        assert ctx.get(engine.command_overflow) == 0
        assert ctx.get(engine.last_committed_command_sequence) == 0x64
        stationary, acknowledgements = await drain_report(ctx, engine, 1)
        ctx.set(engine.relative_valid, 0)

        assert stationary == bytes([127])
        assert acknowledgements == {"relative"}
        assert ctx.get(engine.command_overflow) == 1
        assert ctx.get(engine.pending_x) == (1 << 31) - 1 - 127
        assert ctx.get(engine.last_committed_command_sequence) == 0x64
        assert not ctx.get(engine.command_committed)

    simulation.add_testbench(bench)
    simulation.run()


def test_zero_delta_relative_command_waits_for_native_report() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        await push_report(ctx, engine, bytes([0]))
        native, _ = await drain_report(ctx, engine, 1)
        assert native == bytes([0])

        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 0)
        ctx.set(engine.relative_command_sequence, 0x66)

        for _ in range(4):
            await pulse_sof(ctx, engine)
            for _ in range(8):
                await ctx.tick("usb")
                assert not ctx.get(engine.output_valid)
                assert not ctx.get(engine.relative_ready)
        assert ctx.get(engine.last_committed_command_sequence) == 0

        await push_report(ctx, engine, bytes([0]))
        emitted, acknowledgements = await drain_report(ctx, engine, 1)
        ctx.set(engine.relative_valid, 0)
        assert emitted == bytes([0])
        assert acknowledgements == {"relative"}
        assert ctx.get(engine.last_committed_command_sequence) == 0x66

    simulation.add_testbench(bench)
    simulation.run()


def test_stationary_template_cache_is_one_bounded_synchronous_memory() -> None:
    harness = InjectionHarness()
    converted = rtlil.convert(
        harness,
        ports=[
            harness.engine.report_valid,
            harness.engine.report_ready,
            harness.engine.output_valid,
            harness.engine.output_ready,
            harness.engine.sof_tick,
        ],
    )

    # Geometry: byte-wide and deep, so it infers a block RAM rather than the
    # wide-shallow LUTRAM it used to be. ECP5 LUTRAM cost scales with total
    # bits, so 512x16 spent hundreds of LUTs on the same 8,192 bits that fit in
    # one 18 kb block.
    assert "memory width 8 size 1024 \\stationary_template_cache" in converted
    # A single-byte write port: one enable, not a 64-lane byte mask.
    assert "wire width 64 \\template_cache_write__en" not in converted
    # The durable invariant, and the reason this test exists (48ee45e): the
    # cache must stay ONE bounded memory. Unrolling it into a register per
    # layout is what this catches, and it is independent of the shape above.
    assert "state_template_0" not in converted


def test_native_template_copy_blocks_capture_and_feeds_stationary_report() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=4,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 100)
        ctx.set(engine.relative_command_sequence, 0x77)
        await push_report(ctx, engine, bytes([100, 0xA1, 0xB2, 0xC3]))
        emitted, acknowledgements = await drain_report(ctx, engine, 4)
        ctx.set(engine.relative_valid, 0)
        assert emitted == bytes([127, 0xA1, 0xB2, 0xC3])
        assert acknowledgements == {"relative"}
        assert ctx.get(engine.pending_x) == 73

        # A mapped native report is not accepted until its native lane has
        # copied into the stationary-template cache byte by byte. An SOF in
        # that copy window is deferred and drives stationary output without a
        # second pulse.
        await pulse_sof(ctx, engine)
        ctx.set(engine.report_valid, 1)
        ctx.set(engine.report_first, 1)
        ctx.set(engine.report_last, 1)
        ctx.set(engine.report_data, 0x55)
        for _ in range(3):
            assert not ctx.get(engine.report_ready)
            await ctx.tick("usb")
        ctx.set(engine.report_valid, 0)
        ctx.set(engine.report_first, 0)
        ctx.set(engine.report_last, 0)

        stationary, stationary_acknowledgements = await drain_report(ctx, engine, 4)
        assert stationary == bytes([73, 0xA1, 0xB2, 0xC3])
        assert not stationary_acknowledgements

    simulation.add_testbench(bench)
    simulation.run()


def test_invalidation_during_stationary_template_load_aborts_partial_report() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=4,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 100)
        await push_report(ctx, engine, bytes([100, 0xA1, 0xB2, 0xC3]))
        emitted, _ = await drain_report(ctx, engine, 4)
        ctx.set(engine.relative_valid, 0)
        assert emitted == bytes([127, 0xA1, 0xB2, 0xC3])
        assert ctx.get(engine.pending_x) == 73

        # Let the four-byte native-template copy finish, then enter stationary
        # load and allow byte zero to be copied before invalidating the map.
        for _ in range(5):
            await ctx.tick("usb")
        await pulse_sof(ctx, engine)
        await ctx.tick("usb")
        await ctx.tick("usb")
        await ctx.tick("usb")
        ctx.set(harness.map_store.invalidate, 1)
        await ctx.tick("usb")
        ctx.set(harness.map_store.invalidate, 0)

        for _ in range(12):
            await ctx.tick("usb")
            assert not ctx.get(engine.output_valid)
        assert ctx.get(engine.pending_x) == 73

    simulation.add_testbench(bench)
    simulation.run()


def test_engine_report_buffer_is_one_bounded_synchronous_block_memory() -> None:
    harness = InjectionHarness()
    converted = rtlil.convert(
        harness,
        ports=[
            harness.engine.report_valid,
            harness.engine.report_ready,
            harness.engine.output_valid,
            harness.engine.output_ready,
            harness.engine.sof_tick,
        ],
    )

    assert converted.count("memory width 16 size 64") == 1
    assert "input_template_0" not in converted
    assert "output_buffer_0" not in converted


def test_sixteen_layout_states_remain_isolated() -> None:
    entries = [
        map_entry(
            bit_offset=8,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
            report_id=report_id,
            entry_index=report_id - 1,
        )
        for report_id in range(1, 17)
    ]
    harness = InjectionHarness(max_fields=16)
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, entries)
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)

        for report_id in range(1, 17):
            ctx.set(engine.relative_report_id, report_id)
            ctx.set(engine.relative_x, 27 + report_id)
            await push_report(ctx, engine, bytes([report_id, 100]))
            emitted, acknowledgements = await drain_report(ctx, engine, 2)
            assert emitted == bytes([report_id, 127])
            assert acknowledgements == {"relative"}

        ctx.set(engine.relative_valid, 0)
        for report_id in range(1, 17):
            await push_report(ctx, engine, bytes([report_id, 0]))
            emitted, acknowledgements = await drain_report(ctx, engine, 2)
            assert emitted == bytes([report_id, report_id])
            assert not acknowledgements

    simulation.add_testbench(bench)
    simulation.run()


def test_stationary_atomic_pipeline_streams_slots_zero_one_and_fifteen() -> None:
    entries = [
        map_entry(
            bit_offset=8,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
            report_id=report_id,
            entry_index=report_id - 1,
        )
        for report_id in range(1, 17)
    ]
    harness = InjectionHarness(max_fields=16)
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def accumulate_residual(ctx, report_id: int, residual: int) -> None:
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, report_id)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 27 + residual)
        await push_report(ctx, engine, bytes([report_id, 100]))
        emitted, _ = await drain_report(ctx, engine, 2)
        assert emitted == bytes([report_id, 127])
        ctx.set(engine.relative_valid, 0)

    async def count_scan_clocks(ctx) -> int:
        for _ in range(8):
            if ctx.get(engine._stationary_scan_active):
                break
            await ctx.tick("usb")
        assert ctx.get(engine._stationary_scan_active)

        scan_clocks = 0
        while ctx.get(engine._stationary_scan_active):
            scan_clocks += 1
            await ctx.tick("usb")
        assert scan_clocks <= 18
        return scan_clocks

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, entries)
        await accumulate_residual(ctx, 1, 11)
        await accumulate_residual(ctx, 2, 17)
        await accumulate_residual(ctx, 16, 22)

        await pulse_sof(ctx, engine)
        scan_clocks = await count_scan_clocks(ctx)
        assert scan_clocks == 3
        await wait_for_output(ctx, engine)
        first, _ = await drain_report(ctx, engine, 2)
        assert first == bytes([1, 11])

        await pulse_sof(ctx, engine)
        scan_clocks = await count_scan_clocks(ctx)
        assert scan_clocks == 4
        await wait_for_output(ctx, engine)
        second, _ = await drain_report(ctx, engine, 2)
        assert second == bytes([2, 17])

        await pulse_sof(ctx, engine)
        scan_clocks = await count_scan_clocks(ctx)
        assert scan_clocks == 18
        await wait_for_output(ctx, engine)
        last, _ = await drain_report(ctx, engine, 2)
        assert last == bytes([16, 22])

    simulation.add_testbench(bench)
    simulation.run()


def test_global_clear_waits_for_final_accept_and_cannot_resurrect_stale_state() -> None:
    entries = [
        map_entry(
            bit_offset=8,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
            report_id=report_id,
            entry_index=report_id - 1,
        )
        for report_id in (1, 2)
    ]
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def accumulate_residual(ctx, report_id: int, residual: int) -> None:
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, report_id)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 27 + residual)
        await push_report(ctx, engine, bytes([report_id, 100]))
        emitted, _ = await drain_report(ctx, engine, 2)
        assert emitted == bytes([report_id, 127])
        ctx.set(engine.relative_valid, 0)

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, entries)
        await accumulate_residual(ctx, 1, 10)
        await accumulate_residual(ctx, 2, 20)
        assert ctx.get(engine.pending_x) == 20

        ctx.set(engine.clear_valid, 1)
        ctx.set(engine.clear_flags, INJ_CLEAR_FLAG_MOTION)
        ctx.set(engine.clear_command_sequence, 0x44)
        await push_report(ctx, engine, bytes([1, 0]))
        await wait_for_output(ctx, engine)

        ctx.set(engine.output_ready, 1)
        assert ctx.get(engine.output_first)
        await ctx.tick("usb")
        ctx.set(engine.output_ready, 0)
        assert ctx.get(engine.output_last)
        for _ in range(8):
            assert not ctx.get(engine.clear_ready)
            assert ctx.get(engine.pending_x) == 20
            await ctx.tick("usb")

        ctx.set(engine.output_ready, 1)
        assert ctx.get(engine.clear_ready)
        await ctx.tick("usb")
        ctx.set(engine.output_ready, 0)
        ctx.set(engine.clear_valid, 0)
        assert ctx.get(engine.pending_x) == 0

        await pulse_sof(ctx, engine)
        for _ in range(24):
            await ctx.tick("usb")
            assert not ctx.get(engine.output_valid)

        await push_report(ctx, engine, bytes([2, 0]))
        emitted, acknowledgements = await drain_report(ctx, engine, 2)
        assert emitted == bytes([2, 0])
        assert not acknowledgements

        await pulse_sof(ctx, engine)
        for _ in range(24):
            await ctx.tick("usb")
            assert not ctx.get(engine.output_valid)

    simulation.add_testbench(bench)
    simulation.run()


def test_invalidation_during_native_state_lookup_fails_open() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, 50)

        await push_report(ctx, engine, bytes([10]))
        ctx.set(harness.map_store.invalidate, 1)
        await ctx.tick("usb")
        ctx.set(harness.map_store.invalidate, 0)

        emitted, acknowledgements = await drain_report(ctx, engine, 1)
        assert emitted == bytes([10])
        assert not acknowledgements
        assert ctx.get(engine.pending_x) == 0

    simulation.add_testbench(bench)
    simulation.run()


def test_layout_state_is_one_bounded_synchronous_memory() -> None:
    harness = InjectionHarness()
    converted = rtlil.convert(
        harness,
        ports=[
            harness.engine.report_valid,
            harness.engine.report_ready,
            harness.engine.output_valid,
            harness.engine.output_ready,
            harness.engine.sof_tick,
        ],
    )

    assert converted.count("memory width 368 size 16") == 1
    assert "state_x_0" not in converted
    assert "state_buttons_0" not in converted


def test_click_release_count_wraps_from_u32_max_to_zero() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=1,
        flags=INJ_MAP_ENTRY_FLAG_BUTTON,
        logical_minimum=0,
        logical_maximum=1,
        report_length=1,
        usage=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.accepted_report_count, 0xFFFF_FFFE)
        ctx.set(engine.button_valid, 1)
        ctx.set(engine.button_interface, 0)
        ctx.set(engine.button_endpoint, 1)
        ctx.set(engine.button_report_id, 0)
        ctx.set(engine.button_buttons, 1)
        ctx.set(engine.button_hold_reports, 2)

        await push_report(ctx, engine, bytes([0]))
        pressed, _ = await drain_report(ctx, engine, 1)
        ctx.set(engine.button_valid, 0)
        assert pressed == bytes([1])

        ctx.set(engine.accepted_report_count, 0xFFFF_FFFF)
        await pulse_sof(ctx, engine)
        for _ in range(8):
            await ctx.tick("usb")
            assert not ctx.get(engine.output_valid)

        ctx.set(engine.accepted_report_count, 0)
        await pulse_sof(ctx, engine)
        released, _ = await drain_report(ctx, engine, 1)
        assert released == bytes([0])
        assert ctx.get(engine.injected_buttons) == 0

    simulation.add_testbench(bench)
    simulation.run()


def test_negative_residual_overflow_rejects_and_preserves_sequence() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, -(1 << 31))
        ctx.set(engine.relative_command_sequence, 0x67)
        await push_report(ctx, engine, bytes([0x81]))
        emitted, _ = await drain_report(ctx, engine, 1)
        assert emitted == bytes([0x81])
        assert ctx.get(engine.pending_x) == -(1 << 31)

        ctx.set(engine.relative_x, -1)
        ctx.set(engine.relative_command_sequence, 0x68)
        await pulse_sof(ctx, engine)
        await wait_for_output(ctx, engine)
        assert ctx.get(engine.pending_x) == -(1 << 31)
        assert ctx.get(engine.last_committed_command_sequence) == 0x67

        stationary, acknowledgements = await drain_report(ctx, engine, 1)
        ctx.set(engine.relative_valid, 0)
        assert stationary == bytes([0x81])
        assert acknowledgements == {"relative"}
        assert ctx.get(engine.command_overflow) == 1
        assert ctx.get(engine.pending_x) == -(1 << 31) + 127
        assert ctx.get(engine.last_committed_command_sequence) == 0x67

    simulation.add_testbench(bench)
    simulation.run()


def test_multi_axis_overflow_rejects_whole_relative_command() -> None:
    entries = [
        map_entry(
            bit_offset=0,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
            entry_index=0,
        ),
        map_entry(
            bit_offset=8,
            bit_width=8,
            flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_Y),
            logical_minimum=-127,
            logical_maximum=127,
            report_length=2,
            usage=0x31,
            entry_index=1,
        ),
    ]
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, entries)
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, (1 << 31) - 1)
        ctx.set(engine.relative_command_sequence, 0x69)
        await push_report(ctx, engine, bytes([127, 0]))
        await drain_report(ctx, engine, 2)
        assert ctx.get(engine.pending_x) == (1 << 31) - 1
        assert ctx.get(engine.pending_y) == 0

        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y)
        ctx.set(engine.relative_x, 1)
        ctx.set(engine.relative_y, 50)
        ctx.set(engine.relative_command_sequence, 0x6A)
        await pulse_sof(ctx, engine)
        await wait_for_output(ctx, engine)
        assert ctx.get(engine.pending_x) == (1 << 31) - 1
        assert ctx.get(engine.pending_y) == 0
        assert ctx.get(engine.command_overflow) == 0

        stationary, acknowledgements = await drain_report(ctx, engine, 2)
        ctx.set(engine.relative_valid, 0)
        assert stationary == bytes([127, 0])
        assert acknowledgements == {"relative"}
        assert ctx.get(engine.pending_y) == 0
        assert ctx.get(engine.command_overflow) == 1
        assert ctx.get(engine.last_committed_command_sequence) == 0x69

    simulation.add_testbench(bench)
    simulation.run()


def test_command_overflow_counter_saturates() -> None:
    entry = map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=1,
    )
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [entry])
        ctx.set(engine.relative_valid, 1)
        ctx.set(engine.relative_interface, 0)
        ctx.set(engine.relative_endpoint, 1)
        ctx.set(engine.relative_report_id, 0)
        ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
        ctx.set(engine.relative_x, (1 << 31) - 1)
        await push_report(ctx, engine, bytes([127]))
        await drain_report(ctx, engine, 1)
        assert ctx.get(engine.pending_x) == (1 << 31) - 1

        ctx.set(engine.command_overflow, 0xFFFF_FFFF)
        ctx.set(engine.relative_x, 1)
        await pulse_sof(ctx, engine)
        await drain_report(ctx, engine, 1)
        ctx.set(engine.relative_valid, 0)
        assert ctx.get(engine.command_overflow) == 0xFFFF_FFFF

    simulation.add_testbench(bench)
    simulation.run()


def _relative_x_entry(report_length: int = 1) -> MapEntryPayload:
    return map_entry(
        bit_offset=0,
        bit_width=8,
        flags=(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
        logical_minimum=-127,
        logical_maximum=127,
        report_length=report_length,
    )


async def _leave_a_valid_state_slot(ctx, engine) -> None:
    """Drive one mapped relative report so a state record is written and valid."""
    ctx.set(engine.relative_valid, 1)
    ctx.set(engine.relative_interface, 0)
    ctx.set(engine.relative_endpoint, 1)
    ctx.set(engine.relative_report_id, 0)
    ctx.set(engine.relative_flags, INJ_RELATIVE_FLAG_X)
    ctx.set(engine.relative_x, 100)
    await push_report(ctx, engine, bytes([100]))
    emitted, _ = await drain_report(ctx, engine, 1)
    ctx.set(engine.relative_valid, 0)
    assert emitted == bytes([127])


def test_empty_map_commit_does_not_wedge_the_stationary_scan() -> None:
    # InjectionMapStore deliberately accepts a zero-entry/zero-layout map, and
    # nothing on that path clears state_valid. With active_layout_count == 0 the
    # scan's three exits are all unreachable, so it free-runs forever with
    # report_ready stuck low - which stalls the whole passthrough, because
    # ReportMergeMux is unbuffered and the poller gates issue_poll on ~buffer_full.
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [_relative_x_entry()])
        await _leave_a_valid_state_slot(ctx, engine)

        await commit_map(ctx, harness.map_store, [], generation=2)
        assert ctx.get(harness.map_store.active_valid) == 1
        assert ctx.get(harness.map_store.active_layout_count) == 0

        await pulse_sof(ctx, engine)
        scan_cycles = 0
        for _ in range(64):
            await ctx.tick("usb")
            scan_cycles += ctx.get(engine._stationary_scan_active)
            assert not ctx.get(engine.output_valid), "empty map must synthesise nothing"
        # A bounded scan walks at most the 16 layout slots plus pipeline latency.
        assert scan_cycles <= 18
        assert ctx.get(engine._stationary_scan_active) == 0

        # The relay must stay alive; this is the part that kills the passthrough.
        for index in range(3):
            await pulse_sof(ctx, engine)
            await push_report(ctx, engine, bytes([0x11 + index]))
            relayed, _ = await drain_report(ctx, engine, 1)
            assert relayed == bytes([0x11 + index])

    simulation.add_testbench(bench)
    simulation.run()


def test_stationary_scan_is_skipped_when_active_map_has_no_layouts() -> None:
    # Pins the entry guard specifically: with only the scan_last fix the scan is
    # still entered and burns cycles every SOF for as long as the map is empty.
    harness = InjectionHarness()
    engine = harness.engine
    simulation = Simulator(harness)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        await commit_map(ctx, harness.map_store, [_relative_x_entry()])
        await _leave_a_valid_state_slot(ctx, engine)
        await commit_map(ctx, harness.map_store, [], generation=2)

        await pulse_sof(ctx, engine)
        for cycle in range(32):
            await ctx.tick("usb")
            assert ctx.get(engine._stationary_scan_active) == 0, f"entered scan at +{cycle}"

    simulation.add_testbench(bench)
    simulation.run()
