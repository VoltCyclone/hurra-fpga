"""Atomic, bounded storage for descriptor-compiled injection field maps."""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from enum import IntEnum

from amaranth import Array, Cat, Const, Elaboratable, Module, Mux, Signal
from amaranth.lib.data import StructLayout, signed, unsigned
from amaranth.lib.memory import Memory

from .injection_wire import (
    CRC32_INIT,
    CRC32_POLY,
    CRC32_XOROUT,
    INJ_MAP_ENTRY_FLAG_BUTTON,
    INJ_MAP_ENTRY_FLAG_PAN,
    INJ_MAP_ENTRY_FLAG_RELATIVE,
    INJ_MAP_ENTRY_FLAG_SIGNED,
    INJ_MAP_ENTRY_FLAG_WHEEL,
    INJ_MAP_ENTRY_FLAG_X,
    INJ_MAP_ENTRY_FLAG_Y,
    INJ_MAP_STATUS_ERROR_BIT_OFFSET,
    INJ_MAP_STATUS_ERROR_CRC,
    INJ_MAP_STATUS_ERROR_DESCRIPTOR_GENERATION,
    INJ_MAP_STATUS_ERROR_DUPLICATE_ENTRY,
    INJ_MAP_STATUS_ERROR_ENDPOINT,
    INJ_MAP_STATUS_ERROR_ENTRY_COUNT,
    INJ_MAP_STATUS_ERROR_FIELD_WIDTH,
    INJ_MAP_STATUS_ERROR_INTERFACE,
    INJ_MAP_STATUS_ERROR_LAYOUT_COUNT,
    INJ_MAP_STATUS_ERROR_MAP_GENERATION,
    INJ_MAP_STATUS_ERROR_NONE,
    INJ_MAP_STATUS_ERROR_OVERLAP,
    INJ_MAP_STATUS_ERROR_REPORT_ID_PREFIX,
    INJ_MAP_STATUS_ERROR_REPORT_LENGTH,
    INJ_MAP_STATUS_ERROR_UNSUPPORTED_FIELD,
    LIMITS,
    MAX_PAYLOAD,
    PAYLOAD_LAYOUTS,
)

__all__ = [
    "MAP_ENTRY_LAYOUT",
    "InjectionMapStore",
    "MapError",
]


class MapError(IntEnum):
    """Map validation errors, using the canonical MAP_STATUS wire values."""

    NONE = INJ_MAP_STATUS_ERROR_NONE
    GENERATION = INJ_MAP_STATUS_ERROR_DESCRIPTOR_GENERATION
    DESCRIPTOR_GENERATION = INJ_MAP_STATUS_ERROR_DESCRIPTOR_GENERATION
    MAP_GENERATION = INJ_MAP_STATUS_ERROR_MAP_GENERATION
    FIELD_COUNT = INJ_MAP_STATUS_ERROR_ENTRY_COUNT
    ENTRY_COUNT = INJ_MAP_STATUS_ERROR_ENTRY_COUNT
    LAYOUT_COUNT = INJ_MAP_STATUS_ERROR_LAYOUT_COUNT
    CRC = INJ_MAP_STATUS_ERROR_CRC
    INTERFACE = INJ_MAP_STATUS_ERROR_INTERFACE
    ENDPOINT = INJ_MAP_STATUS_ERROR_ENDPOINT
    REPORT_LENGTH = INJ_MAP_STATUS_ERROR_REPORT_LENGTH
    FIELD_WIDTH = INJ_MAP_STATUS_ERROR_FIELD_WIDTH
    BIT_OFFSET = INJ_MAP_STATUS_ERROR_BIT_OFFSET
    REPORT_ID_PREFIX = INJ_MAP_STATUS_ERROR_REPORT_ID_PREFIX
    OVERLAP = INJ_MAP_STATUS_ERROR_OVERLAP
    UNSUPPORTED_FIELD = INJ_MAP_STATUS_ERROR_UNSUPPORTED_FIELD
    DUPLICATE_ENTRY = INJ_MAP_STATUS_ERROR_DUPLICATE_ENTRY


