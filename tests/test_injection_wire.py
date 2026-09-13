import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from hurra_cynthion.injection_wire import (
    COUNTER_POSITIONS,
    CRC32_ALGORITHM,
    CRC32_INIT,
    CRC32_POLY,
    CRC32_XOROUT,
    ENUMERATIONS,
    FRAME_SIZE,
    LIMITS,
    MAP_ENTRY_GOLDEN_PAYLOAD,
    MAX_PAYLOAD,
    PAYLOAD_LAYOUTS,
    RELATIVE_GOLDEN_PAYLOAD,
    FrameError,
    MapEntryPayload,
    RelativePayload,
    SequenceDisposition,
    classify_sequence,
    crc16_ccitt_false,
    pack_slot,
    unpack_slot,
)

EXPECTED_LAYOUTS = {
    "IDLE": (("reserved", 0, 26),),
    "LINK_STATUS": (
        ("usb_frame", 0, 2),
        ("usb_subframe", 2, 1),
        ("link_flags", 3, 1),
        ("descriptor_generation", 4, 2),
        ("active_map_generation", 6, 2),
        ("slot_counter", 8, 4),
        ("native_report_count", 12, 4),
        ("fault_flags", 16, 4),
        ("last_rx_sequence", 20, 1),
        ("reserved", 21, 5),
    ),
    "DESCRIPTOR_FRAGMENT": (
        ("descriptor_generation", 0, 2),
        ("interface_number", 2, 1),
        ("reserved", 3, 1),
        ("offset", 4, 2),
        ("total", 6, 2),
        ("data", 8, 18),
    ),
    "REPORT_FRAGMENT": (
        ("descriptor_generation", 0, 2),
        ("interface_number", 2, 1),
        ("endpoint_number", 3, 1),
        ("report_id", 4, 1),
        ("offset", 5, 2),
        ("total", 7, 2),
        ("data", 9, 17),
    ),
    "MAP_STATUS": (
        ("descriptor_generation", 0, 2),
        ("map_generation", 2, 2),
        ("active_map_generation", 4, 2),
        ("entry_index", 6, 1),
        ("status", 7, 1),
        ("error", 8, 1),
        ("flags", 9, 1),
        ("entries_crc32", 10, 4),
        ("reserved", 14, 12),
    ),
    "COMMAND_ACK": (
        ("lease_generation", 0, 2),
        ("map_generation", 2, 2),
        ("command_sequence", 4, 2),
        ("target_frame", 6, 2),
        ("accepted_frame", 8, 2),
        ("completed_frame", 10, 2),
        ("ack_stage", 12, 1),
        ("result", 13, 1),
        ("command_type", 14, 1),
        ("flags", 15, 1),
        ("interface_number", 16, 1),
        ("endpoint_number", 17, 1),
        ("report_id", 18, 1),
        ("reserved", 19, 7),
    ),
    "COUNTERS": (
        ("page", 0, 1),
        ("flags", 1, 1),
        ("counter0", 2, 4),
        ("counter1", 6, 4),
        ("counter2", 10, 4),
        ("counter3", 14, 4),
        ("counter4", 18, 4),
        ("counter5", 22, 4),
    ),
    "MAP_BEGIN": (
        ("descriptor_generation", 0, 2),
        ("map_generation", 2, 2),
        ("entry_count", 4, 1),
        ("layout_count", 5, 1),
        ("flags", 6, 2),
        ("entries_crc32", 8, 4),
        ("reserved", 12, 14),
    ),
    "MAP_ENTRY": (
        ("descriptor_generation", 0, 2),
        ("map_generation", 2, 2),
        ("entry_index", 4, 1),
        ("interface_number", 5, 1),
        ("endpoint_number", 6, 1),
        ("report_id", 7, 1),
        ("usage_page", 8, 2),
        ("usage", 10, 2),
        ("bit_offset", 12, 2),
        ("bit_width", 14, 1),
        ("flags", 15, 1),
        ("logical_minimum", 16, 4),
        ("logical_maximum", 20, 4),
        ("report_length", 24, 1),
        ("reserved", 25, 1),
    ),
    "MAP_COMMIT": (
        ("descriptor_generation", 0, 2),
        ("map_generation", 2, 2),
        ("entry_count", 4, 1),
        ("layout_count", 5, 1),
        ("flags", 6, 2),
        ("entries_crc32", 8, 4),
        ("reserved", 12, 14),
    ),
    "RELATIVE": (
        ("lease_generation", 0, 2),
        ("map_generation", 2, 2),
        ("command_sequence", 4, 2),
        ("target_frame", 6, 2),
        ("interface_number", 8, 1),
        ("endpoint_number", 9, 1),
        ("report_id", 10, 1),
        ("flags", 11, 1),
        ("x", 12, 2),
        ("y", 14, 2),
        ("wheel", 16, 2),
        ("pan", 18, 2),
        ("hold_reports", 20, 2),
        ("reserved", 22, 4),
    ),
    "BUTTON_STATE": (
        ("lease_generation", 0, 2),
        ("map_generation", 2, 2),
        ("command_sequence", 4, 2),
        ("target_frame", 6, 2),
        ("interface_number", 8, 1),
        ("endpoint_number", 9, 1),
        ("report_id", 10, 1),
        ("flags", 11, 1),
        ("buttons", 12, 8),
        ("hold_reports", 20, 2),
        ("reserved", 22, 4),
    ),
    "PHYSICAL_MASK": (
        ("lease_generation", 0, 2),
        ("map_generation", 2, 2),
        ("command_sequence", 4, 2),
        ("target_frame", 6, 2),
        ("interface_number", 8, 1),
        ("endpoint_number", 9, 1),
        ("report_id", 10, 1),
        ("flags", 11, 1),
        ("button_mask", 12, 8),
        ("reserved", 20, 6),
    ),
    "CLEAR": (
        ("lease_generation", 0, 2),
        ("map_generation", 2, 2),
        ("command_sequence", 4, 2),
        ("target_frame", 6, 2),
        ("clear_flags", 8, 2),
        ("reason", 10, 1),
        ("reserved", 11, 15),
    ),
    "TELEMETRY_CONFIG": (
        ("lease_generation", 0, 2),
        ("command_sequence", 2, 2),
        ("telemetry_mask", 4, 4),
        ("interval_slots", 8, 2),
        ("report_sample_divisor", 10, 2),
        ("counter_page", 12, 1),
        ("flags", 13, 1),
        ("reserved", 14, 12),
    ),
}

