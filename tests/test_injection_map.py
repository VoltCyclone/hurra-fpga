import random
import zlib
from dataclasses import dataclass

from amaranth import Elaboratable, Module, Signal
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.injection_map import InjectionMapStore, MapError, _crc32_byte
from hurra_cynthion.injection_wire import (
    INJ_MAP_ENTRY_FLAG_BUTTON,
    INJ_MAP_ENTRY_FLAG_RELATIVE,
    INJ_MAP_ENTRY_FLAG_SIGNED,
    INJ_MAP_ENTRY_FLAG_X,
    INJ_MAP_ENTRY_FLAG_Y,
    MapEntryPayload,
)


@dataclass(frozen=True)
class CommitResult:
    error: MapError
    error_entry_index: int
    active_generation: int
    active_entry_count: int
    active_layout_count: int
    terminal_cycles: int


class Crc32ByteHarness(Elaboratable):
    def __init__(self) -> None:
        self.crc = Signal(32)
        self.byte = Signal(8)
        self.result = Signal(32)

    def elaborate(self, platform):
        m = Module()
        m.d.comb += self.result.eq(_crc32_byte(self.crc, self.byte))
        return m


def test_crc32_byte_matches_zlib_for_boundary_and_random_vectors() -> None:
    dut = Crc32ByteHarness()
    simulation = Simulator(dut)
    rng = random.Random(0xC32B)
    vectors = [
        (0x00000000, 0x00),
        (0x00000000, 0xFF),
        (0xFFFFFFFF, 0x00),
        (0xFFFFFFFF, 0xFF),
        (0x80000000, 0x01),
        (0x00000001, 0x80),
        *((rng.getrandbits(32), rng.getrandbits(8)) for _ in range(128)),
    ]

    async def bench(ctx):
        for crc, byte in vectors:
            ctx.set(dut.crc, crc)
            ctx.set(dut.byte, byte)
            await ctx.delay(1e-9)
            expected = zlib.crc32(bytes([byte]), crc ^ 0xFFFFFFFF) ^ 0xFFFFFFFF
            assert ctx.get(dut.result) == expected

    simulation.add_testbench(bench)
    simulation.run()


def field(
    kind: str,
    bit_offset: int,
    bit_width: int,
    *,
    descriptor_generation: int = 1,
    map_generation: int = 1,
    entry_index: int = 0,
    interface_number: int = 0,
    endpoint_number: int = 1,
    report_id: int = 0,
    usage_page: int = 1,
    usage: int = 0x30,
    flags: int | None = None,
    logical_minimum: int = -32768,
    logical_maximum: int = 32767,
    report_length: int = 4,
) -> MapEntryPayload:
    if flags is None:
        flags_by_kind = {
            "button": INJ_MAP_ENTRY_FLAG_BUTTON,
            "x": (INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X),
            "y": (INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_Y),
        }
        flags = flags_by_kind[kind]
    return MapEntryPayload(
        descriptor_generation=descriptor_generation,
        map_generation=map_generation,
        entry_index=entry_index,
        interface_number=interface_number,
        endpoint_number=endpoint_number,
        report_id=report_id,
        usage_page=usage_page,
        usage=usage,
        bit_offset=bit_offset,
        bit_width=bit_width,
        flags=flags,
        logical_minimum=logical_minimum,
        logical_maximum=logical_maximum,
        report_length=report_length,
    )


def with_candidate_metadata(
    entry: MapEntryPayload,
    *,
    descriptor_generation: int,
    map_generation: int,
    entry_index: int,
) -> MapEntryPayload:
    values = entry.__dict__ | {
        "descriptor_generation": descriptor_generation,
        "map_generation": map_generation,
        "entry_index": entry_index,
    }
    return MapEntryPayload(**values)


def simulated_store(bench, *, max_layouts: int = 16, max_fields: int = 64) -> None:
    store = InjectionMapStore(max_layouts=max_layouts, max_fields=max_fields)
    simulation = Simulator(store)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        await bench(ctx, store)

    simulation.add_testbench(wrapped)
    simulation.run()


