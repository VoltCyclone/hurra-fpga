"""End-to-end proof that a boot-mouse RELATIVE injection is valid at the FPGA.

This uses a hand-built two-entry boot-mouse map (X at byte 1, Y at byte 2 of a
[buttons, X, Y, wheel] report) and emits RELATIVE motion against it, proving the
gateware decode path accepts those field values and mutates a live report
additively.

It is no longer the map the MCXN947 firmware uploads: the firmware now compiles
its map from the attached device's report descriptor, and those compiled maps --
buttons, 16-bit and 12-bit axes, report IDs -- are checked against the same
gateware in ``test_mcu_compiled_map_e2e.py``. The injected X/Y *magnitude* here is
not part of any contract; representative values are used purely to prove the
mutation is additive.
"""

import zlib

from test_injection_e2e import (
    drain_output,
    initialize,
    push_report,
    run_simulation,
    send_rx,
)

from hurra_cynthion.injection_map import MapError
from hurra_cynthion.injection_wire import (
    INJ_MAP_ENTRY_FLAG_RELATIVE,
    INJ_MAP_ENTRY_FLAG_SIGNED,
    INJ_MAP_ENTRY_FLAG_X,
    INJ_MAP_ENTRY_FLAG_Y,
    INJ_RELATIVE_FLAG_X,
    INJ_RELATIVE_FLAG_Y,
    INJ_TYPE_MAP_BEGIN,
    INJ_TYPE_MAP_COMMIT,
    INJ_TYPE_MAP_ENTRY,
    INJ_TYPE_RELATIVE,
    MapBeginPayload,
    MapCommitPayload,
    MapEntryPayload,
    RelativePayload,
)

# Representative injection magnitudes (not the contract; the firmware default is
# tunable and deliberately gentler). Chosen large enough that the additive
# mutation is unmistakable in the assertions below.
FW_INJECT_X = -8
FW_INJECT_Y = -24
FW_REPORT_LENGTH = 4
FW_INTERFACE = 0
FW_ENDPOINT = 1
FW_REPORT_ID = 0
FW_MAP_GENERATION = 1


def _boot_entries(descriptor_generation: int) -> list[MapEntryPayload]:
    """Build a two-entry boot-mouse field map.

    X lands at bit offset 8 (byte 1) and Y at bit offset 16 (byte 2) of a
    [buttons, X, Y, wheel] boot report, both as 8-bit signed relative axes.
    """
    common = {
        "descriptor_generation": descriptor_generation,
        "map_generation": FW_MAP_GENERATION,
        "interface_number": FW_INTERFACE,
        "endpoint_number": FW_ENDPOINT,
        "report_id": FW_REPORT_ID,
        "usage_page": 0x01,
        "logical_minimum": -127,
        "logical_maximum": 127,
        "report_length": FW_REPORT_LENGTH,
    }
    x = MapEntryPayload(
        entry_index=0,
        usage=0x30,
        bit_offset=8,
        bit_width=8,
        flags=INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_X,
        **common,
    )
    y = MapEntryPayload(
        entry_index=1,
        usage=0x31,
        bit_offset=16,
        bit_width=8,
        flags=INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE | INJ_MAP_ENTRY_FLAG_Y,
        **common,
    )
    return [x, y]


async def _commit_boot_map(ctx, harness, entries: list[MapEntryPayload], seq0: int = 1) -> None:
    """Upload and commit ``entries`` through the real SPI decode path, then wait.

    Drives the full MAP_BEGIN -> MAP_ENTRY* -> MAP_COMMIT handshake exactly as the
    MCU would, and returns once ``InjectionMapStore`` acknowledges the commit --
    asserting it landed without error and left an active map in place.
    """
    plane = harness.plane
    blob = b"".join(e.to_bytes() for e in entries)
    metadata = {
        "descriptor_generation": entries[0].descriptor_generation,
        "map_generation": FW_MAP_GENERATION,
        "entry_count": len(entries),
        "layout_count": 1,
        "flags": 0,
        "entries_crc32": zlib.crc32(blob),
    }
    await send_rx(
        ctx, plane, INJ_TYPE_MAP_BEGIN, MapBeginPayload(**metadata).to_bytes(), sequence=seq0
    )
    for i, entry in enumerate(entries):
        await send_rx(
            ctx, plane, INJ_TYPE_MAP_ENTRY, entry.to_bytes(), sequence=(seq0 + 1 + i) & 0xFF
        )
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_MAP_COMMIT,
        MapCommitPayload(**metadata).to_bytes(),
        sequence=(seq0 + 1 + len(entries)) & 0xFF,
    )
    for _ in range(8_000):
        await ctx.tick("usb")
        if ctx.get(plane.map_store.commit_ack):
            assert ctx.get(plane.map_store.commit_error) == MapError.NONE
            assert ctx.get(plane.map_store.active_valid)
            return
    raise AssertionError("firmware boot map did not commit")


