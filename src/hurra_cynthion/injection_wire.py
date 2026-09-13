"""Generated report-injection wire contract; do not edit by hand."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

SOF = 104
FRAME_SIZE = 32
HEADER_SIZE = 4
MAX_PAYLOAD = 26
CRC_INIT = 0xFFFF
CRC_POLY = 0x1021
CRC32_ALGORITHM = "CRC-32/IEEE"
CRC32_INIT = 0xFFFFFFFF
CRC32_POLY = 0xEDB88320
CRC32_XOROUT = 0xFFFFFFFF

INJ_TYPE_IDLE = 0x00
INJ_TYPE_LINK_STATUS = 0x01
INJ_TYPE_DESCRIPTOR_FRAGMENT = 0x02
INJ_TYPE_REPORT_FRAGMENT = 0x03
INJ_TYPE_MAP_STATUS = 0x04
INJ_TYPE_COMMAND_ACK = 0x05
INJ_TYPE_COUNTERS = 0x06
INJ_TYPE_MAP_BEGIN = 0x81
INJ_TYPE_MAP_ENTRY = 0x82
INJ_TYPE_MAP_COMMIT = 0x83
INJ_TYPE_RELATIVE = 0x84
INJ_TYPE_BUTTON_STATE = 0x85
INJ_TYPE_PHYSICAL_MASK = 0x86
INJ_TYPE_CLEAR = 0x87
INJ_TYPE_TELEMETRY_CONFIG = 0x88

MESSAGE_TYPES = {
    "IDLE": INJ_TYPE_IDLE,
    "LINK_STATUS": INJ_TYPE_LINK_STATUS,
    "DESCRIPTOR_FRAGMENT": INJ_TYPE_DESCRIPTOR_FRAGMENT,
    "REPORT_FRAGMENT": INJ_TYPE_REPORT_FRAGMENT,
    "MAP_STATUS": INJ_TYPE_MAP_STATUS,
    "COMMAND_ACK": INJ_TYPE_COMMAND_ACK,
    "COUNTERS": INJ_TYPE_COUNTERS,
    "MAP_BEGIN": INJ_TYPE_MAP_BEGIN,
    "MAP_ENTRY": INJ_TYPE_MAP_ENTRY,
    "MAP_COMMIT": INJ_TYPE_MAP_COMMIT,
    "RELATIVE": INJ_TYPE_RELATIVE,
    "BUTTON_STATE": INJ_TYPE_BUTTON_STATE,
    "PHYSICAL_MASK": INJ_TYPE_PHYSICAL_MASK,
    "CLEAR": INJ_TYPE_CLEAR,
    "TELEMETRY_CONFIG": INJ_TYPE_TELEMETRY_CONFIG,
}
MESSAGE_NAMES = {value: name for name, value in MESSAGE_TYPES.items()}

LIMITS = {
    "descriptor_bytes_per_interface": 2048,
    "fields": 64,
    "interfaces": 4,
    "layouts": 16,
    "report_bytes": 64,
}
COUNTER_POSITIONS = {
    "COMMAND": {
        "ACCEPTED": 0,
        "REJECTED": 1,
        "LATE": 2,
        "QUEUE_FULL": 3,
        "COMPLETED": 4,
        "OVERFLOW": 5,
    },
    "LINK": {
        "BAD_SOF": 0,
        "BAD_CRC": 1,
        "BAD_LENGTH": 2,
        "BAD_TYPE": 3,
        "SEQUENCE_GAP": 4,
        "DUPLICATE": 5,
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
FIELD_ALLOWED_MASKS = {
    "BUTTON_STATE": {
        "flags": (11, 1, 0),
    },
    "CLEAR": {
        "clear_flags": (8, 2, 15),
    },
    "COMMAND_ACK": {
        "flags": (15, 1, 3),
    },
    "COUNTERS": {
        "flags": (1, 1, 0),
    },
    "LINK_STATUS": {
        "link_flags": (3, 1, 15),
    },
    "MAP_BEGIN": {
        "flags": (6, 2, 0),
    },
    "MAP_COMMIT": {
        "flags": (6, 2, 0),
    },
    "MAP_ENTRY": {
        "flags": (15, 1, 127),
    },
    "MAP_STATUS": {
        "flags": (9, 1, 0),
    },
    "PHYSICAL_MASK": {
        "flags": (11, 1, 0),
    },
    "RELATIVE": {
        "flags": (11, 1, 15),
    },
    "TELEMETRY_CONFIG": {
        "flags": (13, 1, 0),
    },
}

RECEIVE_REJECT_MASKS = {
    "MAP_ENTRY": {
        "flags": (15, 1, 128),
    },
}

ENUMERATIONS = {
    "CLEAR_FLAG": {
        "MOTION": 1,
        "BUTTONS": 2,
        "PHYSICAL_MASKS": 4,
        "QUEUED_TIMED": 8,
        "ALL": 15,
    },
    "COMMAND_ACK_FLAG": {
        "LATE": 1,
        "SYNTHESIZED": 2,
    },
    "COMMAND_ACK_RESULT": {
        "SUCCESS": 0,
        "STALE_LEASE": 1,
        "STALE_MAP": 2,
        "BUSY": 3,
        "OVERFLOW": 4,
        "UNSUPPORTED_TARGET": 5,
        "CLEARED": 6,
    },
    "COMMAND_ACK_STAGE": {
        "ADMITTED": 0,
        "COMPLETED": 1,
        "REJECTED": 2,
    },
    "COUNTERS_PAGE": {
        "LINK": 0,
        "COMMAND": 1,
        "REPORT_MAP": 2,
    },
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
    "RELATIVE_FLAG": {
        "X": 1,
        "Y": 2,
        "WHEEL": 4,
        "PAN": 8,
    },
}

INJ_CLEAR_FLAG_MOTION = 1
INJ_CLEAR_FLAG_BUTTONS = 2
INJ_CLEAR_FLAG_PHYSICAL_MASKS = 4
INJ_CLEAR_FLAG_QUEUED_TIMED = 8
INJ_CLEAR_FLAG_ALL = 15
INJ_COMMAND_ACK_FLAG_LATE = 1
INJ_COMMAND_ACK_FLAG_SYNTHESIZED = 2
INJ_COMMAND_ACK_RESULT_SUCCESS = 0
INJ_COMMAND_ACK_RESULT_STALE_LEASE = 1
INJ_COMMAND_ACK_RESULT_STALE_MAP = 2
INJ_COMMAND_ACK_RESULT_BUSY = 3
INJ_COMMAND_ACK_RESULT_OVERFLOW = 4
INJ_COMMAND_ACK_RESULT_UNSUPPORTED_TARGET = 5
INJ_COMMAND_ACK_RESULT_CLEARED = 6
INJ_COMMAND_ACK_STAGE_ADMITTED = 0
INJ_COMMAND_ACK_STAGE_COMPLETED = 1
INJ_COMMAND_ACK_STAGE_REJECTED = 2
INJ_COUNTERS_PAGE_LINK = 0
INJ_COUNTERS_PAGE_COMMAND = 1
INJ_COUNTERS_PAGE_REPORT_MAP = 2
INJ_LINK_STATUS_FLAG_ENUMERATED = 1
INJ_LINK_STATUS_FLAG_MAP_ACTIVE = 2
INJ_LINK_STATUS_FLAG_INJECTION_ENABLED = 4
INJ_LINK_STATUS_FLAG_RELAY_READY = 8
INJ_MAP_ENTRY_FLAG_SIGNED = 1
INJ_MAP_ENTRY_FLAG_RELATIVE = 2
INJ_MAP_ENTRY_FLAG_BUTTON = 4
INJ_MAP_ENTRY_FLAG_X = 8
INJ_MAP_ENTRY_FLAG_Y = 16
INJ_MAP_ENTRY_FLAG_WHEEL = 32
INJ_MAP_ENTRY_FLAG_PAN = 64
INJ_MAP_STATUS_ERROR_NONE = 0
INJ_MAP_STATUS_ERROR_DESCRIPTOR_GENERATION = 1
INJ_MAP_STATUS_ERROR_MAP_GENERATION = 2
INJ_MAP_STATUS_ERROR_ENTRY_COUNT = 3
INJ_MAP_STATUS_ERROR_LAYOUT_COUNT = 4
INJ_MAP_STATUS_ERROR_CRC = 5
INJ_MAP_STATUS_ERROR_INTERFACE = 6
INJ_MAP_STATUS_ERROR_ENDPOINT = 7
INJ_MAP_STATUS_ERROR_REPORT_LENGTH = 8
INJ_MAP_STATUS_ERROR_FIELD_WIDTH = 9
INJ_MAP_STATUS_ERROR_BIT_OFFSET = 10
INJ_MAP_STATUS_ERROR_REPORT_ID_PREFIX = 11
INJ_MAP_STATUS_ERROR_OVERLAP = 12
INJ_MAP_STATUS_ERROR_UNSUPPORTED_FIELD = 13
INJ_MAP_STATUS_ERROR_DUPLICATE_ENTRY = 14
INJ_MAP_STATUS_ERROR_INTERNAL = 15
INJ_MAP_STATUS_STATUS_CANDIDATE_ACCEPTED = 0
INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED = 1
INJ_MAP_STATUS_STATUS_REJECTED = 2
INJ_MAP_STATUS_STATUS_ABORTED = 3
INJ_RELATIVE_FLAG_X = 1
INJ_RELATIVE_FLAG_Y = 2
INJ_RELATIVE_FLAG_WHEEL = 4
INJ_RELATIVE_FLAG_PAN = 8

INJ_BUTTON_STATE_LEASE_GENERATION_OFFSET = 0
INJ_BUTTON_STATE_MAP_GENERATION_OFFSET = 2
INJ_BUTTON_STATE_COMMAND_SEQUENCE_OFFSET = 4
INJ_BUTTON_STATE_TARGET_FRAME_OFFSET = 6
INJ_BUTTON_STATE_INTERFACE_NUMBER_OFFSET = 8
INJ_BUTTON_STATE_ENDPOINT_NUMBER_OFFSET = 9
INJ_BUTTON_STATE_REPORT_ID_OFFSET = 10
INJ_BUTTON_STATE_FLAGS_OFFSET = 11
INJ_BUTTON_STATE_BUTTONS_OFFSET = 12
INJ_BUTTON_STATE_HOLD_REPORTS_OFFSET = 20
INJ_CLEAR_LEASE_GENERATION_OFFSET = 0
INJ_CLEAR_MAP_GENERATION_OFFSET = 2
INJ_CLEAR_COMMAND_SEQUENCE_OFFSET = 4
INJ_CLEAR_TARGET_FRAME_OFFSET = 6
INJ_CLEAR_CLEAR_FLAGS_OFFSET = 8
INJ_CLEAR_REASON_OFFSET = 10
INJ_COMMAND_ACK_LEASE_GENERATION_OFFSET = 0
INJ_COMMAND_ACK_MAP_GENERATION_OFFSET = 2
INJ_COMMAND_ACK_COMMAND_SEQUENCE_OFFSET = 4
INJ_COMMAND_ACK_TARGET_FRAME_OFFSET = 6
INJ_COMMAND_ACK_ACCEPTED_FRAME_OFFSET = 8
INJ_COMMAND_ACK_COMPLETED_FRAME_OFFSET = 10
INJ_COMMAND_ACK_ACK_STAGE_OFFSET = 12
INJ_COMMAND_ACK_RESULT_OFFSET = 13
INJ_COMMAND_ACK_COMMAND_TYPE_OFFSET = 14
INJ_COMMAND_ACK_FLAGS_OFFSET = 15
INJ_COMMAND_ACK_INTERFACE_NUMBER_OFFSET = 16
INJ_COMMAND_ACK_ENDPOINT_NUMBER_OFFSET = 17
INJ_COMMAND_ACK_REPORT_ID_OFFSET = 18
INJ_COUNTERS_PAGE_OFFSET = 0
INJ_COUNTERS_FLAGS_OFFSET = 1
INJ_COUNTERS_COUNTER0_OFFSET = 2
INJ_COUNTERS_COUNTER1_OFFSET = 6
INJ_COUNTERS_COUNTER2_OFFSET = 10
INJ_COUNTERS_COUNTER3_OFFSET = 14
INJ_COUNTERS_COUNTER4_OFFSET = 18
INJ_COUNTERS_COUNTER5_OFFSET = 22
INJ_DESCRIPTOR_FRAGMENT_DESCRIPTOR_GENERATION_OFFSET = 0
INJ_DESCRIPTOR_FRAGMENT_INTERFACE_NUMBER_OFFSET = 2
INJ_DESCRIPTOR_FRAGMENT_OFFSET_OFFSET = 4
INJ_DESCRIPTOR_FRAGMENT_TOTAL_OFFSET = 6
INJ_DESCRIPTOR_FRAGMENT_DATA_OFFSET = 8
INJ_LINK_STATUS_USB_FRAME_OFFSET = 0
INJ_LINK_STATUS_USB_SUBFRAME_OFFSET = 2
INJ_LINK_STATUS_LINK_FLAGS_OFFSET = 3
INJ_LINK_STATUS_DESCRIPTOR_GENERATION_OFFSET = 4
INJ_LINK_STATUS_ACTIVE_MAP_GENERATION_OFFSET = 6
INJ_LINK_STATUS_SLOT_COUNTER_OFFSET = 8
INJ_LINK_STATUS_NATIVE_REPORT_COUNT_OFFSET = 12
INJ_LINK_STATUS_FAULT_FLAGS_OFFSET = 16
INJ_LINK_STATUS_LAST_RX_SEQUENCE_OFFSET = 20
INJ_MAP_BEGIN_DESCRIPTOR_GENERATION_OFFSET = 0
INJ_MAP_BEGIN_MAP_GENERATION_OFFSET = 2
INJ_MAP_BEGIN_ENTRY_COUNT_OFFSET = 4
INJ_MAP_BEGIN_LAYOUT_COUNT_OFFSET = 5
INJ_MAP_BEGIN_FLAGS_OFFSET = 6
INJ_MAP_BEGIN_ENTRIES_CRC32_OFFSET = 8
INJ_MAP_COMMIT_DESCRIPTOR_GENERATION_OFFSET = 0
INJ_MAP_COMMIT_MAP_GENERATION_OFFSET = 2
INJ_MAP_COMMIT_ENTRY_COUNT_OFFSET = 4
INJ_MAP_COMMIT_LAYOUT_COUNT_OFFSET = 5
INJ_MAP_COMMIT_FLAGS_OFFSET = 6
INJ_MAP_COMMIT_ENTRIES_CRC32_OFFSET = 8
INJ_MAP_ENTRY_DESCRIPTOR_GENERATION_OFFSET = 0
INJ_MAP_ENTRY_MAP_GENERATION_OFFSET = 2
INJ_MAP_ENTRY_ENTRY_INDEX_OFFSET = 4
INJ_MAP_ENTRY_INTERFACE_NUMBER_OFFSET = 5
INJ_MAP_ENTRY_ENDPOINT_NUMBER_OFFSET = 6
INJ_MAP_ENTRY_REPORT_ID_OFFSET = 7
INJ_MAP_ENTRY_USAGE_PAGE_OFFSET = 8
INJ_MAP_ENTRY_USAGE_OFFSET = 10
INJ_MAP_ENTRY_BIT_OFFSET_OFFSET = 12
INJ_MAP_ENTRY_BIT_WIDTH_OFFSET = 14
INJ_MAP_ENTRY_FLAGS_OFFSET = 15
INJ_MAP_ENTRY_LOGICAL_MINIMUM_OFFSET = 16
INJ_MAP_ENTRY_LOGICAL_MAXIMUM_OFFSET = 20
INJ_MAP_ENTRY_REPORT_LENGTH_OFFSET = 24
INJ_MAP_STATUS_DESCRIPTOR_GENERATION_OFFSET = 0
INJ_MAP_STATUS_MAP_GENERATION_OFFSET = 2
INJ_MAP_STATUS_ACTIVE_MAP_GENERATION_OFFSET = 4
INJ_MAP_STATUS_ENTRY_INDEX_OFFSET = 6
INJ_MAP_STATUS_STATUS_OFFSET = 7
INJ_MAP_STATUS_ERROR_OFFSET = 8
INJ_MAP_STATUS_FLAGS_OFFSET = 9
INJ_MAP_STATUS_ENTRIES_CRC32_OFFSET = 10
INJ_PHYSICAL_MASK_LEASE_GENERATION_OFFSET = 0
INJ_PHYSICAL_MASK_MAP_GENERATION_OFFSET = 2
INJ_PHYSICAL_MASK_COMMAND_SEQUENCE_OFFSET = 4
INJ_PHYSICAL_MASK_TARGET_FRAME_OFFSET = 6
INJ_PHYSICAL_MASK_INTERFACE_NUMBER_OFFSET = 8
INJ_PHYSICAL_MASK_ENDPOINT_NUMBER_OFFSET = 9
INJ_PHYSICAL_MASK_REPORT_ID_OFFSET = 10
INJ_PHYSICAL_MASK_FLAGS_OFFSET = 11
INJ_PHYSICAL_MASK_BUTTON_MASK_OFFSET = 12
INJ_RELATIVE_LEASE_GENERATION_OFFSET = 0
INJ_RELATIVE_MAP_GENERATION_OFFSET = 2
INJ_RELATIVE_COMMAND_SEQUENCE_OFFSET = 4
INJ_RELATIVE_TARGET_FRAME_OFFSET = 6
INJ_RELATIVE_INTERFACE_NUMBER_OFFSET = 8
INJ_RELATIVE_ENDPOINT_NUMBER_OFFSET = 9
INJ_RELATIVE_REPORT_ID_OFFSET = 10
INJ_RELATIVE_FLAGS_OFFSET = 11
INJ_RELATIVE_X_OFFSET = 12
INJ_RELATIVE_Y_OFFSET = 14
INJ_RELATIVE_WHEEL_OFFSET = 16
INJ_RELATIVE_PAN_OFFSET = 18
INJ_RELATIVE_HOLD_REPORTS_OFFSET = 20
INJ_REPORT_FRAGMENT_DESCRIPTOR_GENERATION_OFFSET = 0
INJ_REPORT_FRAGMENT_INTERFACE_NUMBER_OFFSET = 2
INJ_REPORT_FRAGMENT_ENDPOINT_NUMBER_OFFSET = 3
INJ_REPORT_FRAGMENT_REPORT_ID_OFFSET = 4
INJ_REPORT_FRAGMENT_OFFSET_OFFSET = 5
INJ_REPORT_FRAGMENT_TOTAL_OFFSET = 7
INJ_REPORT_FRAGMENT_DATA_OFFSET = 9
INJ_TELEMETRY_CONFIG_LEASE_GENERATION_OFFSET = 0
INJ_TELEMETRY_CONFIG_COMMAND_SEQUENCE_OFFSET = 2
INJ_TELEMETRY_CONFIG_TELEMETRY_MASK_OFFSET = 4
INJ_TELEMETRY_CONFIG_INTERVAL_SLOTS_OFFSET = 8
INJ_TELEMETRY_CONFIG_REPORT_SAMPLE_DIVISOR_OFFSET = 10
INJ_TELEMETRY_CONFIG_COUNTER_PAGE_OFFSET = 12
INJ_TELEMETRY_CONFIG_FLAGS_OFFSET = 13

PAYLOAD_LAYOUTS = {
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
    "CLEAR": (
        ("lease_generation", 0, 2),
        ("map_generation", 2, 2),
        ("command_sequence", 4, 2),
        ("target_frame", 6, 2),
        ("clear_flags", 8, 2),
        ("reason", 10, 1),
        ("reserved", 11, 15),
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
    "DESCRIPTOR_FRAGMENT": (
        ("descriptor_generation", 0, 2),
        ("interface_number", 2, 1),
        ("reserved", 3, 1),
        ("offset", 4, 2),
        ("total", 6, 2),
        ("data", 8, 18),
    ),
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
    "MAP_BEGIN": (
        ("descriptor_generation", 0, 2),
        ("map_generation", 2, 2),
        ("entry_count", 4, 1),
        ("layout_count", 5, 1),
        ("flags", 6, 2),
        ("entries_crc32", 8, 4),
        ("reserved", 12, 14),
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
    "REPORT_FRAGMENT": (
        ("descriptor_generation", 0, 2),
        ("interface_number", 2, 1),
        ("endpoint_number", 3, 1),
        ("report_id", 4, 1),
        ("offset", 5, 2),
        ("total", 7, 2),
        ("data", 9, 17),
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
_PAYLOAD_FIELDS = {
    "BUTTON_STATE": (
        ("lease_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("command_sequence", 4, "u16", 2, False),
        ("target_frame", 6, "u16", 2, False),
        ("interface_number", 8, "u8", 1, False),
        ("endpoint_number", 9, "u8", 1, False),
        ("report_id", 10, "u8", 1, False),
        ("flags", 11, "u8", 1, False),
        ("buttons", 12, "u64", 8, False),
        ("hold_reports", 20, "u16", 2, False),
        ("reserved", 22, "u32", 4, True),
    ),
    "CLEAR": (
        ("lease_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("command_sequence", 4, "u16", 2, False),
        ("target_frame", 6, "u16", 2, False),
        ("clear_flags", 8, "u16", 2, False),
        ("reason", 10, "u8", 1, False),
        ("reserved", 11, "bytes", 15, True),
    ),
    "COMMAND_ACK": (
        ("lease_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("command_sequence", 4, "u16", 2, False),
        ("target_frame", 6, "u16", 2, False),
        ("accepted_frame", 8, "u16", 2, False),
        ("completed_frame", 10, "u16", 2, False),
        ("ack_stage", 12, "u8", 1, False),
        ("result", 13, "u8", 1, False),
        ("command_type", 14, "u8", 1, False),
        ("flags", 15, "u8", 1, False),
        ("interface_number", 16, "u8", 1, False),
        ("endpoint_number", 17, "u8", 1, False),
        ("report_id", 18, "u8", 1, False),
        ("reserved", 19, "bytes", 7, True),
    ),
    "COUNTERS": (
        ("page", 0, "u8", 1, False),
        ("flags", 1, "u8", 1, False),
        ("counter0", 2, "u32", 4, False),
        ("counter1", 6, "u32", 4, False),
        ("counter2", 10, "u32", 4, False),
        ("counter3", 14, "u32", 4, False),
        ("counter4", 18, "u32", 4, False),
        ("counter5", 22, "u32", 4, False),
    ),
    "DESCRIPTOR_FRAGMENT": (
        ("descriptor_generation", 0, "u16", 2, False),
        ("interface_number", 2, "u8", 1, False),
        ("reserved", 3, "u8", 1, True),
        ("offset", 4, "u16", 2, False),
        ("total", 6, "u16", 2, False),
        ("data", 8, "bytes", 18, False),
    ),
    "IDLE": (("reserved", 0, "bytes", 26, True),),
    "LINK_STATUS": (
        ("usb_frame", 0, "u16", 2, False),
        ("usb_subframe", 2, "u8", 1, False),
        ("link_flags", 3, "u8", 1, False),
        ("descriptor_generation", 4, "u16", 2, False),
        ("active_map_generation", 6, "u16", 2, False),
        ("slot_counter", 8, "u32", 4, False),
        ("native_report_count", 12, "u32", 4, False),
        ("fault_flags", 16, "u32", 4, False),
        ("last_rx_sequence", 20, "u8", 1, False),
        ("reserved", 21, "bytes", 5, True),
    ),
    "MAP_BEGIN": (
        ("descriptor_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("entry_count", 4, "u8", 1, False),
        ("layout_count", 5, "u8", 1, False),
        ("flags", 6, "u16", 2, False),
        ("entries_crc32", 8, "u32", 4, False),
        ("reserved", 12, "bytes", 14, True),
    ),
    "MAP_COMMIT": (
        ("descriptor_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("entry_count", 4, "u8", 1, False),
        ("layout_count", 5, "u8", 1, False),
        ("flags", 6, "u16", 2, False),
        ("entries_crc32", 8, "u32", 4, False),
        ("reserved", 12, "bytes", 14, True),
    ),
    "MAP_ENTRY": (
        ("descriptor_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("entry_index", 4, "u8", 1, False),
        ("interface_number", 5, "u8", 1, False),
        ("endpoint_number", 6, "u8", 1, False),
        ("report_id", 7, "u8", 1, False),
        ("usage_page", 8, "u16", 2, False),
        ("usage", 10, "u16", 2, False),
        ("bit_offset", 12, "u16", 2, False),
        ("bit_width", 14, "u8", 1, False),
        ("flags", 15, "u8", 1, False),
        ("logical_minimum", 16, "i32", 4, False),
        ("logical_maximum", 20, "i32", 4, False),
        ("report_length", 24, "u8", 1, False),
        ("reserved", 25, "u8", 1, True),
    ),
    "MAP_STATUS": (
        ("descriptor_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("active_map_generation", 4, "u16", 2, False),
        ("entry_index", 6, "u8", 1, False),
        ("status", 7, "u8", 1, False),
        ("error", 8, "u8", 1, False),
        ("flags", 9, "u8", 1, False),
        ("entries_crc32", 10, "u32", 4, False),
        ("reserved", 14, "bytes", 12, True),
    ),
    "PHYSICAL_MASK": (
        ("lease_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("command_sequence", 4, "u16", 2, False),
        ("target_frame", 6, "u16", 2, False),
        ("interface_number", 8, "u8", 1, False),
        ("endpoint_number", 9, "u8", 1, False),
        ("report_id", 10, "u8", 1, False),
        ("flags", 11, "u8", 1, False),
        ("button_mask", 12, "u64", 8, False),
        ("reserved", 20, "bytes", 6, True),
    ),
    "RELATIVE": (
        ("lease_generation", 0, "u16", 2, False),
        ("map_generation", 2, "u16", 2, False),
        ("command_sequence", 4, "u16", 2, False),
        ("target_frame", 6, "u16", 2, False),
        ("interface_number", 8, "u8", 1, False),
        ("endpoint_number", 9, "u8", 1, False),
        ("report_id", 10, "u8", 1, False),
        ("flags", 11, "u8", 1, False),
        ("x", 12, "i16", 2, False),
        ("y", 14, "i16", 2, False),
        ("wheel", 16, "i16", 2, False),
        ("pan", 18, "i16", 2, False),
        ("hold_reports", 20, "u16", 2, False),
        ("reserved", 22, "u32", 4, True),
    ),
    "REPORT_FRAGMENT": (
        ("descriptor_generation", 0, "u16", 2, False),
        ("interface_number", 2, "u8", 1, False),
        ("endpoint_number", 3, "u8", 1, False),
        ("report_id", 4, "u8", 1, False),
        ("offset", 5, "u16", 2, False),
        ("total", 7, "u16", 2, False),
        ("data", 9, "bytes", 17, False),
    ),
    "TELEMETRY_CONFIG": (
        ("lease_generation", 0, "u16", 2, False),
        ("command_sequence", 2, "u16", 2, False),
        ("telemetry_mask", 4, "u32", 4, False),
        ("interval_slots", 8, "u16", 2, False),
        ("report_sample_divisor", 10, "u16", 2, False),
        ("counter_page", 12, "u8", 1, False),
        ("flags", 13, "u8", 1, False),
        ("reserved", 14, "bytes", 12, True),
    ),
}


@dataclass(frozen=True)
class ButtonStatePayload:
    lease_generation: int
    map_generation: int
    command_sequence: int
    target_frame: int
    interface_number: int
    endpoint_number: int
    report_id: int
    flags: int
    buttons: int
    hold_reports: int

    def to_bytes(self) -> bytes:
        return _pack_payload("BUTTON_STATE", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> ButtonStatePayload:
        return cls(**_unpack_payload("BUTTON_STATE", payload))


@dataclass(frozen=True)
class ClearPayload:
    lease_generation: int
    map_generation: int
    command_sequence: int
    target_frame: int
    clear_flags: int
    reason: int

    def to_bytes(self) -> bytes:
        return _pack_payload("CLEAR", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> ClearPayload:
        return cls(**_unpack_payload("CLEAR", payload))


@dataclass(frozen=True)
class CommandAckPayload:
    lease_generation: int
    map_generation: int
    command_sequence: int
    target_frame: int
    accepted_frame: int
    completed_frame: int
    ack_stage: int
    result: int
    command_type: int
    flags: int
    interface_number: int
    endpoint_number: int
    report_id: int

    def to_bytes(self) -> bytes:
        return _pack_payload("COMMAND_ACK", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> CommandAckPayload:
        return cls(**_unpack_payload("COMMAND_ACK", payload))


@dataclass(frozen=True)
class CountersPayload:
    page: int
    flags: int
    counter0: int
    counter1: int
    counter2: int
    counter3: int
    counter4: int
    counter5: int

    def to_bytes(self) -> bytes:
        return _pack_payload("COUNTERS", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> CountersPayload:
        return cls(**_unpack_payload("COUNTERS", payload))


@dataclass(frozen=True)
class DescriptorFragmentPayload:
    descriptor_generation: int
    interface_number: int
    offset: int
    total: int
    data: bytes

    def to_bytes(self) -> bytes:
        return _pack_payload("DESCRIPTOR_FRAGMENT", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> DescriptorFragmentPayload:
        return cls(**_unpack_payload("DESCRIPTOR_FRAGMENT", payload))


@dataclass(frozen=True)
class IdlePayload:
    pass

    def to_bytes(self) -> bytes:
        return _pack_payload("IDLE", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> IdlePayload:
        return cls(**_unpack_payload("IDLE", payload))


@dataclass(frozen=True)
class LinkStatusPayload:
    usb_frame: int
    usb_subframe: int
    link_flags: int
    descriptor_generation: int
    active_map_generation: int
    slot_counter: int
    native_report_count: int
    fault_flags: int
    last_rx_sequence: int

    def to_bytes(self) -> bytes:
        return _pack_payload("LINK_STATUS", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> LinkStatusPayload:
        return cls(**_unpack_payload("LINK_STATUS", payload))


@dataclass(frozen=True)
class MapBeginPayload:
    descriptor_generation: int
    map_generation: int
    entry_count: int
    layout_count: int
    flags: int
    entries_crc32: int

    def to_bytes(self) -> bytes:
        return _pack_payload("MAP_BEGIN", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> MapBeginPayload:
        return cls(**_unpack_payload("MAP_BEGIN", payload))


@dataclass(frozen=True)
class MapCommitPayload:
    descriptor_generation: int
    map_generation: int
    entry_count: int
    layout_count: int
    flags: int
    entries_crc32: int

    def to_bytes(self) -> bytes:
        return _pack_payload("MAP_COMMIT", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> MapCommitPayload:
        return cls(**_unpack_payload("MAP_COMMIT", payload))


@dataclass(frozen=True)
class MapEntryPayload:
    descriptor_generation: int
    map_generation: int
    entry_index: int
    interface_number: int
    endpoint_number: int
    report_id: int
    usage_page: int
    usage: int
    bit_offset: int
    bit_width: int
    flags: int
    logical_minimum: int
    logical_maximum: int
    report_length: int

    def to_bytes(self) -> bytes:
        return _pack_payload("MAP_ENTRY", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> MapEntryPayload:
        return cls(**_unpack_payload("MAP_ENTRY", payload))


@dataclass(frozen=True)
class MapStatusPayload:
    descriptor_generation: int
    map_generation: int
    active_map_generation: int
    entry_index: int
    status: int
    error: int
    flags: int
    entries_crc32: int

    def to_bytes(self) -> bytes:
        return _pack_payload("MAP_STATUS", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> MapStatusPayload:
        return cls(**_unpack_payload("MAP_STATUS", payload))


@dataclass(frozen=True)
class PhysicalMaskPayload:
    lease_generation: int
    map_generation: int
    command_sequence: int
    target_frame: int
    interface_number: int
    endpoint_number: int
    report_id: int
    flags: int
    button_mask: int

    def to_bytes(self) -> bytes:
        return _pack_payload("PHYSICAL_MASK", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> PhysicalMaskPayload:
        return cls(**_unpack_payload("PHYSICAL_MASK", payload))


@dataclass(frozen=True)
class RelativePayload:
    lease_generation: int
    map_generation: int
    command_sequence: int
    target_frame: int
    interface_number: int
    endpoint_number: int
    report_id: int
    flags: int
    x: int
    y: int
    wheel: int
    pan: int
    hold_reports: int

    def to_bytes(self) -> bytes:
        return _pack_payload("RELATIVE", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> RelativePayload:
        return cls(**_unpack_payload("RELATIVE", payload))


@dataclass(frozen=True)
class ReportFragmentPayload:
    descriptor_generation: int
    interface_number: int
    endpoint_number: int
    report_id: int
    offset: int
    total: int
    data: bytes

    def to_bytes(self) -> bytes:
        return _pack_payload("REPORT_FRAGMENT", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> ReportFragmentPayload:
        return cls(**_unpack_payload("REPORT_FRAGMENT", payload))


@dataclass(frozen=True)
class TelemetryConfigPayload:
    lease_generation: int
    command_sequence: int
    telemetry_mask: int
    interval_slots: int
    report_sample_divisor: int
    counter_page: int
    flags: int

    def to_bytes(self) -> bytes:
        return _pack_payload("TELEMETRY_CONFIG", self.__dict__)

    @classmethod
    def from_bytes(cls, payload: bytes) -> TelemetryConfigPayload:
        return cls(**_unpack_payload("TELEMETRY_CONFIG", payload))


RELATIVE_GOLDEN_PAYLOAD = bytes.fromhex("2211443366558877090a0b0ffeff3412ccedff7fbc9a00000000")
MAP_ENTRY_GOLDEN_PAYLOAD = bytes.fromhex("341278569a0203010100300008000c1b00f8ffffff0700000400")


class FrameError(ValueError):
    """A malformed or unsupported slot."""


@dataclass(frozen=True)
class Frame:
    type_: int
    seq: int
    payload: bytes


def _store_u8(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset] = value


def _store_u16(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 2] = value.to_bytes(2, "little")


def _store_u32(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 4] = value.to_bytes(4, "little")


def _store_u64(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 8] = value.to_bytes(8, "little")


def _load_u8(payload: bytes, offset: int) -> int:
    return payload[offset]


def _load_u16(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 2], "little")


def _load_u32(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 4], "little")


def _load_u64(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 8], "little")


def _store_int(buffer: bytearray, offset: int, size: int, value: int, signed: bool) -> None:
    minimum = -(1 << (size * 8 - 1)) if signed else 0
    maximum = (1 << (size * 8 - (1 if signed else 0))) - 1
    if not minimum <= value <= maximum:
        raise ValueError(f"integer {value} does not fit in {size * 8} bits")
    buffer[offset : offset + size] = value.to_bytes(size, "little", signed=signed)


def _pack_payload(name: str, values: dict[str, object]) -> bytes:
    try:
        fields = _PAYLOAD_FIELDS[name]
    except KeyError as error:
        raise ValueError(f"unknown payload {name}") from error
    payload = bytearray(MAX_PAYLOAD)
    for field_name, offset, kind, size, reserved in fields:
        if reserved:
            continue
        value = values[field_name]
        if kind == "bytes":
            if not isinstance(value, bytes) or len(value) != size:
                raise ValueError(f"{name}.{field_name} must be {size} bytes")
            payload[offset : offset + size] = value
        else:
            _store_int(payload, offset, size, int(value), kind.startswith("i"))
    encoded = bytes(payload)
    _validate_transmit_payload(name, encoded)
    return encoded


def _unpack_payload(name: str, payload: bytes) -> dict[str, object]:
    if len(payload) != MAX_PAYLOAD:
        raise ValueError(f"payload must be {MAX_PAYLOAD} bytes")
    _validate_received_payload(name, payload)
    values: dict[str, object] = {}
    for field_name, offset, kind, size, reserved in _PAYLOAD_FIELDS[name]:
        if reserved:
            continue
        if kind == "bytes":
            values[field_name] = payload[offset : offset + size]
        else:
            values[field_name] = int.from_bytes(
                payload[offset : offset + size],
                "little",
                signed=kind.startswith("i"),
            )
    return values


def _validate_transmit_payload(name: str, payload: bytes) -> None:
    for field_name, (offset, size, mask) in FIELD_ALLOWED_MASKS.get(name, {}).items():
        value = int.from_bytes(payload[offset : offset + size], "little")
        if value & ~mask:
            raise ValueError(f"{name}.{field_name} has unassigned flags")


def _validate_received_payload(name: str, payload: bytes) -> None:
    for field_name, (offset, size, mask) in RECEIVE_REJECT_MASKS.get(name, {}).items():
        value = int.from_bytes(payload[offset : offset + size], "little")
        if value & mask:
            raise ValueError(f"{name}.{field_name} has rejected flags")


def crc16_ccitt_false(data: bytes) -> int:
    remainder = CRC_INIT
    for byte in data:
        remainder ^= byte << 8
        for _ in range(8):
            if remainder & 0x8000:
                remainder = ((remainder << 1) ^ CRC_POLY) & 0xFFFF
            else:
                remainder = (remainder << 1) & 0xFFFF
    return remainder


def pack_slot(type_: int, seq: int, payload: bytes) -> bytes:
    if not 0 <= type_ <= 0xFF or not 0 <= seq <= 0xFF:
        raise ValueError("type and sequence must be bytes")
    if type_ not in MESSAGE_NAMES:
        raise ValueError("unknown type")
    expected_length = 0 if type_ == INJ_TYPE_IDLE else MAX_PAYLOAD
    if len(payload) != expected_length:
        raise ValueError(f"payload length must be {expected_length}")
    if expected_length:
        _validate_transmit_payload(MESSAGE_NAMES[type_], payload)
    frame = bytearray(FRAME_SIZE)
    frame[:HEADER_SIZE] = bytes((SOF, type_, seq, len(payload)))
    frame[HEADER_SIZE : HEADER_SIZE + len(payload)] = payload
    frame[-2:] = crc16_ccitt_false(frame[:-2]).to_bytes(2, "little")
    return bytes(frame)


def unpack_slot(slot: bytes) -> Frame:
    if len(slot) != FRAME_SIZE:
        raise FrameError("size")
    if slot[0] != SOF:
        raise FrameError("sof")
    length = slot[3]
    if length > MAX_PAYLOAD:
        raise FrameError("length")
    if int.from_bytes(slot[-2:], "little") != crc16_ccitt_false(slot[:-2]):
        raise FrameError("crc")
    type_ = slot[1]
    if type_ not in MESSAGE_NAMES:
        raise FrameError("type")
    if length != (0 if type_ == INJ_TYPE_IDLE else MAX_PAYLOAD):
        raise FrameError("length")
    if length:
        try:
            _validate_received_payload(
                MESSAGE_NAMES[type_], slot[HEADER_SIZE : HEADER_SIZE + length]
            )
        except ValueError as error:
            raise FrameError("flags") from error
    return Frame(type_, slot[2], bytes(slot[HEADER_SIZE : HEADER_SIZE + length]))


class SequenceDisposition(Enum):
    NEXT = "next"
    DUPLICATE = "duplicate"
    GAP = "gap"
    STALE = "stale"


def classify_sequence(previous: int, received: int) -> tuple[SequenceDisposition, int]:
    delta = (received - previous) & 0xFF
    if delta == 0:
        return SequenceDisposition.DUPLICATE, 0
    if delta == 1:
        return SequenceDisposition.NEXT, 0
    if delta < 0x80:
        return SequenceDisposition.GAP, delta - 1
    return SequenceDisposition.STALE, 0