EXPECTED_ENUMERATIONS = {
    "CLEAR_FLAG": {"MOTION": 1, "BUTTONS": 2, "PHYSICAL_MASKS": 4, "QUEUED_TIMED": 8, "ALL": 15},
    "COMMAND_ACK_FLAG": {"LATE": 1, "SYNTHESIZED": 2},
    "COMMAND_ACK_RESULT": {
        "SUCCESS": 0,
        "STALE_LEASE": 1,
        "STALE_MAP": 2,
        "BUSY": 3,
        "OVERFLOW": 4,
        "UNSUPPORTED_TARGET": 5,
        "CLEARED": 6,
    },
    "COMMAND_ACK_STAGE": {"ADMITTED": 0, "COMPLETED": 1, "REJECTED": 2},
    "COUNTERS_PAGE": {"LINK": 0, "COMMAND": 1, "REPORT_MAP": 2},
    "LINK_STATUS_FLAG": {
        "ENUMERATED": 1,
        "MAP_ACTIVE": 2,
        "INJECTION_ENABLED": 4,
        "RELAY_READY": 8,
    },
    "MAP_ENTRY_FLAG": {
        "SIGNED": 1,
        "RELATIVE": 2,
        "BUTTON": 4,
        "X": 8,
        "Y": 16,
        "WHEEL": 32,
        "PAN": 64,
    },
    "MAP_STATUS_ERROR": {
        "NONE": 0,
        "DESCRIPTOR_GENERATION": 1,
        "MAP_GENERATION": 2,
        "ENTRY_COUNT": 3,
        "LAYOUT_COUNT": 4,
        "CRC": 5,
        "INTERFACE": 6,
        "ENDPOINT": 7,
        "REPORT_LENGTH": 8,
        "FIELD_WIDTH": 9,
        "BIT_OFFSET": 10,
        "REPORT_ID_PREFIX": 11,
        "OVERLAP": 12,
        "UNSUPPORTED_FIELD": 13,
        "DUPLICATE_ENTRY": 14,
        "INTERNAL": 15,
    },
    "MAP_STATUS_STATUS": {
        "CANDIDATE_ACCEPTED": 0,
        "COMMIT_ACCEPTED": 1,
        "REJECTED": 2,
        "ABORTED": 3,
    },
    "RELATIVE_FLAG": {"X": 1, "Y": 2, "WHEEL": 4, "PAN": 8},
}

