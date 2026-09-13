#!/usr/bin/env python3
"""Generate the versioned report-injection wire contract from its JSON schema."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "protocol/report_injection_wire.json"
PYTHON_PATH = ROOT / "src/hurra_cynthion/injection_wire.py"
C_HEADER_PATH = ROOT / "firmware/ch32h417/include/injection_wire.h"

TYPE_INFO = {
    "u8": (1, "uint8_t", "int"),
    "u16": (2, "uint16_t", "int"),
    "u32": (4, "uint32_t", "int"),
    "u64": (8, "uint64_t", "int"),
    "i16": (2, "int16_t", "int"),
    "i32": (4, "int32_t", "int"),
}


def _size(field: dict[str, object]) -> int:
    if field["type"] == "bytes":
        return int(field["count"])
    return TYPE_INFO[str(field["type"])][0]


def _class_name(payload_name: str) -> str:
    return "".join(part.title() for part in payload_name.split("_")) + "Payload"


def _schema(path: Path) -> dict[str, object]:
    schema = json.loads(path.read_text())
    payload_size = schema["frame"]["payload_size"]
    for name, fields in schema["payloads"].items():
        ordered = sorted(fields, key=lambda field: field["offset"])
        if ordered != fields or max(field["offset"] + _size(field) for field in fields) != payload_size:
            raise ValueError(f"{name} does not fill one payload in offset order")
    return schema


def _golden_payload(schema: dict[str, object], payload_name: str, values: dict[str, object]) -> bytes:
    payload = bytearray(schema["frame"]["payload_size"])
    for field in schema["payloads"][payload_name]:
        if field.get("reserved"):
            continue
        value = values[field["name"]]
        size = _size(field)
        if field["type"] == "bytes":
            if len(value) != size:
                raise ValueError(f"golden {payload_name}.{field['name']} has wrong size")
            payload[field["offset"] : field["offset"] + size] = value
        else:
            payload[field["offset"] : field["offset"] + size] = int(value).to_bytes(
                size, "little", signed=str(field["type"]).startswith("i")
            )
    return bytes(payload)


def render_python(path: Path) -> str:
    schema = _schema(path)
    frame = schema["frame"]
    crc32 = schema["crc32"]
    limits = schema["limits"]
    counter_positions = schema["counter_positions"]
    enumerations = schema["enumerations"]
    field_masks = schema["field_masks"]
    receive_reject_masks = schema["receive_reject_masks"]
    payloads = schema["payloads"]
    message_types = schema["message_types"]
    lines = [
        '"""Generated report-injection wire contract; do not edit by hand."""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from enum import Enum",
        "",
        f"SOF = {frame['sof']}",
        f"FRAME_SIZE = {frame['size']}",
        f"HEADER_SIZE = {frame['header_size']}",
        f"MAX_PAYLOAD = {frame['payload_size']}",
        f"CRC_INIT = 0x{frame['crc_init']:04X}",
        f"CRC_POLY = 0x{frame['crc_poly']:04X}",
        f"CRC32_ALGORITHM = {crc32['algorithm']!r}",
        f"CRC32_INIT = 0x{crc32['init']:08X}",
        f"CRC32_POLY = 0x{crc32['poly']:08X}",
        f"CRC32_XOROUT = 0x{crc32['xorout']:08X}",
        "",
    ]
    for name, value in sorted(message_types.items(), key=lambda item: item[1]):
        lines.append(f"INJ_TYPE_{name} = 0x{value:02X}")
    lines += ["", "MESSAGE_TYPES = {"]
    for name, value in sorted(message_types.items(), key=lambda item: item[1]):
        lines.append(f"    {name!r}: INJ_TYPE_{name},")
    lines += ["}", "MESSAGE_NAMES = {value: name for name, value in MESSAGE_TYPES.items()}", ""]
    lines += ["LIMITS = {"]
    for name, value in sorted(limits.items()):
        lines.append(f"    {name!r}: {value},")
    lines += ["}", "COUNTER_POSITIONS = {"]
    for page in sorted(counter_positions):
        lines.append(f"    {page!r}: {{")
        for name, value in sorted(counter_positions[page].items(), key=lambda item: item[1]):
            lines.append(f"        {name!r}: {value},")
        lines.append("    },")
    lines += ["}", "FIELD_ALLOWED_MASKS = {"]
    for payload_name in sorted(field_masks):
        lines.append(f"    {payload_name!r}: {{")
        fields = {field["name"]: field for field in payloads[payload_name]}
        for field_name, mask in sorted(field_masks[payload_name].items()):
            field = fields[field_name]
            lines.append(f"        {field_name!r}: ({field['offset']}, {_size(field)}, {mask}),")
        lines.append("    },")
    lines += ["}", ""]
    lines += ["RECEIVE_REJECT_MASKS = {"]
    for payload_name in sorted(receive_reject_masks):
        lines.append(f"    {payload_name!r}: {{")
        fields = {field["name"]: field for field in payloads[payload_name]}
        for field_name, mask in sorted(receive_reject_masks[payload_name].items()):
            field = fields[field_name]
            lines.append(f"        {field_name!r}: ({field['offset']}, {_size(field)}, {mask}),")
        lines.append("    },")
    lines += ["}", ""]
    lines += ["ENUMERATIONS = {"]
    for group in sorted(enumerations):
        lines.append(f"    {group!r}: {{")
        for name, value in sorted(enumerations[group].items(), key=lambda item: item[1]):
            lines.append(f"        {name!r}: {value},")
        lines.append("    },")
    lines += ["}", ""]
    for group in sorted(enumerations):
        for name, value in sorted(enumerations[group].items(), key=lambda item: item[1]):
            lines.append(f"INJ_{group}_{name} = {value}")
    lines += [""]
    for payload_name in sorted(payloads):
        for field in payloads[payload_name]:
            if not field.get("reserved"):
                lines.append(
                    f"INJ_{payload_name}_{field['name'].upper()}_OFFSET = {field['offset']}"
                )
    lines += ["", "PAYLOAD_LAYOUTS = {"]
    for payload_name in sorted(payloads):
        lines.append(f"    {payload_name!r}: (")
        for field in payloads[payload_name]:
            lines.append(f"        ({field['name']!r}, {field['offset']}, {_size(field)}),")
        lines.append("    ),")
    lines += ["}", "_PAYLOAD_FIELDS = {"]
    for payload_name in sorted(payloads):
        lines.append(f"    {payload_name!r}: (")
        for field in payloads[payload_name]:
            lines.append(
                f"        ({field['name']!r}, {field['offset']}, {field['type']!r}, "
                f"{_size(field)}, {bool(field.get('reserved'))}),"
            )
        lines.append("    ),")
    lines += ["}", ""]
    for payload_name in sorted(payloads):
        fields = [field for field in payloads[payload_name] if not field.get("reserved")]
        class_name = _class_name(payload_name)
        lines += ["@dataclass(frozen=True)", f"class {class_name}:"]
        if fields:
            for field in fields:
                annotation = "bytes" if field["type"] == "bytes" else "int"
                lines.append(f"    {field['name']}: {annotation}")
        else:
            lines.append("    pass")
        lines += [
            "",
            "    def to_bytes(self) -> bytes:",
            f"        return _pack_payload({payload_name!r}, self.__dict__)",
            "",
            "    @classmethod",
            f"    def from_bytes(cls, payload: bytes) -> {class_name}:",
            f"        return cls(**_unpack_payload({payload_name!r}, payload))",
            "",
        ]
    relative = _golden_payload(schema, "RELATIVE", schema["goldens"]["relative"])
    map_entry = _golden_payload(schema, "MAP_ENTRY", schema["goldens"]["map_entry"])
    lines += [
        f"RELATIVE_GOLDEN_PAYLOAD = bytes.fromhex({relative.hex()!r})",
        f"MAP_ENTRY_GOLDEN_PAYLOAD = bytes.fromhex({map_entry.hex()!r})",
        "",
        "class FrameError(ValueError):",
        '    """A malformed or unsupported slot."""',
        "",
        "",
        "@dataclass(frozen=True)",
        "class Frame:",
        "    type_: int",
        "    seq: int",
        "    payload: bytes",
        "",
        "",
        "def _store_u8(buffer: bytearray, offset: int, value: int) -> None:",
        "    buffer[offset] = value",
        "",
        "",
        "def _store_u16(buffer: bytearray, offset: int, value: int) -> None:",
        "    buffer[offset : offset + 2] = value.to_bytes(2, 'little')",
        "",
        "",
        "def _store_u32(buffer: bytearray, offset: int, value: int) -> None:",
        "    buffer[offset : offset + 4] = value.to_bytes(4, 'little')",
        "",
        "",
        "def _store_u64(buffer: bytearray, offset: int, value: int) -> None:",
        "    buffer[offset : offset + 8] = value.to_bytes(8, 'little')",
        "",
        "",
        "def _load_u8(payload: bytes, offset: int) -> int:",
        "    return payload[offset]",
        "",
        "",
        "def _load_u16(payload: bytes, offset: int) -> int:",
        "    return int.from_bytes(payload[offset : offset + 2], 'little')",
        "",
        "",
        "def _load_u32(payload: bytes, offset: int) -> int:",
        "    return int.from_bytes(payload[offset : offset + 4], 'little')",
        "",
        "",
        "def _load_u64(payload: bytes, offset: int) -> int:",
        "    return int.from_bytes(payload[offset : offset + 8], 'little')",
        "",
        "",
        "def _store_int(buffer: bytearray, offset: int, size: int, value: int, signed: bool) -> None:",
        "    minimum = -(1 << (size * 8 - 1)) if signed else 0",
        "    maximum = (1 << (size * 8 - (1 if signed else 0))) - 1",
        "    if not minimum <= value <= maximum:",
        "        raise ValueError(f'integer {value} does not fit in {size * 8} bits')",
        "    buffer[offset : offset + size] = value.to_bytes(size, 'little', signed=signed)",
        "",
        "",
        "def _pack_payload(name: str, values: dict[str, object]) -> bytes:",
        "    try:",
        "        fields = _PAYLOAD_FIELDS[name]",
        "    except KeyError as error:",
        "        raise ValueError(f'unknown payload {name}') from error",
        "    payload = bytearray(MAX_PAYLOAD)",
        "    for field_name, offset, kind, size, reserved in fields:",
        "        if reserved:",
        "            continue",
        "        value = values[field_name]",
        "        if kind == 'bytes':",
        "            if not isinstance(value, bytes) or len(value) != size:",
        "                raise ValueError(f'{name}.{field_name} must be {size} bytes')",
        "            payload[offset : offset + size] = value",
        "        else:",
        "            _store_int(payload, offset, size, int(value), kind.startswith('i'))",
        "    encoded = bytes(payload)",
        "    _validate_transmit_payload(name, encoded)",
        "    return encoded",
        "",
        "",
        "def _unpack_payload(name: str, payload: bytes) -> dict[str, object]:",
        "    if len(payload) != MAX_PAYLOAD:",
        "        raise ValueError(f'payload must be {MAX_PAYLOAD} bytes')",
        "    _validate_received_payload(name, payload)",
        "    values: dict[str, object] = {}",
        "    for field_name, offset, kind, size, reserved in _PAYLOAD_FIELDS[name]:",
        "        if reserved:",
        "            continue",
        "        if kind == 'bytes':",
        "            values[field_name] = payload[offset : offset + size]",
        "        else:",
        "            values[field_name] = int.from_bytes(",
        "                payload[offset : offset + size],",
        "                'little',",
        "                signed=kind.startswith('i'),",
        "            )",
        "    return values",
        "",
        "",
        "def _validate_transmit_payload(name: str, payload: bytes) -> None:",
        "    for field_name, (offset, size, mask) in FIELD_ALLOWED_MASKS.get(name, {}).items():",
        "        value = int.from_bytes(payload[offset : offset + size], 'little')",
        "        if value & ~mask:",
        "            raise ValueError(f'{name}.{field_name} has unassigned flags')",
        "",
        "",
        "def _validate_received_payload(name: str, payload: bytes) -> None:",
        "    for field_name, (offset, size, mask) in RECEIVE_REJECT_MASKS.get(name, {}).items():",
        "        value = int.from_bytes(payload[offset : offset + size], 'little')",
        "        if value & mask:",
        "            raise ValueError(f'{name}.{field_name} has rejected flags')",
        "",
        "",
        "def crc16_ccitt_false(data: bytes) -> int:",
        "    remainder = CRC_INIT",
        "    for byte in data:",
        "        remainder ^= byte << 8",
        "        for _ in range(8):",
        "            if remainder & 0x8000:",
        "                remainder = ((remainder << 1) ^ CRC_POLY) & 0xFFFF",
        "            else:",
        "                remainder = (remainder << 1) & 0xFFFF",
        "    return remainder",
        "",
        "",
        "def pack_slot(type_: int, seq: int, payload: bytes) -> bytes:",
        "    if not 0 <= type_ <= 0xFF or not 0 <= seq <= 0xFF:",
        "        raise ValueError('type and sequence must be bytes')",
        "    if type_ not in MESSAGE_NAMES:",
        "        raise ValueError('unknown type')",
        "    expected_length = 0 if type_ == INJ_TYPE_IDLE else MAX_PAYLOAD",
        "    if len(payload) != expected_length:",
        "        raise ValueError(f'payload length must be {expected_length}')",
        "    if expected_length:",
        "        _validate_transmit_payload(MESSAGE_NAMES[type_], payload)",
        "    frame = bytearray(FRAME_SIZE)",
        "    frame[:HEADER_SIZE] = bytes((SOF, type_, seq, len(payload)))",
        "    frame[HEADER_SIZE : HEADER_SIZE + len(payload)] = payload",
        "    frame[-2:] = crc16_ccitt_false(frame[:-2]).to_bytes(2, 'little')",
        "    return bytes(frame)",
        "",
        "",
        "def unpack_slot(slot: bytes) -> Frame:",
        "    if len(slot) != FRAME_SIZE:",
        "        raise FrameError('size')",
        "    if slot[0] != SOF:",
        "        raise FrameError('sof')",
        "    length = slot[3]",
        "    if length > MAX_PAYLOAD:",
        "        raise FrameError('length')",
        "    if int.from_bytes(slot[-2:], 'little') != crc16_ccitt_false(slot[:-2]):",
        "        raise FrameError('crc')",
        "    type_ = slot[1]",
        "    if type_ not in MESSAGE_NAMES:",
        "        raise FrameError('type')",
        "    if length != (0 if type_ == INJ_TYPE_IDLE else MAX_PAYLOAD):",
        "        raise FrameError('length')",
        "    if length:",
        "        try:",
        "            _validate_received_payload(MESSAGE_NAMES[type_], slot[HEADER_SIZE : HEADER_SIZE + length])",
        "        except ValueError as error:",
        "            raise FrameError('flags') from error",
        "    return Frame(type_, slot[2], bytes(slot[HEADER_SIZE : HEADER_SIZE + length]))",
        "",
        "",
        "class SequenceDisposition(Enum):",
        "    NEXT = 'next'",
        "    DUPLICATE = 'duplicate'",
        "    GAP = 'gap'",
        "    STALE = 'stale'",
        "",
        "",
        "def classify_sequence(previous: int, received: int) -> tuple[SequenceDisposition, int]:",
        "    delta = (received - previous) & 0xFF",
        "    if delta == 0:",
        "        return SequenceDisposition.DUPLICATE, 0",
        "    if delta == 1:",
        "        return SequenceDisposition.NEXT, 0",
        "    if delta < 0x80:",
        "        return SequenceDisposition.GAP, delta - 1",
        "    return SequenceDisposition.STALE, 0",
        "",
    ]
    return "\n".join(lines)


def _c_field(field: dict[str, object]) -> str:
    if field["type"] == "bytes":
        return f"    uint8_t {field['name']}[{field['count']}];"
    return f"    {TYPE_INFO[field['type']][1]} {field['name']};"


def _c_bytes(payload: bytes) -> str:
    return ", ".join(f"0x{byte:02X}u" for byte in payload)


def render_c(path: Path) -> str:
    schema = _schema(path)
    frame = schema["frame"]
    crc32 = schema["crc32"]
    limits = schema["limits"]
    counter_positions = schema["counter_positions"]
    enumerations = schema["enumerations"]
    payloads = schema["payloads"]
    message_types = schema["message_types"]
    lines = [
        "/* Generated report-injection wire contract; do not edit by hand. */",
        "#ifndef INJECTION_WIRE_H",
        "#define INJECTION_WIRE_H",
        "",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        f"#define INJ_FRAME_SOF 0x{frame['sof']:02X}u",
        f"#define INJ_FRAME_SIZE {frame['size']}u",
        f"#define INJ_FRAME_HEADER_SIZE {frame['header_size']}u",
        f"#define INJ_FRAME_PAYLOAD_SIZE {frame['payload_size']}u",
        f"#define INJ_CRC16_INIT 0x{frame['crc_init']:04X}u",
        f"#define INJ_CRC16_POLY 0x{frame['crc_poly']:04X}u",
        f"#define INJ_CRC32_INIT 0x{crc32['init']:08X}u",
        f"#define INJ_CRC32_POLY 0x{crc32['poly']:08X}u",
        f"#define INJ_CRC32_XOROUT 0x{crc32['xorout']:08X}u",
        "",
    ]
    for name, value in sorted(limits.items()):
        lines.append(f"#define INJ_MAX_{name.upper()} {value}u")
    lines.append("")
    for name, value in sorted(message_types.items(), key=lambda item: item[1]):
        lines.append(f"#define INJ_TYPE_{name} 0x{value:02X}u")
    for group in sorted(enumerations):
        for name, value in sorted(enumerations[group].items(), key=lambda item: item[1]):
            lines.append(f"#define INJ_{group}_{name} {value}u")
    for page in sorted(counter_positions):
        for name, value in sorted(counter_positions[page].items(), key=lambda item: item[1]):
            lines.append(f"#define INJ_COUNTER_{page}_{name} {value}u")
    lines.append("")
    for payload_name in sorted(payloads):
        for field in payloads[payload_name]:
            if not field.get("reserved"):
                lines.append(
                    f"#define INJ_{payload_name}_{field['name'].upper()}_OFFSET {field['offset']}u"
                )
    lines += [
        "",
        "static inline uint16_t inj_load_u16_le(const uint8_t *data) {",
        "    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);",
        "}",
        "",
        "static inline uint32_t inj_load_u32_le(const uint8_t *data) {",
        "    return (uint32_t)data[0] | ((uint32_t)data[1] << 8) |",
        "           ((uint32_t)data[2] << 16) | ((uint32_t)data[3] << 24);",
        "}",
        "",
        "static inline uint64_t inj_load_u64_le(const uint8_t *data) {",
        "    return (uint64_t)inj_load_u32_le(data) | ((uint64_t)inj_load_u32_le(data + 4) << 32);",
        "}",
        "",
        "static inline void inj_store_u16_le(uint8_t *data, uint16_t value) {",
        "    data[0] = (uint8_t)value;",
        "    data[1] = (uint8_t)(value >> 8);",
        "}",
        "",
        "static inline void inj_store_u32_le(uint8_t *data, uint32_t value) {",
        "    data[0] = (uint8_t)value;",
        "    data[1] = (uint8_t)(value >> 8);",
        "    data[2] = (uint8_t)(value >> 16);",
        "    data[3] = (uint8_t)(value >> 24);",
        "}",
        "",
        "static inline void inj_store_u64_le(uint8_t *data, uint64_t value) {",
        "    inj_store_u32_le(data, (uint32_t)value);",
        "    inj_store_u32_le(data + 4, (uint32_t)(value >> 32));",
        "}",
        "",
    ]
    for payload_name in sorted(payloads):
        c_name = payload_name.lower()
        lines += [f"typedef struct __attribute__((packed)) {{"]
        lines.extend(_c_field(field) for field in payloads[payload_name])
        lines += [
            f"}} inj_{c_name}_payload_t;",
            f"_Static_assert(sizeof(inj_{c_name}_payload_t) == INJ_FRAME_PAYLOAD_SIZE,",
            f"               \"{c_name} payload must fill one slot payload\");",
            "",
        ]
    # The offset macros above are emitted from field['offset']; the structs are
    # emitted from the field list order. Two independent derivations of one
    # layout, so assert they agree. sizeof() alone cannot see this: it is
    # invariant under any same-width permutation of the fields.
    for payload_name in sorted(payloads):
        c_type = f"inj_{payload_name.lower()}_payload_t"
        for field in payloads[payload_name]:
            if field.get("reserved"):
                continue
            macro = f"INJ_{payload_name}_{field['name'].upper()}_OFFSET"
            lines += [
                f"_Static_assert(offsetof({c_type}, {field['name']}) == {macro},",
                f"               \"{c_type}.{field['name']} must sit at its wire offset\");",
            ]
    lines += [
        "",
        "/* Every payload struct is packed and aliases wire bytes directly. */",
        "#if defined(__BYTE_ORDER__) && defined(__ORDER_LITTLE_ENDIAN__)",
        "_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,",
        "               \"packed payload structs assume a little-endian host\");",
        "#endif",
        "",
        "/* Membership in the contract's message-type set. Generated because that",
        " * set is literal JSON data; the per-type payload *length* is not, and is",
        " * hand-written in spi_frame.c. */",
        "static inline int inj_type_is_known(uint8_t type) {",
        "    switch (type) {",
    ]
    for name, _value in sorted(message_types.items(), key=lambda item: item[1]):
        lines.append(f"    case INJ_TYPE_{name}:")
    lines += [
        "        return 1;",
        "    default:",
        "        return 0;",
        "    }",
        "}",
        "",
    ]
    for golden_name, payload_name in (("relative", "RELATIVE"), ("map_entry", "MAP_ENTRY")):
        payload = _golden_payload(schema, payload_name, schema["goldens"][golden_name])
        lines += [
            f"static const uint8_t inj_golden_{golden_name}_payload[INJ_FRAME_PAYLOAD_SIZE] = {{",
            f"    {_c_bytes(payload)},",
            "};",
            "",
        ]
    lines += ["#endif /* INJECTION_WIRE_H */", ""]
    return "\n".join(lines)


def main() -> None:
    PYTHON_PATH.write_text(render_python(SCHEMA_PATH))
    C_HEADER_PATH.write_text(render_c(SCHEMA_PATH))
    subprocess.run(["ruff", "format", str(PYTHON_PATH)], check=True)


if __name__ == "__main__":
    main()
