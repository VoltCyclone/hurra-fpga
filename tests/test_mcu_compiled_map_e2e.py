"""The MCXN947's descriptor-compiled maps are accepted by the real FPGA map store.

The firmware compiles its injection map from the attached device's own report
descriptor (``firmware/mcxn947/src/hid_mouse_layout.c`` -> ``inj_map_build.c``).
Its host tests prove what bytes it emits; these tests compile those same C
sources, feed them the same descriptor fixtures (``test/hid_fixtures.h``), and
push the resulting MAP_ENTRY payloads through the gateware's real validation
and mutation path. That is the only place the two ends meet on shapes the old
fixed boot map never exercised: button entries, 16-bit and 12-bit axes, a
report-ID prefix, wheel and AC Pan.
"""

import dataclasses
import shutil
import subprocess
import textwrap
import zlib
from pathlib import Path

import pytest
from test_injection_e2e import (
    drain_output,
    initialize,
    push_report,
    run_simulation,
    send_rx,
)

from hurra_cynthion.injection_map import MapError
from hurra_cynthion.injection_wire import (
    INJ_RELATIVE_FLAG_WHEEL,
    INJ_RELATIVE_FLAG_X,
    INJ_RELATIVE_FLAG_Y,
    INJ_TYPE_BUTTON_STATE,
    INJ_TYPE_MAP_BEGIN,
    INJ_TYPE_MAP_COMMIT,
    INJ_TYPE_MAP_ENTRY,
    INJ_TYPE_RELATIVE,
    ButtonStatePayload,
    MapBeginPayload,
    MapCommitPayload,
    MapEntryPayload,
    RelativePayload,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MCU_ROOT = REPO_ROOT / "firmware" / "mcxn947"
MCU_SOURCES = (
    "hid_item.c",
    "hid_fields.c",
    "hid_mouse_layout.c",
    "inj_map_build.c",
    "inj_command.c",
    "spi_frame.c",
)
# The firmware's own host-test warning set; weaker flags could accept C that
# `make test` rejects.
HOST_WARNINGS = ("-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wshadow")

MAP_GENERATION = 1
INTERFACE = 1
ENDPOINT = 2

_HARNESS = textwrap.dedent(
    """
    #include <stdio.h>

    #include "hid_fixtures.h"
    #include "hid_mouse_layout.h"
    #include "inj_map_build.h"

    int main(void)
    {
        hid_mouse_layout_t layout;
        if (hid_mouse_compile(FIXTURE, sizeof(FIXTURE), &layout) != HID_MOUSE_OK) {
            return 1;
        }
        const inj_map_target_t target = {
            .descriptor_generation = 0u,
            .map_generation = %(map_generation)du,
            .interface_number = %(interface)du,
            .endpoint_number = %(endpoint)du,
            .report_length = layout.report_length,
        };
        inj_map_entry_payload_t entries[HID_MOUSE_MAX_FIELDS];
        const uint8_t count = inj_map_build_entries(&layout, &target, entries);
        for (uint8_t i = 0u; i < count; ++i) {
            const uint8_t *bytes = (const uint8_t *)&entries[i];
            for (size_t b = 0u; b < sizeof(entries[i]); ++b) {
                printf("%%02x", bytes[b]);
            }
            printf("\\n");
        }
        return 0;
    }
    """
)


def _firmware_entries(
    tmp_path: Path, fixture: str, *, interface: int = INTERFACE, endpoint: int = ENDPOINT
) -> list[MapEntryPayload]:
    """Compile the firmware's map for ``fixture`` and return its MAP_ENTRY payloads."""
    harness = tmp_path / "harness.c"
    harness.write_text(
        _HARNESS % {"map_generation": MAP_GENERATION, "interface": interface, "endpoint": endpoint}
    )
    binary = tmp_path / "harness"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            *HOST_WARNINGS,
            f"-DFIXTURE={fixture}",
            "-isystem",
            str(MCU_ROOT / "include"),
            "-I",
            str(MCU_ROOT / "src"),
            "-I",
            str(MCU_ROOT / "test"),
            str(harness),
            *(str(MCU_ROOT / "src" / name) for name in MCU_SOURCES),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    out = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
    return [MapEntryPayload.from_bytes(bytes.fromhex(line)) for line in out.split()]


async def _commit(ctx, harness, entries: list[MapEntryPayload]) -> None:
    """Upload and commit ``entries`` through the real SPI decode path."""
    plane = harness.plane
    generation = ctx.get(harness.store.descriptor_generation)
    entries = [dataclasses.replace(e, descriptor_generation=generation) for e in entries]
    blob = b"".join(e.to_bytes() for e in entries)
    metadata = {
        "descriptor_generation": generation,
        "map_generation": MAP_GENERATION,
        "entry_count": len(entries),
        "layout_count": 1,
        "flags": 0,
        "entries_crc32": zlib.crc32(blob),
    }
    await send_rx(
        ctx, plane, INJ_TYPE_MAP_BEGIN, MapBeginPayload(**metadata).to_bytes(), sequence=1
    )
    for i, entry in enumerate(entries):
        await send_rx(ctx, plane, INJ_TYPE_MAP_ENTRY, entry.to_bytes(), sequence=2 + i)
    await send_rx(
        ctx,
        plane,
        INJ_TYPE_MAP_COMMIT,
        MapCommitPayload(**metadata).to_bytes(),
        sequence=2 + len(entries),
    )
    for _ in range(8_000):
        await ctx.tick("usb")
        if ctx.get(plane.map_store.commit_ack):
            assert ctx.get(plane.map_store.commit_error) == MapError.NONE
            assert ctx.get(plane.map_store.active_valid)
            return
    raise AssertionError("the firmware's compiled map did not commit")


needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")


@needs_cc
@pytest.mark.parametrize(
    ("fixture", "entry_count"),
    [
        ("HID_FIXTURE_BOOT_MOUSE", 3),  # X, Y, buttons 1-3
        ("HID_FIXTURE_ID_MOUSE_16BIT", 5),  # X, Y, wheel, AC Pan, buttons 1-5
        ("HID_FIXTURE_MOUSE_12BIT", 3),  # 12-bit X and Y straddling bytes, buttons 1-8
    ],
)
def test_firmware_compiled_map_commits(tmp_path: Path, fixture: str, entry_count: int) -> None:
    entries = _firmware_entries(tmp_path, fixture)
    assert len(entries) == entry_count

    async def bench(ctx, harness) -> None:
        await initialize(ctx, harness.plane)
        await _commit(ctx, harness, entries)
        assert ctx.get(harness.plane.map_store.active_generation) == MAP_GENERATION

    run_simulation(bench)


def _id_mouse_report(buttons: int, x: int, y: int, wheel: int) -> bytes:
    """Report ID 2: [ID, buttons, X lo, X hi, Y lo, Y hi, wheel, pan]."""
    return bytes(
        [
            0x02,
            buttons,
            *(x & 0xFFFF).to_bytes(2, "little"),
            *(y & 0xFFFF).to_bytes(2, "little"),
            wheel & 0xFF,
            0x00,
        ]
    )


@needs_cc
def test_relative_lands_in_the_16bit_axes_of_a_report_id_mouse(tmp_path: Path) -> None:
    # The whole point of compiling the map: a fixed boot map would have added X
    # to this report's buttons byte. Here +300 lands in the 16-bit X field.
    entries = _firmware_entries(tmp_path, "HID_FIXTURE_ID_MOUSE_16BIT")

    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        await _commit(ctx, harness, entries)
        rel = RelativePayload(
            lease_generation=MAP_GENERATION,
            map_generation=MAP_GENERATION,
            command_sequence=1,
            target_frame=0,
            interface_number=INTERFACE,
            endpoint_number=ENDPOINT,
            report_id=2,
            flags=INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y | INJ_RELATIVE_FLAG_WHEEL,
            x=300,
            y=-5,
            wheel=1,
            pan=0,
            hold_reports=0,
        )
        await send_rx(ctx, plane, INJ_TYPE_RELATIVE, rel.to_bytes(), sequence=0x40)

        native = _id_mouse_report(buttons=0, x=1000, y=-100, wheel=0)
        await push_report(ctx, plane, native, interface=INTERFACE, endpoint=ENDPOINT)
        emitted = await drain_output(ctx, plane, len(native))
        assert emitted == _id_mouse_report(buttons=0, x=1300, y=-105, wheel=1)

    run_simulation(bench)


@needs_cc
def test_injected_button_lands_after_the_report_id_byte(tmp_path: Path) -> None:
    # BUTTON_STATE bit 0 is button 1, which the compiled button entry places at
    # bit 8: the first bit AFTER the report ID.
    entries = _firmware_entries(tmp_path, "HID_FIXTURE_ID_MOUSE_16BIT")

    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        await _commit(ctx, harness, entries)
        state = ButtonStatePayload(
            lease_generation=MAP_GENERATION,
            map_generation=MAP_GENERATION,
            command_sequence=1,
            target_frame=0,
            interface_number=INTERFACE,
            endpoint_number=ENDPOINT,
            report_id=2,
            flags=0,
            buttons=0b1,
            hold_reports=0,
        )
        await send_rx(ctx, plane, INJ_TYPE_BUTTON_STATE, state.to_bytes(), sequence=0x40)

        native = _id_mouse_report(buttons=0, x=7, y=7, wheel=0)
        await push_report(ctx, plane, native, interface=INTERFACE, endpoint=ENDPOINT)
        emitted = await drain_output(ctx, plane, len(native))
        assert emitted == _id_mouse_report(buttons=0b1, x=7, y=7, wheel=0)

    run_simulation(bench)


@needs_cc
def test_a_mouse_on_interface_five_endpoint_twelve_is_injectable(tmp_path: Path) -> None:
    """A device need not number its interfaces 0..3, nor its endpoints 1..4.

    The gateware used to refuse an interface number of 4 or more in a map
    entry, and the MCU used to drop the descriptor of one, so a mouse on
    interface 5 could never be injected.
    """
    entries = _firmware_entries(tmp_path, "HID_FIXTURE_BOOT_MOUSE", interface=5, endpoint=12)
    assert {(e.interface_number, e.endpoint_number) for e in entries} == {(5, 12)}

    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        await _commit(ctx, harness, entries)
        rel = RelativePayload(
            lease_generation=MAP_GENERATION,
            map_generation=MAP_GENERATION,
            command_sequence=1,
            target_frame=0,
            interface_number=5,
            endpoint_number=12,
            report_id=0,
            flags=INJ_RELATIVE_FLAG_X,
            x=10,
            y=0,
            wheel=0,
            pan=0,
            hold_reports=0,
        )
        await send_rx(ctx, plane, INJ_TYPE_RELATIVE, rel.to_bytes(), sequence=0x40)
        native = bytes([0x00, 0x05, 0x00])
        await push_report(ctx, plane, native, interface=5, endpoint=12)
        assert await drain_output(ctx, plane, len(native)) == bytes([0x00, 0x0F, 0x00])

    run_simulation(bench)