async def commit_candidate(
    ctx,
    store,
    *,
    generation: int,
    entries: list[MapEntryPayload],
    descriptor_generation: int = 1,
    current_descriptor_generation: int = 1,
    declared_entry_count: int | None = None,
    declared_layout_count: int | None = None,
    entries_crc32: int | None = None,
    commit_generation: int | None = None,
    commit_descriptor_generation: int | None = None,
    after_commit=None,
) -> CommitResult:
    entries = [
        with_candidate_metadata(
            entry,
            descriptor_generation=descriptor_generation,
            map_generation=generation,
            entry_index=index,
        )
        for index, entry in enumerate(entries)
    ]
    if declared_entry_count is None:
        declared_entry_count = len(entries)
    if declared_layout_count is None:
        declared_layout_count = len(
            {(entry.interface_number, entry.endpoint_number, entry.report_id) for entry in entries}
        )
    canonical_payload = b"".join(entry.to_bytes() for entry in entries)
    if entries_crc32 is None:
        entries_crc32 = zlib.crc32(canonical_payload)

    ctx.set(store.descriptor_generation, current_descriptor_generation)
    ctx.set(store.candidate_descriptor_generation, descriptor_generation)
    ctx.set(store.candidate_map_generation, generation)
    ctx.set(store.candidate_entry_count, declared_entry_count)
    ctx.set(store.candidate_layout_count, declared_layout_count)
    ctx.set(store.candidate_entries_crc32, entries_crc32)
    ctx.set(store.begin, 1)
    await ctx.tick("usb")
    ctx.set(store.begin, 0)

    for entry in entries:
        ctx.set(store.entry.as_value(), int.from_bytes(entry.to_bytes(), "little"))
        ctx.set(store.entry_valid, 1)
        await ctx.tick("usb")
    ctx.set(store.entry_valid, 0)

    ctx.set(
        store.candidate_descriptor_generation,
        descriptor_generation
        if commit_descriptor_generation is None
        else commit_descriptor_generation,
    )
    ctx.set(
        store.candidate_map_generation,
        generation if commit_generation is None else commit_generation,
    )
    ctx.set(store.commit, 1)
    await ctx.tick("usb")
    ctx.set(store.commit, 0)
    assert not ctx.get(store.commit_ack)
    if after_commit is not None:
        await after_commit(ctx, store)

    for terminal_cycles in range(1, 8_001):
        await ctx.tick("usb")
        if ctx.get(store.commit_ack):
            return CommitResult(
                error=MapError(ctx.get(store.commit_error)),
                error_entry_index=ctx.get(store.commit_error_entry_index),
                active_generation=ctx.get(store.active_generation),
                active_entry_count=ctx.get(store.active_entry_count),
                active_layout_count=ctx.get(store.active_layout_count),
                terminal_cycles=terminal_cycles,
            )
    raise AssertionError("map commit did not produce a terminal acknowledgement")


async def lookup_entry(ctx, store, index: int) -> MapEntryPayload | None:
    ctx.set(store.lookup_index, index)
    await ctx.tick("usb")
    if not ctx.get(store.lookup_valid):
        return None
    payload = ctx.get(store.lookup_entry.as_value()).to_bytes(26, "little")
    return MapEntryPayload.from_bytes(payload)


async def query_layout(
    ctx,
    store,
    *,
    interface: int,
    endpoint: int,
    report_id: int,
) -> tuple[bool, int, int, int]:
    ctx.set(store.layout_interface, interface)
    ctx.set(store.layout_endpoint, endpoint)
    ctx.set(store.layout_report_id, report_id)
    assert ctx.get(store.query_ready)
    ctx.set(store.query_valid, 1)
    await ctx.tick("usb")
    ctx.set(store.query_valid, 0)

    cycles = 0
    while not ctx.get(store.result_valid):
        assert not ctx.get(store.query_ready)
        cycles += 1
        assert cycles <= 16
        await ctx.tick("usb")

    assert not ctx.get(store.query_ready)
    result = (
        bool(ctx.get(store.layout_found)),
        ctx.get(store.layout_index),
        ctx.get(store.layout_report_length),
        cycles,
    )
    await ctx.tick("usb")
    assert not ctx.get(store.result_valid)
    assert ctx.get(store.query_ready)
    return result