def test_firmware_boot_map_commits() -> None:
    # The FPGA must accept the firmware's exact boot map and activate its generation.
    async def bench(ctx, harness) -> None:
        await initialize(ctx, harness.plane)
        dg = ctx.get(harness.store.descriptor_generation)
        await _commit_boot_map(ctx, harness, _boot_entries(dg))
        assert ctx.get(harness.plane.map_store.active_generation) == FW_MAP_GENERATION

    run_simulation(bench)


def test_firmware_relative_command_mutates_a_live_report_additively() -> None:
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        dg = ctx.get(harness.store.descriptor_generation)
        await _commit_boot_map(ctx, harness, _boot_entries(dg))

        rel = RelativePayload(
            lease_generation=FW_MAP_GENERATION,
            map_generation=FW_MAP_GENERATION,
            command_sequence=1,
            target_frame=0,
            interface_number=FW_INTERFACE,
            endpoint_number=FW_ENDPOINT,
            report_id=FW_REPORT_ID,
            flags=INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y,
            x=FW_INJECT_X,
            y=FW_INJECT_Y,
            wheel=0,
            pan=0,
            hold_reports=0,
        )
        await send_rx(ctx, plane, INJ_TYPE_RELATIVE, rel.to_bytes(), sequence=0x40)

        # A Teensy-style live report: buttons=0, X=+5, Y=+100, wheel=0. The
        # injection is additive, so X -> 5-8 = -3 (253) and Y -> 100-24 = 76.
        native = bytes([0x00, 5, 100, 0x00])
        await push_report(ctx, plane, native, interface=FW_INTERFACE, endpoint=FW_ENDPOINT)
        emitted = await drain_output(ctx, plane, len(native))

        assert emitted[0] == 0x00  # buttons untouched
        assert emitted[1] == (5 + FW_INJECT_X) & 0xFF  # 253
        assert emitted[2] == (100 + FW_INJECT_Y) & 0xFF  # 76
        assert emitted[3] == 0x00  # wheel untouched

    run_simulation(bench)


def test_firmware_relative_command_moves_a_still_device_right_after_the_commit() -> None:
    # No native report between the commit and the command: the device is still.
    # The command is delivered from the seeded template and acked.
    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        dg = ctx.get(harness.store.descriptor_generation)
        await _commit_boot_map(ctx, harness, _boot_entries(dg))

        rel = RelativePayload(
            lease_generation=FW_MAP_GENERATION,
            map_generation=FW_MAP_GENERATION,
            command_sequence=1,
            target_frame=0,
            interface_number=FW_INTERFACE,
            endpoint_number=FW_ENDPOINT,
            report_id=FW_REPORT_ID,
            flags=INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y,
            x=FW_INJECT_X,
            y=FW_INJECT_Y,
            wheel=0,
            pan=0,
            hold_reports=0,
        )
        await send_rx(ctx, plane, INJ_TYPE_RELATIVE, rel.to_bytes(), sequence=0x40)

        ctx.set(plane.sof_tick, 1)
        await ctx.tick("usb")
        ctx.set(plane.sof_tick, 0)
        emitted = await drain_output(ctx, plane, FW_REPORT_LENGTH)

        assert emitted == bytes([0x00, FW_INJECT_X & 0xFF, FW_INJECT_Y & 0xFF, 0x00])
        assert ctx.get(plane.synthesized_report_count) == 1
        assert ctx.get(plane.command_commit_count) == 1
        # Consumed: the RX staging is free.
        assert not ctx.get(plane.engine.relative_valid)

    run_simulation(bench)