def _map_entry_layout() -> StructLayout:
    members = {}
    expected_offset = 0
    for name, offset, size in PAYLOAD_LAYOUTS["MAP_ENTRY"]:
        if offset != expected_offset:
            raise ValueError("MAP_ENTRY wire layout must be contiguous")
        width = size * 8
        members[name] = (
            signed(width) if name in {"logical_minimum", "logical_maximum"} else unsigned(width)
        )
        expected_offset += size
    if expected_offset != MAX_PAYLOAD:
        raise ValueError("MAP_ENTRY wire layout must fill one canonical payload")
    return StructLayout(members)


MAP_ENTRY_LAYOUT = _map_entry_layout()

LAYOUT_DIRECTORY_LAYOUT = StructLayout(
    {
        "interface_number": unsigned(8),
        "endpoint_number": unsigned(8),
        "report_id": unsigned(8),
        "report_length": unsigned(7),
    }
)

_AXIS_FLAGS = (
    INJ_MAP_ENTRY_FLAG_X | INJ_MAP_ENTRY_FLAG_Y | INJ_MAP_ENTRY_FLAG_WHEEL | INJ_MAP_ENTRY_FLAG_PAN
)
_ALLOWED_FLAGS = (
    INJ_MAP_ENTRY_FLAG_SIGNED
    | INJ_MAP_ENTRY_FLAG_RELATIVE
    | INJ_MAP_ENTRY_FLAG_BUTTON
    | _AXIS_FLAGS
)


def _crc32_byte(crc, byte):
    """Advance reflected IEEE CRC-32 by one byte."""

    value = crc ^ byte
    for _ in range(8):
        value = Mux(value[0], (value >> 1) ^ Const(CRC32_POLY, 32), value >> 1)
    return value