def test_candidate_layout_identity_is_registered_before_scan_and_append() -> None:
    expected_identities = [
        (0, 1, 1, 4),
        (1, 2, 2, 5),
    ]

    async def bench(ctx, store) -> None:
        captured_identities = []
        dispatched_identities = []

        async def observe_identity_captures(ctx, store) -> None:
            for _ in range(2_000):
                capture_active = ctx.get(store._candidate_layout_capture_active)
                dispatch_active = ctx.get(store._candidate_layout_dispatch_active)
                if dispatch_active:
                    dispatched_identities.append(
                        (
                            ctx.get(store._candidate_layout_interface_q),
                            ctx.get(store._candidate_layout_endpoint_q),
                            ctx.get(store._candidate_layout_report_id_q),
                            ctx.get(store._candidate_layout_report_length_q),
                        )
                    )
                await ctx.tick("usb")
                if capture_active:
                    captured_identities.append(
                        (
                            ctx.get(store._candidate_layout_interface_q),
                            ctx.get(store._candidate_layout_endpoint_q),
                            ctx.get(store._candidate_layout_report_id_q),
                            ctx.get(store._candidate_layout_report_length_q),
                        )
                    )
                if len(captured_identities) == len(expected_identities) and len(
                    dispatched_identities
                ) == len(expected_identities):
                    return
            raise AssertionError("candidate layout identities were not captured")

        result = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[
                field(
                    "x",
                    8,
                    12,
                    interface_number=0,
                    endpoint_number=1,
                    report_id=1,
                    report_length=4,
                ),
                field(
                    "y",
                    8,
                    12,
                    interface_number=1,
                    endpoint_number=2,
                    report_id=2,
                    report_length=5,
                ),
            ],
            after_commit=observe_identity_captures,
        )
        assert result.error == MapError.NONE
        assert captured_identities == expected_identities
        assert dispatched_identities == expected_identities

    simulated_store(bench)


def test_validation_snapshot_adds_one_bounded_cycle_per_entry_and_preserves_rejection_index() -> (
    None
):
    async def bench(ctx, store) -> None:
        one_entry = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[
                field(
                    "button",
                    8,
                    1,
                    report_id=1,
                    usage=1,
                    report_length=2,
                )
            ],
        )
        assert one_entry.error == MapError.NONE
        assert one_entry.terminal_cycles == 546

        three_entries = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[
                field(
                    "button",
                    8,
                    1,
                    report_id=index + 1,
                    usage=index + 1,
                    report_length=2,
                )
                for index in range(3)
            ],
        )
        assert three_entries.error == MapError.NONE
        assert three_entries.terminal_cycles == 615

        rejected = await commit_candidate(
            ctx,
            store,
            generation=3,
            entries=[
                field(
                    "button",
                    8,
                    1,
                    report_id=1,
                    usage=1,
                    report_length=2,
                ),
                field(
                    "button",
                    8,
                    1,
                    endpoint_number=0,
                    report_id=2,
                    usage=2,
                    report_length=2,
                ),
                field(
                    "button",
                    8,
                    1,
                    report_id=3,
                    usage=3,
                    report_length=2,
                ),
            ],
        )
        assert rejected.error == MapError.ENDPOINT
        assert rejected.error_entry_index == 1
        assert rejected.terminal_cycles == 549
        assert rejected.active_generation == 2
        assert rejected.active_entry_count == 3

    simulated_store(bench)


