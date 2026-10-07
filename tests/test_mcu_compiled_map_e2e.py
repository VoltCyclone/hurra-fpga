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
from _ds4_fixture import DS4_REPORT_DESCRIPTOR, DS4_REPORT_DESCRIPTOR_CAPTURED
from test_injection_e2e import (
    drain_output,
    initialize,
    push_report,
    run_simulation,
    send_rx,
)

from hurra_cynthion.injection_map import MapError
from hurra_cynthion.injection_wire import (
    INJ_ABSOLUTE_FLAG_HAT,
    INJ_ABSOLUTE_FLAG_LT,
    INJ_ABSOLUTE_FLAG_LX,
    INJ_MAP_ENTRY_CHANNEL_HAT,
    INJ_MAP_ENTRY_CHANNEL_LT,
    INJ_MAP_ENTRY_CHANNEL_LX,
    INJ_MAP_ENTRY_CHANNEL_LY,
    INJ_MAP_ENTRY_CHANNEL_RT,
    INJ_MAP_ENTRY_CHANNEL_RX,
    INJ_MAP_ENTRY_CHANNEL_RY,
    INJ_MAP_ENTRY_FLAG_BUTTON,
    INJ_MAP_ENTRY_FLAG_RELATIVE,
    INJ_MAP_FLAG_NATIVE_ONLY,
    INJ_RELATIVE_FLAG_WHEEL,
    INJ_RELATIVE_FLAG_X,
    INJ_RELATIVE_FLAG_Y,
    INJ_TYPE_ABSOLUTE,
    INJ_TYPE_BUTTON_STATE,
    INJ_TYPE_MAP_BEGIN,
    INJ_TYPE_MAP_COMMIT,
    INJ_TYPE_MAP_ENTRY,
    INJ_TYPE_RELATIVE,
    AbsolutePayload,
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
    "hid_pad_layout.c",
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
PAD_INTERFACE = 3
PAD_ENDPOINT = 4

_HARNESS = textwrap.dedent(
    """
    #include <stdio.h>

    #include "hid_fixtures.h"
    #include "hid_mouse_layout.h"
    #include "hid_pad_layout.h"
    #include "inj_map_build.h"

    int main(void)
    {
        hid_layout_t layout;
    #if PAD_HARNESS
        if (hid_pad_compile(FIXTURE, sizeof(FIXTURE), &layout) != HID_PAD_OK) {
            return 1;
        }
    #else
        if (hid_mouse_compile(FIXTURE, sizeof(FIXTURE), &layout) != HID_MOUSE_OK) {
            return 1;
        }
    #endif
        const inj_map_target_t target = {
            .descriptor_generation = 0u,
            .map_generation = %(map_generation)du,
            .interface_number = %(interface)du,
            .endpoint_number = %(endpoint)du,
            .report_length = layout.report_length,
        };
        inj_map_entry_payload_t entries[HID_LAYOUT_MAX_FIELDS];
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

# Prints one fixture's bytes, so the C and Python copies can be held equal.
_FIXTURE_DUMP = textwrap.dedent(
    """
    #include <stdio.h>

    #include "hid_fixtures.h"

    int main(void)
    {
        for (size_t i = 0u; i < sizeof(FIXTURE); ++i) {
            printf("%02x", FIXTURE[i]);
        }
        printf("\\n");
        return 0;
    }
    """
)


def _compile_and_run(tmp_path: Path, source: str, fixture: str, *defines: str) -> str:
    harness = tmp_path / "harness.c"
    harness.write_text(source)
    binary = tmp_path / "harness"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            *HOST_WARNINGS,
            f"-DFIXTURE={fixture}",
            *defines,
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
    return subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout


def _firmware_entries(
    tmp_path: Path,
    fixture: str,
    *,
    interface: int = INTERFACE,
    endpoint: int = ENDPOINT,
    pad: bool = False,
) -> list[MapEntryPayload]:
    """Compile the firmware's map for ``fixture`` and return its MAP_ENTRY payloads."""
    source = _HARNESS % {
        "map_generation": MAP_GENERATION,
        "interface": interface,
        "endpoint": endpoint,
    }
    out = _compile_and_run(tmp_path, source, fixture, f"-DPAD_HARNESS={int(pad)}")
    return [MapEntryPayload.from_bytes(bytes.fromhex(line)) for line in out.split()]


def _c_fixture_bytes(tmp_path: Path, fixture: str) -> bytes:
    return bytes.fromhex(_compile_and_run(tmp_path, _FIXTURE_DUMP, fixture).strip())


async def _commit(ctx, harness, entries: list[MapEntryPayload], flags: int = 0) -> None:
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
        "flags": flags,
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
            assert ctx.get(plane.map_store.active_native_only) == (
                1 if flags & INJ_MAP_FLAG_NATIVE_ONLY else 0
            )
            return
    raise AssertionError("the firmware's compiled map did not commit")


needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")

# The two pad fixtures: the synthetic DS4-shaped one always, the real capture
# once it has landed (tests/_ds4_fixture.py and test/hid_fixtures.h together).
PAD_FIXTURES = [
    "HID_FIXTURE_GAMEPAD_DS4_SHAPED",
    pytest.param(
        "HID_FIXTURE_DS4",
        marks=pytest.mark.skipif(
            not DS4_REPORT_DESCRIPTOR_CAPTURED, reason="DS4 report descriptor not captured yet"
        ),
    ),
]


def _pad_report(
    *, lx: int, ly: int, rx: int, ry: int, hat: int, buttons: int, lt: int, rt: int
) -> bytes:
    """Report ID 1, 64 bytes, in the DS4-shaped fixture's layout.

    byte 1..4 sticks; byte 5 = hat (low nibble) | buttons 1-4 (high nibble);
    byte 6 = buttons 5-12; byte 7 = buttons 13-14 (bits 0-1) | counter (bits 2-7);
    bytes 8-9 triggers; bytes 10-63 vendor.
    """
    body = bytes(
        [
            0x01,
            lx,
            ly,
            rx,
            ry,
            (hat & 0x0F) | ((buttons & 0x0F) << 4),
            (buttons >> 4) & 0xFF,
            ((buttons >> 12) & 0x03) | (0x0F << 2),  # counter 15
            lt,
            rt,
        ]
    )
    return body + bytes(range(54))


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


@needs_cc
def test_c_and_python_ds4_fixtures_are_the_same_bytes(tmp_path: Path) -> None:
    # Both copies of the capture (or both placeholders) must agree, or the e2e
    # would validate a descriptor the firmware never sees.
    assert len(DS4_REPORT_DESCRIPTOR) == 507
    assert _c_fixture_bytes(tmp_path, "HID_FIXTURE_DS4") == DS4_REPORT_DESCRIPTOR


@needs_cc
@pytest.mark.parametrize("fixture", PAD_FIXTURES)
def test_firmware_pad_map_is_absolute_with_channels_and_commits_native_only(
    tmp_path: Path, fixture: str
) -> None:
    entries = _firmware_entries(
        tmp_path, fixture, interface=PAD_INTERFACE, endpoint=PAD_ENDPOINT, pad=True
    )
    assert len(entries) == 8  # LX LY RX RY LT RT HAT, buttons 1-14
    axes = [e for e in entries if not e.flags & INJ_MAP_ENTRY_FLAG_BUTTON]
    assert [e.channel for e in axes] == [
        INJ_MAP_ENTRY_CHANNEL_LX,
        INJ_MAP_ENTRY_CHANNEL_LY,
        INJ_MAP_ENTRY_CHANNEL_RX,
        INJ_MAP_ENTRY_CHANNEL_RY,
        INJ_MAP_ENTRY_CHANNEL_LT,
        INJ_MAP_ENTRY_CHANNEL_RT,
        INJ_MAP_ENTRY_CHANNEL_HAT,
    ]
    assert all(e.flags == 0 for e in axes)  # unsigned, neither RELATIVE nor BUTTON
    assert not any(e.flags & INJ_MAP_ENTRY_FLAG_RELATIVE for e in entries)
    buttons = entries[7]
    assert buttons.flags == INJ_MAP_ENTRY_FLAG_BUTTON and buttons.channel == 0
    assert buttons.bit_offset == 44 and buttons.bit_width == 14
    assert {(e.report_id, e.report_length) for e in entries} == {(1, 64)}
    # channel survives the wire round trip byte-exactly.
    assert all(MapEntryPayload.from_bytes(e.to_bytes()) == e for e in entries)

    async def bench(ctx, harness) -> None:
        await initialize(ctx, harness.plane)
        await _commit(ctx, harness, entries, flags=INJ_MAP_FLAG_NATIVE_ONLY)
        assert ctx.get(harness.plane.map_store.active_generation) == MAP_GENERATION
        assert ctx.get(harness.plane.map_store.active_native_only) == 1

    run_simulation(bench)


@needs_cc
def test_absolute_sets_a_still_pad_and_the_next_native_carries_it(tmp_path: Path) -> None:
    """The pad equivalent of the still-mouse case, on a NATIVE_ONLY map.

    No native report between the commit and the ABSOLUTE: the command commits
    to the layout's state and acks with nothing on the wire. The pad's next
    report carries LX = 200, LT = 255 and the hat's null 8 (which a clamp into
    0..7 would have turned into "up-left"); LY and the released channels pass
    the physical value through.
    """
    entries = _firmware_entries(
        tmp_path,
        "HID_FIXTURE_GAMEPAD_DS4_SHAPED",
        interface=PAD_INTERFACE,
        endpoint=PAD_ENDPOINT,
        pad=True,
    )

    async def bench(ctx, harness) -> None:
        plane = harness.plane
        await initialize(ctx, plane)
        await _commit(ctx, harness, entries, flags=INJ_MAP_FLAG_NATIVE_ONLY)

        held = INJ_ABSOLUTE_FLAG_LX | INJ_ABSOLUTE_FLAG_LT | INJ_ABSOLUTE_FLAG_HAT
        command = AbsolutePayload(
            lease_generation=MAP_GENERATION,
            map_generation=MAP_GENERATION,
            command_sequence=1,
            hold_reports=0,
            interface_number=PAD_INTERFACE,
            endpoint_number=PAD_ENDPOINT,
            report_id=1,
            flags=held,
            lx=200,
            ly=0,
            rx=0,
            ry=0,
            lt=255,
            rt=0,
            hat=8,
        )
        await send_rx(ctx, plane, INJ_TYPE_ABSOLUTE, command.to_bytes(), sequence=0x40)

        # The stationary scan runs on the frame tick; NATIVE_ONLY commits the
        # held set without synthesising a report.
        ctx.set(plane.sof_tick, 1)
        await ctx.tick("usb")
        ctx.set(plane.sof_tick, 0)
        for _ in range(20_000):
            assert not ctx.get(plane.output_valid), "a report was offered on a NATIVE_ONLY map"
            if ctx.get(plane.engine.held_mask) == held:
                break
            await ctx.tick("usb")
        assert ctx.get(plane.engine.held_mask) == held
        assert not ctx.get(plane.engine.absolute_valid)  # consumed: the staging is free
        assert ctx.get(plane.synthesized_report_count) == 0  # nothing on the wire
        assert ctx.get(plane.command_commit_count) == 1

        native = _pad_report(lx=0x80, ly=0x80, rx=0x80, ry=0x80, hat=3, buttons=0x5, lt=0, rt=0)
        await push_report(ctx, plane, native, interface=PAD_INTERFACE, endpoint=PAD_ENDPOINT)
        emitted = await drain_output(ctx, plane, len(native))

        expected = bytearray(native)
        expected[1] = 200  # LX held
        expected[5] = (expected[5] & 0xF0) | 8  # hat held at null, buttons 1-4 untouched
        expected[8] = 255  # LT held
        assert emitted == bytes(expected)  # LY/RX/RY/RT and bytes 10..63 pass through

        # A redundant ABSOLUTE is acked (never wedges the one-deep staging).
        again = dataclasses.replace(command, command_sequence=2)
        await send_rx(ctx, plane, INJ_TYPE_ABSOLUTE, again.to_bytes(), sequence=0x41)
        ctx.set(plane.sof_tick, 1)
        await ctx.tick("usb")
        ctx.set(plane.sof_tick, 0)
        for _ in range(20_000):
            assert not ctx.get(plane.output_valid), "a report was offered on a NATIVE_ONLY map"
            if ctx.get(plane.command_commit_count) == 2:
                break
            await ctx.tick("usb")
        assert ctx.get(plane.command_commit_count) == 2
        assert ctx.get(plane.synthesized_report_count) == 0

    run_simulation(bench)