class InjectionMapStore(Elaboratable):
    """Validate an inactive field-map bank and publish it with one atomic flip.

    Map entries use the exact packed 26-byte ``MAP_ENTRY`` payload layout.
    Candidate metadata is presented with ``begin`` and presented again with
    ``commit``; the latter must match the values latched at ``begin``. Validation
    is deliberately sequential and bounded in the ``usb`` clock domain.
    """

    def __init__(
        self,
        *,
        max_layouts: int = LIMITS["layouts"],
        max_fields: int = LIMITS["fields"],
    ) -> None:
        if not 1 <= max_layouts <= LIMITS["layouts"]:
            raise ValueError(f"max_layouts must be between 1 and {LIMITS['layouts']}")
        if not 1 <= max_fields <= LIMITS["fields"]:
            raise ValueError(f"max_fields must be between 1 and {LIMITS['fields']}")

        self.max_layouts = max_layouts
        self.max_fields = max_fields

        self.begin = Signal()
        self.entry_valid = Signal()
        self.entry = Signal(MAP_ENTRY_LAYOUT)
        self.commit = Signal()
        self.abort = Signal()
        self.invalidate = Signal()
        self.descriptor_generation = Signal(16)
        self.candidate_descriptor_generation = Signal(16)
        self.candidate_map_generation = Signal(16)
        self.candidate_entry_count = Signal(8)
        self.candidate_layout_count = Signal(8)
        self.candidate_entries_crc32 = Signal(32)

        self.busy = Signal()
        self.commit_ack = Signal()
        self.commit_error = Signal(4)
        self.commit_error_entry_index = Signal(8, init=0xFF)

        self.active_bank = Signal()
        self.active_valid = Signal()
        self.active_descriptor_generation = Signal(16)
        self.active_generation = Signal(16)
        self.active_entry_count = Signal(7)
        self.active_layout_count = Signal(5)

        self.lookup_index = Signal(range(max_fields))
        self.lookup_valid = Signal()
        self.lookup_entry = Signal(MAP_ENTRY_LAYOUT)

        self.layout_interface = Signal(8)
        self.layout_endpoint = Signal(8)
        self.layout_report_id = Signal(8)
        self.query_valid = Signal()
        self.query_ready = Signal()
        self.result_valid = Signal()
        self.layout_found = Signal()
        self.layout_index = Signal(range(max_layouts))
        self.layout_report_length = Signal(7)

        self._candidate_layout_capture_active = Signal()
        self._candidate_layout_dispatch_active = Signal()
        self._candidate_layout_interface_q = Signal(8)
        self._candidate_layout_endpoint_q = Signal(8)
        self._candidate_layout_report_id_q = Signal(8)
        self._candidate_layout_report_length_q = Signal(8)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()

        write_ports = []
        validation_ports = []
        lookup_ports = []
        for bank in range(2):
            memory = Memory(shape=MAP_ENTRY_LAYOUT, depth=self.max_fields, init=[])
            setattr(m.submodules, f"entry_bank_{bank}", memory)
            write_ports.append(memory.write_port(domain="usb"))
            validation_ports.append(memory.read_port(domain="usb"))
            lookup_ports.append(memory.read_port(domain="usb"))

        occupancy_memory = Memory(shape=unsigned(self.max_layouts), depth=512, init=[])
        m.submodules.validation_occupancy = occupancy_memory
        occupancy_write = occupancy_memory.write_port(domain="usb")
        occupancy_read = occupancy_memory.read_port(domain="usb")

        bank_valid = [Signal(name=f"bank_valid_{bank}") for bank in range(2)]
        bank_descriptor_generations = [
            Signal(16, name=f"bank_descriptor_generation_{bank}") for bank in range(2)
        ]
        bank_map_generations = [Signal(16, name=f"bank_map_generation_{bank}") for bank in range(2)]
        bank_entry_counts = [Signal(7, name=f"bank_entry_count_{bank}") for bank in range(2)]
        bank_layout_counts = [Signal(5, name=f"bank_layout_count_{bank}") for bank in range(2)]

        inactive_bank = ~self.active_bank

        layout_directory = Memory(
            shape=LAYOUT_DIRECTORY_LAYOUT,
            depth=2 * LIMITS["layouts"],
            init=[],
            attrs={"ram_style": "distributed"},
        )
        m.submodules.layout_directory = layout_directory
        layout_directory_write = layout_directory.write_port(domain="usb")
        runtime_layout_read = layout_directory.read_port(domain="usb")
        candidate_layout_read = layout_directory.read_port(domain="usb")
        directory_write_enable = Signal()
        directory_write_address = Signal(5)
        directory_write_data = Signal(LAYOUT_DIRECTORY_LAYOUT)
        runtime_read_enable = Signal()
        runtime_read_address = Signal(5)
        candidate_read_enable = Signal()
        candidate_read_address = Signal(5)

        capture_active = Signal()
        validation_active = Signal()
        received_count = Signal(range(self.max_fields + 2))
        receive_overflow = Signal()

        latched_descriptor_generation = Signal(16)
        latched_map_generation = Signal(16)
        latched_entry_count = Signal(8)
        latched_layout_count = Signal(8)
        latched_entries_crc32 = Signal(32)

        validation_index = Signal(range(self.max_fields))
        crc_byte_index = Signal(range(MAX_PAYLOAD))
        crc = Signal(32, init=CRC32_INIT)
        layout_count = Signal(range(self.max_layouts + 1))
        selected_layout = Signal(range(self.max_layouts))
        candidate_scan_index = Signal(4)
        field_bit_index = Signal(range(32))
        occupancy_clear_index = Signal(range(512))
        occupancy_clear = Signal()
        occupancy_set = Signal()
        pending_error = Signal(4)
        pending_error_entry_index = Signal(8, init=0xFF)
        generation_changed = Signal()
        invalidation_latched = Signal()

        with m.If(
            validation_active & (self.descriptor_generation != latched_descriptor_generation)
        ):
            m.d.usb += generation_changed.eq(1)

        for bank, write_port in enumerate(write_ports):
            m.d.comb += [
                write_port.en.eq(
                    capture_active
                    & self.entry_valid
                    & (received_count < self.max_fields)
                    & (inactive_bank == bank)
                ),
                write_port.addr.eq(received_count[: write_port.addr.shape().width]),
                write_port.data.as_value().eq(self.entry.as_value()),
                validation_ports[bank].addr.eq(validation_index),
                lookup_ports[bank].addr.eq(self.lookup_index),
            ]

        validation_entry = Signal(MAP_ENTRY_LAYOUT)
        validation_entry_q = Signal(MAP_ENTRY_LAYOUT)
        m.d.comb += validation_entry.as_value().eq(
            Mux(
                inactive_bank,
                validation_ports[1].data.as_value(),
                validation_ports[0].data.as_value(),
            )
        )

        lookup_in_range = self.active_valid & (
            self.lookup_index < Array(bank_entry_counts)[self.active_bank]
        )
        lookup_in_range_d = Signal()
        lookup_bank_d = Signal()
        lookup_descriptor_generation_d = Signal(16)
        lookup_map_generation_d = Signal(16)
        m.d.usb += [
            lookup_in_range_d.eq(lookup_in_range),
            lookup_bank_d.eq(self.active_bank),
            lookup_descriptor_generation_d.eq(self.active_descriptor_generation),
            lookup_map_generation_d.eq(self.active_generation),
        ]
        m.d.comb += [
            self.lookup_valid.eq(
                lookup_in_range_d
                & self.active_valid
                & (lookup_bank_d == self.active_bank)
                & (lookup_descriptor_generation_d == self.active_descriptor_generation)
                & (lookup_map_generation_d == self.active_generation)
            ),
            self.lookup_entry.as_value().eq(
                Mux(
                    lookup_bank_d,
                    lookup_ports[1].data.as_value(),
                    lookup_ports[0].data.as_value(),
                )
            ),
        ]

        m.d.comb += [
            self.busy.eq(capture_active | validation_active),
            self.active_descriptor_generation.eq(
                Array(bank_descriptor_generations)[self.active_bank]
            ),
            self.active_generation.eq(Array(bank_map_generations)[self.active_bank]),
            self.active_entry_count.eq(Array(bank_entry_counts)[self.active_bank]),
            self.active_layout_count.eq(Array(bank_layout_counts)[self.active_bank]),
            self.active_valid.eq(
                Array(bank_valid)[self.active_bank]
                & ~self.invalidate
                & ~invalidation_latched
                & (
                    Array(bank_descriptor_generations)[self.active_bank]
                    == self.descriptor_generation
                )
            ),
        ]

        query_busy = Signal()
        query_immediate_miss = Signal()
        query_interface = Signal(8)
        query_endpoint = Signal(8)
        query_report_id = Signal(8)
        query_bank = Signal()
        query_descriptor_generation = Signal(16)
        query_map_generation = Signal(16)
        query_layout_count = Signal(5)
        query_scan_index = Signal(4)
        query_handshake = self.query_valid & self.query_ready
        runtime_record = runtime_layout_read.data
        runtime_record_matches = (
            (runtime_record.interface_number == query_interface)
            & (runtime_record.endpoint_number == query_endpoint)
            & (runtime_record.report_id == query_report_id)
        )
        runtime_snapshot_matches = (
            self.active_valid
            & (self.active_bank == query_bank)
            & (self.active_descriptor_generation == query_descriptor_generation)
            & (self.active_generation == query_map_generation)
        )

        candidate_record = candidate_layout_read.data
        candidate_record_matches = (
            (candidate_record.interface_number == self._candidate_layout_interface_q)
            & (candidate_record.endpoint_number == self._candidate_layout_endpoint_q)
            & (candidate_record.report_id == self._candidate_layout_report_id_q)
        )

        m.d.comb += [
            self.query_ready.eq(~query_busy & ~self.result_valid),
            self._candidate_layout_capture_active.eq(0),
            self._candidate_layout_dispatch_active.eq(0),
            directory_write_enable.eq(0),
            directory_write_address.eq(0),
            directory_write_data.as_value().eq(0),
            runtime_read_enable.eq(0),
            runtime_read_address.eq(0),
            candidate_read_enable.eq(0),
            candidate_read_address.eq(0),
            layout_directory_write.en.eq(directory_write_enable),
            layout_directory_write.addr.eq(directory_write_address),
            layout_directory_write.data.as_value().eq(directory_write_data.as_value()),
            runtime_layout_read.en.eq(runtime_read_enable),
            runtime_layout_read.addr.eq(runtime_read_address),
            candidate_layout_read.en.eq(candidate_read_enable),
            candidate_layout_read.addr.eq(candidate_read_address),
        ]

        m.d.usb += self.result_valid.eq(0)
        with m.If(query_handshake):
            m.d.usb += [
                query_busy.eq(1),
                query_immediate_miss.eq(~self.active_valid | (self.active_layout_count == 0)),
                query_interface.eq(self.layout_interface),
                query_endpoint.eq(self.layout_endpoint),
                query_report_id.eq(self.layout_report_id),
                query_bank.eq(self.active_bank),
                query_descriptor_generation.eq(self.active_descriptor_generation),
                query_map_generation.eq(self.active_generation),
                query_layout_count.eq(self.active_layout_count),
                query_scan_index.eq(0),
                self.layout_found.eq(0),
                self.layout_index.eq(0),
                self.layout_report_length.eq(0),
            ]
            with m.If(self.active_valid & (self.active_layout_count != 0)):
                m.d.comb += [
                    runtime_read_enable.eq(1),
                    runtime_read_address.eq(Cat(Const(0, 4), self.active_bank)),
                ]
        with m.Elif(query_busy):
            with m.If(query_immediate_miss | ~runtime_snapshot_matches):
                m.d.usb += [
                    query_busy.eq(0),
                    self.result_valid.eq(1),
                    self.layout_found.eq(0),
                    self.layout_index.eq(0),
                    self.layout_report_length.eq(0),
                ]
            with m.Elif(runtime_record_matches):
                m.d.usb += [
                    query_busy.eq(0),
                    self.result_valid.eq(1),
                    self.layout_found.eq(1),
                    self.layout_index.eq(query_scan_index),
                    self.layout_report_length.eq(runtime_record.report_length),
                ]
            with m.Elif(query_scan_index == query_layout_count - 1):
                m.d.usb += [
                    query_busy.eq(0),
                    self.result_valid.eq(1),
                    self.layout_found.eq(0),
                    self.layout_index.eq(0),
                    self.layout_report_length.eq(0),
                ]
            with m.Else():
                m.d.comb += [
                    runtime_read_enable.eq(1),
                    runtime_read_address.eq(Cat((query_scan_index + 1)[:4], query_bank)),
                ]
                m.d.usb += query_scan_index.eq(query_scan_index + 1)

        absolute_field_bit = validation_entry_q.bit_offset + field_bit_index
        selected_occupancy_bit = Const(0)
        selected_layout_mask = Const(0, self.max_layouts)
        for slot in reversed(range(self.max_layouts)):
            selected_occupancy_bit = Mux(
                selected_layout == slot,
                occupancy_read.data[slot],
                selected_occupancy_bit,
            )
            selected_layout_mask = Mux(
                selected_layout == slot,
                Const(1 << slot, self.max_layouts),
                selected_layout_mask,
            )
        m.d.comb += [
            occupancy_clear.eq(0),
            occupancy_set.eq(0),
            occupancy_read.addr.eq(absolute_field_bit[: occupancy_read.addr.shape().width]),
            occupancy_write.en.eq(occupancy_clear | occupancy_set),
            occupancy_write.addr.eq(
                Mux(
                    occupancy_clear,
                    occupancy_clear_index,
                    absolute_field_bit[: occupancy_write.addr.shape().width],
                )
            ),
            occupancy_write.data.eq(
                Mux(
                    occupancy_clear,
                    0,
                    occupancy_read.data | selected_layout_mask,
                )
            ),
        ]

        axis_flags = validation_entry_q.flags & _AXIS_FLAGS
        axis_one_hot = (axis_flags != 0) & ((axis_flags & (axis_flags - 1)) == 0)
        has_signed = (validation_entry_q.flags & INJ_MAP_ENTRY_FLAG_SIGNED) != 0
        has_relative = (validation_entry_q.flags & INJ_MAP_ENTRY_FLAG_RELATIVE) != 0
        has_button = (validation_entry_q.flags & INJ_MAP_ENTRY_FLAG_BUTTON) != 0
        supported_button = has_button & ~has_relative & ~has_signed & (axis_flags == 0)
        supported_axis = has_relative & ~has_button & axis_one_hot
        unsupported_field = (
            ((validation_entry_q.flags & ~_ALLOWED_FLAGS) != 0)
            | ~(supported_button | supported_axis)
            | (validation_entry_q.logical_minimum > validation_entry_q.logical_maximum)
        )

        current_crc_byte = validation_entry_q.as_value().word_select(crc_byte_index, 8)
        next_crc = _crc32_byte(crc, current_crc_byte)

        m.d.usb += [
            self.commit_ack.eq(0),
            self.commit_error.eq(MapError.NONE),
            self.commit_error_entry_index.eq(0xFF),
        ]

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.usb += [
                    capture_active.eq(0),
                    validation_active.eq(0),
                ]
                with m.If(self.begin):
                    m.d.usb += [
                        capture_active.eq(1),
                        received_count.eq(0),
                        receive_overflow.eq(0),
                        latched_descriptor_generation.eq(self.candidate_descriptor_generation),
                        latched_map_generation.eq(self.candidate_map_generation),
                        latched_entry_count.eq(self.candidate_entry_count),
                        latched_layout_count.eq(self.candidate_layout_count),
                        latched_entries_crc32.eq(self.candidate_entries_crc32),
                    ]
                    m.next = "RECEIVE"

            with m.State("RECEIVE"):
                with m.If(self.abort | self.invalidate):
                    m.d.usb += capture_active.eq(0)
                    m.next = "IDLE"
                with m.Elif(self.commit):
                    m.d.usb += capture_active.eq(0)
                    with m.If(latched_descriptor_generation != self.descriptor_generation):
                        m.d.usb += [
                            pending_error.eq(MapError.GENERATION),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Elif(
                        self.candidate_descriptor_generation != latched_descriptor_generation
                    ):
                        m.d.usb += [
                            pending_error.eq(MapError.GENERATION),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Elif(self.candidate_map_generation != latched_map_generation):
                        m.d.usb += [
                            pending_error.eq(MapError.MAP_GENERATION),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Elif(self.candidate_entry_count != latched_entry_count):
                        m.d.usb += [
                            pending_error.eq(MapError.FIELD_COUNT),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Elif(self.candidate_layout_count != latched_layout_count):
                        m.d.usb += [
                            pending_error.eq(MapError.LAYOUT_COUNT),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Elif(self.candidate_entries_crc32 != latched_entries_crc32):
                        m.d.usb += [
                            pending_error.eq(MapError.CRC),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Elif(
                        (latched_entry_count > self.max_fields)
                        | receive_overflow
                        | (received_count != latched_entry_count)
                    ):
                        m.d.usb += [
                            pending_error.eq(MapError.FIELD_COUNT),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Elif(latched_layout_count > self.max_layouts):
                        m.d.usb += [
                            pending_error.eq(MapError.LAYOUT_COUNT),
                            pending_error_entry_index.eq(0xFF),
                        ]
                        m.next = "REJECT"
                    with m.Else():
                        m.d.usb += [
                            validation_active.eq(1),
                            validation_index.eq(0),
                            layout_count.eq(0),
                            crc.eq(CRC32_INIT),
                            generation_changed.eq(0),
                            occupancy_clear_index.eq(0),
                        ]
                        with m.If(latched_entry_count == 0):
                            with m.If(
                                (latched_layout_count != 0)
                                | (latched_entries_crc32 != (CRC32_INIT ^ CRC32_XOROUT))
                            ):
                                m.d.usb += [
                                    pending_error.eq(
                                        Mux(
                                            latched_layout_count != 0,
                                            MapError.LAYOUT_COUNT,
                                            MapError.CRC,
                                        )
                                    ),
                                    pending_error_entry_index.eq(0xFF),
                                ]
                                m.next = "REJECT"
                            with m.Else():
                                m.next = "CLEAR_OCCUPANCY"
                        with m.Else():
                            m.next = "CLEAR_OCCUPANCY"
                with m.Elif(self.entry_valid):
                    with m.If(received_count < self.max_fields):
                        m.d.usb += received_count.eq(received_count + 1)
                    with m.Else():
                        m.d.usb += [
                            receive_overflow.eq(1),
                            received_count.eq(self.max_fields + 1),
                        ]

            with m.State("CLEAR_OCCUPANCY"):
                m.d.comb += occupancy_clear.eq(1)
                with m.If(occupancy_clear_index == 511):
                    with m.If(latched_entry_count == 0):
                        m.next = "ACCEPT"
                    with m.Else():
                        m.next = "READ_ENTRY"
                with m.Else():
                    m.d.usb += occupancy_clear_index.eq(occupancy_clear_index + 1)

            with m.State("READ_ENTRY"):
                m.next = "SNAPSHOT_ENTRY"

            with m.State("SNAPSHOT_ENTRY"):
                m.d.usb += validation_entry_q.as_value().eq(validation_entry.as_value())
                m.next = "CHECK_ENTRY"

            with m.State("CHECK_ENTRY"):
                with m.If(
                    validation_entry_q.descriptor_generation != latched_descriptor_generation
                ):
                    m.d.usb += [
                        pending_error.eq(MapError.GENERATION),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(validation_entry_q.map_generation != latched_map_generation):
                    m.d.usb += [
                        pending_error.eq(MapError.MAP_GENERATION),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(validation_entry_q.entry_index != validation_index):
                    m.d.usb += [
                        pending_error.eq(MapError.DUPLICATE_ENTRY),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(validation_entry_q.interface_number >= LIMITS["interfaces"]):
                    m.d.usb += [
                        pending_error.eq(MapError.INTERFACE),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(validation_entry_q.endpoint_number == 0):
                    m.d.usb += [
                        pending_error.eq(MapError.ENDPOINT),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(
                    (validation_entry_q.report_length == 0)
                    | (validation_entry_q.report_length > LIMITS["report_bytes"])
                ):
                    m.d.usb += [
                        pending_error.eq(MapError.REPORT_LENGTH),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(
                    (validation_entry_q.bit_width == 0) | (validation_entry_q.bit_width > 32)
                ):
                    m.d.usb += [
                        pending_error.eq(MapError.FIELD_WIDTH),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(
                    validation_entry_q.bit_offset + validation_entry_q.bit_width
                    > validation_entry_q.report_length * 8
                ):
                    m.d.usb += [
                        pending_error.eq(MapError.BIT_OFFSET),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(
                    (validation_entry_q.report_id != 0) & (validation_entry_q.bit_offset < 8)
                ):
                    m.d.usb += [
                        pending_error.eq(MapError.REPORT_ID_PREFIX),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Elif(unsupported_field):
                    m.d.usb += [
                        pending_error.eq(MapError.UNSUPPORTED_FIELD),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Else():
                    m.d.comb += self._candidate_layout_capture_active.eq(1)
                    m.d.usb += [
                        self._candidate_layout_interface_q.eq(validation_entry_q.interface_number),
                        self._candidate_layout_endpoint_q.eq(validation_entry_q.endpoint_number),
                        self._candidate_layout_report_id_q.eq(validation_entry_q.report_id),
                        self._candidate_layout_report_length_q.eq(validation_entry_q.report_length),
                        candidate_scan_index.eq(0),
                    ]
                    m.next = "DISPATCH_LAYOUT"

            with m.State("DISPATCH_LAYOUT"):
                m.d.comb += self._candidate_layout_dispatch_active.eq(1)
                with m.If(layout_count == 0):
                    m.next = "APPEND_LAYOUT"
                with m.Else():
                    m.d.comb += [
                        candidate_read_enable.eq(1),
                        candidate_read_address.eq(Cat(Const(0, 4), inactive_bank)),
                    ]
                    m.next = "SCAN_LAYOUT"

            with m.State("SCAN_LAYOUT"):
                with m.If(candidate_record_matches):
                    with m.If(
                        candidate_record.report_length != self._candidate_layout_report_length_q
                    ):
                        m.d.usb += [
                            pending_error.eq(MapError.REPORT_LENGTH),
                            pending_error_entry_index.eq(validation_index),
                        ]
                        m.next = "REJECT"
                    with m.Else():
                        m.d.usb += [
                            selected_layout.eq(candidate_scan_index),
                            crc_byte_index.eq(0),
                        ]
                        m.next = "CRC_ENTRY"
                with m.Elif(candidate_scan_index == layout_count - 1):
                    m.next = "APPEND_LAYOUT"
                with m.Else():
                    m.d.comb += [
                        candidate_read_enable.eq(1),
                        candidate_read_address.eq(
                            Cat((candidate_scan_index + 1)[:4], inactive_bank)
                        ),
                    ]
                    m.d.usb += candidate_scan_index.eq(candidate_scan_index + 1)

            with m.State("APPEND_LAYOUT"):
                with m.If(layout_count >= self.max_layouts):
                    m.d.usb += [
                        pending_error.eq(MapError.LAYOUT_COUNT),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Else():
                    m.d.comb += [
                        directory_write_enable.eq(1),
                        directory_write_address.eq(Cat(layout_count[:4], inactive_bank)),
                        directory_write_data.interface_number.eq(
                            self._candidate_layout_interface_q
                        ),
                        directory_write_data.endpoint_number.eq(self._candidate_layout_endpoint_q),
                        directory_write_data.report_id.eq(self._candidate_layout_report_id_q),
                        directory_write_data.report_length.eq(
                            self._candidate_layout_report_length_q
                        ),
                    ]
                    m.d.usb += [
                        selected_layout.eq(layout_count),
                        layout_count.eq(layout_count + 1),
                        crc_byte_index.eq(0),
                    ]
                    m.next = "CRC_ENTRY"

            with m.State("CRC_ENTRY"):
                m.d.usb += crc.eq(next_crc)
                with m.If(crc_byte_index == MAX_PAYLOAD - 1):
                    m.d.usb += field_bit_index.eq(0)
                    m.next = "READ_OCCUPANCY"
                with m.Else():
                    m.d.usb += crc_byte_index.eq(crc_byte_index + 1)

            with m.State("READ_OCCUPANCY"):
                m.next = "OCCUPY_FIELD"

            with m.State("OCCUPY_FIELD"):
                with m.If(selected_occupancy_bit):
                    m.d.usb += [
                        pending_error.eq(MapError.OVERLAP),
                        pending_error_entry_index.eq(validation_index),
                    ]
                    m.next = "REJECT"
                with m.Else():
                    m.d.comb += occupancy_set.eq(1)
                    with m.If(field_bit_index == validation_entry_q.bit_width - 1):
                        with m.If(validation_index == latched_entry_count - 1):
                            with m.If(layout_count != latched_layout_count):
                                m.d.usb += [
                                    pending_error.eq(MapError.LAYOUT_COUNT),
                                    pending_error_entry_index.eq(0xFF),
                                ]
                                m.next = "REJECT"
                            with m.Elif((crc ^ CRC32_XOROUT) != latched_entries_crc32):
                                m.d.usb += [
                                    pending_error.eq(MapError.CRC),
                                    pending_error_entry_index.eq(0xFF),
                                ]
                                m.next = "REJECT"
                            with m.Else():
                                m.next = "ACCEPT"
                        with m.Else():
                            m.d.usb += validation_index.eq(validation_index + 1)
                            m.next = "READ_ENTRY"
                    with m.Else():
                        m.d.usb += field_bit_index.eq(field_bit_index + 1)
                        m.next = "READ_OCCUPANCY"

            with m.State("ACCEPT"):
                with m.If(
                    generation_changed
                    | (self.descriptor_generation != latched_descriptor_generation)
                ):
                    m.d.usb += [
                        validation_active.eq(0),
                        self.commit_ack.eq(1),
                        self.commit_error.eq(MapError.GENERATION),
                        self.commit_error_entry_index.eq(0xFF),
                    ]
                with m.Else():
                    for bank in range(2):
                        with m.If(inactive_bank == bank):
                            m.d.usb += [
                                bank_valid[bank].eq(1),
                                bank_descriptor_generations[bank].eq(latched_descriptor_generation),
                                bank_map_generations[bank].eq(latched_map_generation),
                                bank_entry_counts[bank].eq(latched_entry_count),
                                bank_layout_counts[bank].eq(latched_layout_count),
                            ]
                    m.d.usb += [
                        self.active_bank.eq(inactive_bank),
                        validation_active.eq(0),
                        self.commit_ack.eq(1),
                        self.commit_error.eq(MapError.NONE),
                        self.commit_error_entry_index.eq(0xFF),
                    ]
                m.next = "IDLE"

            with m.State("REJECT"):
                m.d.usb += [
                    validation_active.eq(0),
                    self.commit_ack.eq(1),
                    self.commit_error.eq(pending_error),
                    self.commit_error_entry_index.eq(pending_error_entry_index),
                ]
                m.next = "IDLE"

        # Link/session invalidation is permanent: old banks cannot become
        # active again merely because readiness returns. A new candidate begin
        # releases the latch, after both old bank-valid bits have been cleared.
        with m.If(self.invalidate):
            m.d.usb += invalidation_latched.eq(1)
        with m.Elif(self.begin & ~self.busy):
            m.d.usb += invalidation_latched.eq(0)
        with m.If(self.invalidate | invalidation_latched):
            for valid in bank_valid:
                m.d.usb += valid.eq(0)

        return m