def test_commit_is_atomic_and_overlap_rejects_whole_candidate() -> None:
    async def bench(ctx, store) -> None:
        committed = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[field("x", 8, 12)],
        )
        assert committed.error == MapError.NONE
        assert committed.active_generation == 1

        rejected = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[field("x", 8, 12), field("y", 16, 8)],
        )
        assert rejected.error == MapError.OVERLAP
        assert rejected.error_entry_index == 1
        assert rejected.active_generation == 1
        assert rejected.active_entry_count == 1
        assert await lookup_entry(ctx, store, 0) == with_candidate_metadata(
            field("x", 8, 12),
            descriptor_generation=1,
            map_generation=1,
            entry_index=0,
        )

    simulated_store(bench)


def test_commit_error_is_a_single_cycle_pulse_that_returns_to_defaults() -> None:
    # Pins the source contract that ``DebugRegisterBlock.latch_on`` exists to cover:
    # both fields are valid only on the ``commit_ack`` cycle. If this shape ever
    # changes, the latch in ``gateware.py`` must be revisited.
    async def bench(ctx, store) -> None:
        await commit_candidate(ctx, store, generation=1, entries=[field("x", 8, 12)])
        rejected = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[field("x", 8, 12), field("y", 16, 8)],
        )
        assert rejected.error == MapError.OVERLAP
        assert rejected.error_entry_index == 1

        # ``commit_candidate`` returns on the ``commit_ack`` cycle itself.
        for cycle in range(1, 21):
            await ctx.tick("usb")
            assert ctx.get(store.commit_ack) == 0, f"ack still high at +{cycle}"
            assert ctx.get(store.commit_error) == MapError.NONE, f"error held at +{cycle}"
            assert ctx.get(store.commit_error_entry_index) == 0xFF, f"entry held at +{cycle}"

    simulated_store(bench)


def test_rejects_wrong_length_width_count_and_descriptor_generation() -> None:
    async def bench(ctx, store) -> None:
        cases = [
            (
                {"entries": [field("x", 0, 8, report_length=65)]},
                MapError.REPORT_LENGTH,
            ),
            ({"entries": [field("x", 0, 0)]}, MapError.FIELD_WIDTH),
            ({"entries": [field("x", 0, 33)]}, MapError.FIELD_WIDTH),
            (
                {
                    "entries": [field("x", 0, 8)],
                    "declared_entry_count": 65,
                },
                MapError.FIELD_COUNT,
            ),
            (
                {
                    "entries": [field("x", 0, 8)],
                    "descriptor_generation": 2,
                    "current_descriptor_generation": 1,
                },
                MapError.GENERATION,
            ),
        ]
        for generation, (arguments, error) in enumerate(cases, start=1):
            result = await commit_candidate(
                ctx,
                store,
                generation=generation,
                **arguments,
            )
            assert result.error == error
            assert result.active_generation == 0

    simulated_store(bench)


def test_report_start_coordinates_and_layout_occupancy_are_enforced() -> None:
    async def bench(ctx, store) -> None:
        accepted = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[field("x", 8, 12, report_id=1, report_length=3)],
        )
        assert accepted.error == MapError.NONE

        prefix = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[field("x", 0, 8, report_id=1, report_length=3)],
        )
        assert prefix.error == MapError.REPORT_ID_PREFIX
        assert prefix.active_generation == 1

        bit_bound = await commit_candidate(
            ctx,
            store,
            generation=3,
            entries=[field("x", 17, 8, report_id=1, report_length=3)],
        )
        assert bit_bound.error == MapError.BIT_OFFSET
        assert bit_bound.active_generation == 1

        separate_layout = await commit_candidate(
            ctx,
            store,
            generation=4,
            entries=[
                field("x", 8, 12, report_id=1, report_length=3),
                field("y", 8, 12, report_id=2, report_length=3),
            ],
        )
        assert separate_layout.error == MapError.NONE
        assert separate_layout.active_layout_count == 2

    simulated_store(bench)