EXPECTED_COUNTER_POSITIONS = {
    "LINK": {
        "BAD_SOF": 0,
        "BAD_CRC": 1,
        "BAD_LENGTH": 2,
        "BAD_TYPE": 3,
        "SEQUENCE_GAP": 4,
        "DUPLICATE": 5,
    },
    "COMMAND": {
        "ACCEPTED": 0,
        "REJECTED": 1,
        "LATE": 2,
        "QUEUE_FULL": 3,
        "COMPLETED": 4,
        "OVERFLOW": 5,
    },
    "REPORT_MAP": {
        "NATIVE": 0,
        "MUTATED": 1,
        "SYNTHESIZED": 2,
        "MONITORING_DROP": 3,
        "MAP_COMMIT": 4,
        "MAP_REJECT": 5,
    },
}


def test_idle_golden_slot():
    slot = pack_slot(0x00, 0x2A, b"")
    assert len(slot) == 32
    assert slot[:4] == bytes.fromhex("68 00 2a 00")
    assert slot[4:30] == bytes(26)
    assert slot.hex() == "68002a00" + "00" * 26 + "5399"


def test_rejects_corruption_and_oversize():
    assert FRAME_SIZE == 32
    assert MAX_PAYLOAD == 26
    with pytest.raises(ValueError):
        pack_slot(0x84, 0, bytes(27))
    damaged = bytearray(pack_slot(0x84, 1, bytes(26)))
    damaged[9] ^= 0x80
    with pytest.raises(FrameError, match="crc"):
        unpack_slot(bytes(damaged))


def test_pack_slot_rejects_unknown_or_wrong_known_payload_lengths():
    with pytest.raises(ValueError, match="type"):
        pack_slot(0x7F, 0, bytes(26))
    with pytest.raises(ValueError, match="length"):
        pack_slot(0x00, 0, b"\x00")
    with pytest.raises(ValueError, match="length"):
        pack_slot(0x84, 0, b"\x00")


@pytest.mark.parametrize(
    ("type_", "offset", "invalid", "receive_reject"),
    [
        (0x01, 3, 0x10, False),
        (0x04, 9, 0x01, False),
        (0x05, 15, 0x04, False),
        (0x06, 1, 0x01, False),
        (0x81, 6, 0x01, False),
        (0x82, 15, 0x80, True),
        (0x83, 6, 0x01, False),
        (0x84, 11, 0x10, False),
        (0x85, 11, 0x01, False),
        (0x86, 11, 0x01, False),
        (0x87, 8, 0x10, False),
        (0x88, 13, 0x01, False),
    ],
)
def test_raw_slot_transmit_rejects_unassigned_flags(type_, offset, invalid, receive_reject):
    payload = bytearray(26)
    payload[offset] = invalid
    with pytest.raises(ValueError, match="flags"):
        pack_slot(type_, 0, bytes(payload))
    slot = bytearray(32)
    slot[:4] = bytes((0x68, type_, 0, 26))
    slot[4:30] = payload
    slot[-2:] = crc16_ccitt_false(slot[:-2]).to_bytes(2, "little")
    if receive_reject:
        with pytest.raises(FrameError, match="flags"):
            unpack_slot(bytes(slot))
    else:
        assert unpack_slot(bytes(slot)).payload == bytes(payload)


def test_payload_layouts_are_complete_and_match_c_offsets():
    assert PAYLOAD_LAYOUTS == EXPECTED_LAYOUTS
    header = Path("firmware/ch32h417/include/injection_wire.h").read_text()
    for payload_name, fields in EXPECTED_LAYOUTS.items():
        assert max(offset + size for _, offset, size in fields) == MAX_PAYLOAD
        for field_name, offset, _ in fields:
            if field_name != "reserved":
                macro = f"INJ_{payload_name}_{field_name.upper()}_OFFSET"
                assert f"#define {macro} {offset}u" in header
                # The offset macros are emitted from field['offset']; the packed
                # structs are emitted from the field list order. Two independent
                # derivations of one layout - assert the generator ties them
                # together, so a struct-only reorder fails at compile time in the
                # C-only gate rather than only in this file.
                struct = f"inj_{payload_name.lower()}_payload_t"
                assert f"_Static_assert(offsetof({struct}, {field_name}) == {macro}" in header


def test_generated_header_asserts_a_little_endian_host():
    # Every payload struct is packed and aliases wire bytes directly, so it is
    # only correct on a little-endian host. Nothing stated that before.
    header = Path("firmware/ch32h417/include/injection_wire.h").read_text()
    assert "__ORDER_LITTLE_ENDIAN__" in header
    assert "packed payload structs assume a little-endian host" in header