def test_identical_offsets_are_independent_across_interface_and_endpoint_layouts() -> None:
    async def bench(ctx, store) -> None:
        separate_interfaces = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[
                field("x", 8, 12, interface_number=0),
                field("y", 8, 12, interface_number=1),
            ],
        )
        assert separate_interfaces.error == MapError.NONE
        assert separate_interfaces.active_layout_count == 2

        separate_endpoints = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[
                field("x", 8, 12, endpoint_number=1),
                field("y", 8, 12, endpoint_number=2),
            ],
        )
        assert separate_endpoints.error == MapError.NONE
        assert separate_endpoints.active_layout_count == 2

    simulated_store(bench)


def test_exact_final_report_bit_is_accepted() -> None:
    async def bench(ctx, store) -> None:
        result = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[field("button", 511, 1, usage=1, report_length=64)],
        )
        assert result.error == MapError.NONE
        assert result.active_generation == 1

    simulated_store(bench)


def test_descriptor_change_during_validation_rejects_without_bank_flip() -> None:
    async def bench(ctx, store) -> None:
        baseline_entry = field("x", 8, 12, report_id=1, report_length=4)
        baseline = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[baseline_entry],
        )
        assert baseline.error == MapError.NONE
        baseline_bank = ctx.get(store.active_bank)

        async def invalidate_descriptor(ctx, store) -> None:
            for _ in range(5):
                await ctx.tick("usb")
                assert not ctx.get(store.commit_ack)
            ctx.set(store.descriptor_generation, 2)

        rejected = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[
                field("x", 8, 32, report_id=1, report_length=8),
                field("y", 40, 24, report_id=1, report_length=8),
            ],
            after_commit=invalidate_descriptor,
        )
        assert rejected.error == MapError.GENERATION
        assert rejected.error_entry_index == 0xFF
        assert rejected.active_generation == 1
        assert rejected.active_entry_count == 1
        assert rejected.active_layout_count == 1
        assert ctx.get(store.active_bank) == baseline_bank

        ctx.set(store.descriptor_generation, 1)
        assert await lookup_entry(ctx, store, 0) == with_candidate_metadata(
            baseline_entry,
            descriptor_generation=1,
            map_generation=1,
            entry_index=0,
        )

    simulated_store(bench)


def test_descriptor_invalidation_cancels_outstanding_lookup_response() -> None:
    async def bench(ctx, store) -> None:
        committed = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[field("x", 8, 12)],
        )
        assert committed.error == MapError.NONE

        ctx.set(store.lookup_index, 0)
        await ctx.tick("usb")
        assert ctx.get(store.lookup_valid)

        ctx.set(store.descriptor_generation, 2)
        assert not ctx.get(store.active_valid)
        assert not ctx.get(store.lookup_valid)

    simulated_store(bench)


def test_explicit_invalidation_permanently_clears_both_map_banks() -> None:
    async def bench(ctx, store) -> None:
        committed = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[field("x", 8, 12)],
        )
        assert committed.error == MapError.NONE
        assert ctx.get(store.active_valid)

        ctx.set(store.invalidate, 1)
        await ctx.tick("usb")
        ctx.set(store.invalidate, 0)
        assert not ctx.get(store.active_valid)

        for _ in range(4):
            await ctx.tick("usb")
            assert not ctx.get(store.active_valid)

        replacement = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[field("y", 24, 8)],
        )
        assert replacement.error == MapError.NONE
        assert ctx.get(store.active_valid)
        assert ctx.get(store.active_generation) == 2

    simulated_store(bench)


def test_bank_flip_cancels_old_bank_lookup_response() -> None:
    async def bench(ctx, store) -> None:
        baseline = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=[field("x", 8, 12)],
        )
        assert baseline.error == MapError.NONE

        ctx.set(store.lookup_index, 0)
        replacement = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[field("y", 24, 8)],
        )
        assert replacement.error == MapError.NONE
        assert replacement.active_generation == 2
        assert not ctx.get(store.lookup_valid)

        await ctx.tick("usb")
        assert ctx.get(store.lookup_valid)
        assert ctx.get(store.lookup_entry.map_generation) == 2

    simulated_store(bench)


def test_rejects_invalid_layouts_crc_generations_and_unsupported_fields() -> None:
    async def bench(ctx, store) -> None:
        invalid_cases = [
            (
                {"entries": [field("x", 0, 8, interface_number=4)]},
                MapError.INTERFACE,
            ),
            (
                {"entries": [field("x", 0, 8, endpoint_number=0)]},
                MapError.ENDPOINT,
            ),
            (
                {"entries": [field("x", 0, 8, flags=0)]},
                MapError.UNSUPPORTED_FIELD,
            ),
            (
                {"entries": [field("x", 0, 8)], "entries_crc32": 0x12345678},
                MapError.CRC,
            ),
            (
                {"entries": [field("x", 0, 8)], "commit_generation": 99},
                MapError.MAP_GENERATION,
            ),
            (
                {
                    "entries": [field("x", 0, 8)],
                    "commit_descriptor_generation": 99,
                },
                MapError.GENERATION,
            ),
            (
                {
                    "entries": [
                        field("x", 0, 8, report_length=2),
                        field("y", 8, 8, report_length=3),
                    ],
                },
                MapError.REPORT_LENGTH,
            ),
            (
                {
                    "entries": [field("x", 0, 8)],
                    "declared_layout_count": 2,
                },
                MapError.LAYOUT_COUNT,
            ),
        ]
        for generation, (arguments, error) in enumerate(invalid_cases, start=1):
            result = await commit_candidate(
                ctx,
                store,
                generation=generation,
                **arguments,
            )
            assert result.error == error
            assert result.active_generation == 0

    simulated_store(bench)


def test_bounds_sixteen_layouts_and_sixty_four_packed_entries() -> None:
    async def bench(ctx, store) -> None:
        entries = [
            field(
                "button",
                8 + (index % 4),
                1,
                report_id=(index // 4) + 1,
                usage=index + 1,
                report_length=3,
            )
            for index in range(64)
        ]
        accepted = await commit_candidate(
            ctx,
            store,
            generation=1,
            entries=entries,
        )
        assert accepted.error == MapError.NONE
        assert accepted.active_entry_count == 64
        assert accepted.active_layout_count == 16

        too_many_layouts = await commit_candidate(
            ctx,
            store,
            generation=2,
            entries=[
                field(
                    "button",
                    8,
                    1,
                    report_id=index + 1,
                    usage=index + 1,
                    report_length=2,
                )
                for index in range(17)
            ],
        )
        assert too_many_layouts.error == MapError.LAYOUT_COUNT
        assert too_many_layouts.active_generation == 1

    simulated_store(bench)


def test_active_layout_and_packed_entry_lookup() -> None:
    async def bench(ctx, store) -> None:
        entries = [
            field("x", 8, 12, report_id=1, report_length=4),
            field("button", 20, 3, report_id=1, usage=1, report_length=4),
        ]
        result = await commit_candidate(ctx, store, generation=9, entries=entries)
        assert result.error == MapError.NONE

        found, index, report_length, _cycles = await query_layout(
            ctx,
            store,
            interface=0,
            endpoint=1,
            report_id=1,
        )
        assert found
        assert index == 0
        assert report_length == 4

        assert await lookup_entry(ctx, store, 0) == with_candidate_metadata(
            entries[0],
            descriptor_generation=1,
            map_generation=9,
            entry_index=0,
        )
        assert await lookup_entry(ctx, store, 1) == with_candidate_metadata(
            entries[1],
            descriptor_generation=1,
            map_generation=9,
            entry_index=1,
        )
        assert await lookup_entry(ctx, store, 2) is None

        ctx.set(store.descriptor_generation, 2)
        assert not ctx.get(store.active_valid)
        assert ctx.get(store.active_generation) == 9
        found, index, report_length, cycles = await query_layout(
            ctx,
            store,
            interface=0,
            endpoint=1,
            report_id=1,
        )
        assert not found
        assert index == 0
        assert report_length == 0
        assert cycles == 1
        assert await lookup_entry(ctx, store, 0) is None

    simulated_store(bench)


def test_active_layout_index_tracks_slot_bank_and_invalid_lookup() -> None:
    async def bench(ctx, store) -> None:
        first_bank = [
            field("x", 8, 8, report_id=4, report_length=3),
            field("x", 8, 8, report_id=7, report_length=3),
            field("y", 16, 8, report_id=4, report_length=3),
        ]
        result = await commit_candidate(ctx, store, generation=9, entries=first_bank)
        assert result.error == MapError.NONE

        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=4) == (
            True,
            0,
            3,
            1,
        )
        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=7) == (
            True,
            1,
            3,
            2,
        )
        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=99) == (
            False,
            0,
            0,
            2,
        )

        second_bank = [
            field("x", 8, 8, report_id=7, report_length=3),
            field("x", 8, 8, report_id=4, report_length=3),
        ]
        result = await commit_candidate(ctx, store, generation=10, entries=second_bank)
        assert result.error == MapError.NONE

        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=7) == (
            True,
            0,
            3,
            1,
        )
        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=4) == (
            True,
            1,
            3,
            2,
        )

        ctx.set(store.descriptor_generation, 2)
        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=4) == (
            False,
            0,
            0,
            1,
        )

    simulated_store(bench)