def test_flag_status_and_crc32_assignments_match_c_constants():
    assert (CRC32_ALGORITHM, CRC32_INIT, CRC32_POLY, CRC32_XOROUT) == (
        "CRC-32/IEEE",
        0xFFFFFFFF,
        0xEDB88320,
        0xFFFFFFFF,
    )
    assert ENUMERATIONS == EXPECTED_ENUMERATIONS
    header = Path("firmware/ch32h417/include/injection_wire.h").read_text()
    for group, values in EXPECTED_ENUMERATIONS.items():
        for name, value in values.items():
            assert f"#define INJ_{group}_{name} {value}u" in header


def test_operational_limits_and_counter_positions_match_c_constants():
    assert LIMITS == {
        "interfaces": 4,
        "report_bytes": 64,
        "descriptor_bytes_per_interface": 2048,
        "layouts": 16,
        "fields": 64,
    }
    assert COUNTER_POSITIONS == EXPECTED_COUNTER_POSITIONS
    header = Path("firmware/ch32h417/include/injection_wire.h").read_text()
    for name, value in LIMITS.items():
        assert f"#define INJ_MAX_{name.upper()} {value}u" in header
    for page, counters in EXPECTED_COUNTER_POSITIONS.items():
        for name, index in counters.items():
            assert f"#define INJ_COUNTER_{page}_{name} {index}u" in header


def test_sequence_classification_covers_every_delta():
    assert classify_sequence(255, 0) == (SequenceDisposition.NEXT, 0)
    assert classify_sequence(4, 4) == (SequenceDisposition.DUPLICATE, 0)
    assert classify_sequence(4, 8) == (SequenceDisposition.GAP, 3)
    assert classify_sequence(4, 3) == (SequenceDisposition.STALE, 0)

    def expected(delta: int) -> tuple[SequenceDisposition, int]:
        if delta == 0:
            return SequenceDisposition.DUPLICATE, 0
        if delta == 1:
            return SequenceDisposition.NEXT, 0
        if delta < 0x80:
            return SequenceDisposition.GAP, delta - 1
        return SequenceDisposition.STALE, 0

    # The rule is a pure function of delta, so the result must be identical from
    # every starting point. Half the space (0x80..0xFF) is stale, never a gap.
    for delta in range(256):
        results = {
            classify_sequence(previous, (previous + delta) & 0xFF) for previous in range(256)
        }
        assert results == {expected(delta)}, f"delta 0x{delta:02x}"


def test_relative_golden_payload():
    payload = RelativePayload(
        lease_generation=0x1122,
        map_generation=0x3344,
        command_sequence=0x5566,
        target_frame=0x7788,
        interface_number=9,
        endpoint_number=10,
        report_id=11,
        flags=0x0F,
        x=-2,
        y=0x1234,
        wheel=-0x1234,
        pan=0x7FFF,
        hold_reports=0x9ABC,
    ).to_bytes()
    assert payload == RELATIVE_GOLDEN_PAYLOAD
    assert payload.hex() == "2211443366558877090a0b0ffeff3412ccedff7fbc9a00000000"


def test_structured_pack_rejects_unassigned_flags():
    with pytest.raises(ValueError, match="flags"):
        RelativePayload(
            lease_generation=0,
            map_generation=0,
            command_sequence=0,
            target_frame=0,
            interface_number=0,
            endpoint_number=0,
            report_id=0,
            flags=0x10,
            x=0,
            y=0,
            wheel=0,
            pan=0,
            hold_reports=0,
        ).to_bytes()


def test_map_entry_golden_payload():
    payload = MapEntryPayload(
        descriptor_generation=0x1234,
        map_generation=0x5678,
        entry_index=0x9A,
        interface_number=2,
        endpoint_number=3,
        report_id=1,
        usage_page=1,
        usage=0x30,
        bit_offset=8,
        bit_width=12,
        flags=0x1B,
        logical_minimum=-2048,
        logical_maximum=2047,
        report_length=4,
    ).to_bytes()
    assert payload == MAP_ENTRY_GOLDEN_PAYLOAD
    assert payload.hex() == "341278569a0203010100300008000c1b00f8ffffff0700000400"


def test_generated_c_header_is_current():
    expected = Path("firmware/ch32h417/include/injection_wire.h").read_text()
    generator_path = Path("tools/generate_report_injection_wire.py")
    spec = importlib.util.spec_from_file_location("generate_report_injection_wire", generator_path)
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)

    assert expected == generator.render_c(Path("protocol/report_injection_wire.json"))


def test_second_generation_does_not_change_outputs():
    generated = (
        Path("src/hurra_cynthion/injection_wire.py"),
        Path("firmware/ch32h417/include/injection_wire.h"),
    )
    before = {path: path.read_bytes() for path in generated}
    subprocess.run(
        [sys.executable, "tools/generate_report_injection_wire.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert {path: path.read_bytes() for path in generated} == before