def test_layout_query_latency_and_stale_snapshot_miss_are_bounded() -> None:
    async def bench(ctx, store) -> None:
        entries = [
            field(
                "button",
                8,
                1,
                report_id=index + 1,
                usage=index + 1,
                report_length=2,
            )
            for index in range(16)
        ]
        result = await commit_candidate(ctx, store, generation=1, entries=entries)
        assert result.error == MapError.NONE

        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=1) == (
            True,
            0,
            2,
            1,
        )
        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=16) == (
            True,
            15,
            2,
            16,
        )
        assert await query_layout(ctx, store, interface=0, endpoint=1, report_id=99) == (
            False,
            0,
            0,
            16,
        )

        ctx.set(store.layout_interface, 0)
        ctx.set(store.layout_endpoint, 1)
        ctx.set(store.layout_report_id, 16)
        assert ctx.get(store.query_ready)
        ctx.set(store.query_valid, 1)
        await ctx.tick("usb")
        ctx.set(store.query_valid, 0)
        for _ in range(4):
            assert not ctx.get(store.result_valid)
            assert not ctx.get(store.query_ready)
            await ctx.tick("usb")

        ctx.set(store.descriptor_generation, 2)
        await ctx.tick("usb")
        assert ctx.get(store.result_valid)
        assert not ctx.get(store.layout_found)
        assert ctx.get(store.layout_index) == 0
        assert ctx.get(store.layout_report_length) == 0

    simulated_store(bench)


def test_module_elaborates_to_two_banks_of_sixty_four_packed_entries() -> None:
    store = InjectionMapStore()
    netlist = rtlil.convert(
        store,
        ports=[
            store.begin,
            store.entry_valid,
            store.entry.as_value(),
            store.commit,
            store.abort,
            store.active_generation,
            store.lookup_index,
            store.lookup_valid,
            store.lookup_entry.as_value(),
            store.query_valid,
            store.query_ready,
            store.result_valid,
            store.layout_found,
            store.layout_index,
            store.layout_report_length,
            store.commit_ack,
            store.commit_error,
        ],
    )
    assert netlist.count("memory width 208 size 64") == 2
    assert netlist.count("memory width 16 size 512") == 1
    assert netlist.count("memory width 31 size 32") == 1
    assert "layout_interface_0_0" not in netlist
