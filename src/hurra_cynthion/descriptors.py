from dataclasses import dataclass

from amaranth import Cat, Const, Elaboratable, Module, Mux, Signal
from amaranth.lib.memory import Memory

DESCRIPTOR_STORE_SIZE = 4096
DESCRIPTOR_ENTRY_COUNT = 12
MAX_CONFIGURATION_SIZE = 1024
MAX_REPORT_SIZE = 2048
MAX_STRING_SIZE = 255
MAX_PACKET_SIZE = 64
MAX_INTERFACES = 4
MAX_ENDPOINTS = 4  # how many interrupt-IN endpoints may be captured
# Which endpoint *numbers* the clone relay can serve. MAX_ENDPOINTS bounds the
# count; this bounds the numbers. A device may legally declare endpoint 5 while
# declaring only one endpoint, and the two limits are not interchangeable.
RELAY_ENDPOINT_NUMBERS = (1, 2, 3, 4)
MAX_RELAY_ENDPOINT_NUMBER = max(RELAY_ENDPOINT_NUMBERS)

__all__ = [
    "DESCRIPTOR_ENTRY_COUNT",
    "DESCRIPTOR_STORE_SIZE",
    "MAX_CONFIGURATION_SIZE",
    "MAX_ENDPOINTS",
    "MAX_INTERFACES",
    "MAX_PACKET_SIZE",
    "MAX_RELAY_ENDPOINT_NUMBER",
    "MAX_REPORT_SIZE",
    "MAX_STRING_SIZE",
    "RELAY_ENDPOINT_NUMBERS",
    "DescriptorError",
    "DescriptorStore",
    "DeviceDescriptor",
    "EndpointBinding",
    "MalformedDescriptorError",
    "MouseConfiguration",
    "OversizedDescriptorError",
    "UnsupportedTopologyError",
    "parse_device_descriptor",
    "parse_mouse_configuration",
    "validate_string_descriptor",
]


class DescriptorError(ValueError):
    pass


class MalformedDescriptorError(DescriptorError):
    pass


class OversizedDescriptorError(DescriptorError):
    pass


class UnsupportedTopologyError(DescriptorError):
    pass


@dataclass(frozen=True)
class DeviceDescriptor:
    ep0_max_packet_size: int
    manufacturer_index: int
    product_index: int
    serial_number_index: int


@dataclass(frozen=True)
class EndpointBinding:
    interface_number: int
    endpoint_number: int
    max_packet_size: int
    interval: int
    report_length: int


@dataclass(frozen=True)
class MouseConfiguration:
    configuration_value: int
    endpoints: tuple["EndpointBinding", ...]


class DescriptorStore(Elaboratable):
    """Volatile descriptor payloads with a mirrored sequential directory."""

    def __init__(self) -> None:
        self.clear = Signal()
        self.capture_start = Signal()
        self.capture_type = Signal(8)
        self.capture_index = Signal(8)
        self.capture_w_index = Signal(16)
        self.capture_valid = Signal()
        self.capture_data = Signal(8)
        self.capture_ready = Signal()
        self.capture_commit = Signal()
        self.capture_abort = Signal()
        self.capture_busy = Signal()
        self.capture_overflow = Signal()
        self.lookup_type = Signal(8)
        self.lookup_index = Signal(8)
        self.lookup_w_index = Signal(16)
        self.lookup_offset = Signal(13)
        self.lookup_request = Signal()
        self.lookup_ready = Signal()
        self.lookup_response = Signal()
        self.lookup_found = Signal()
        self.lookup_length = Signal(13)
        self.lookup_data = Signal(8)
        self.device_lookup_type = Signal(8)
        self.device_lookup_index = Signal(8)
        self.device_lookup_w_index = Signal(16)
        self.device_lookup_offset = Signal(13)
        self.device_lookup_request = Signal()
        self.device_lookup_ready = Signal()
        self.device_lookup_response = Signal()
        self.device_lookup_found = Signal()
        self.device_lookup_length = Signal(13)
        self.device_lookup_data = Signal(8)
        self.device_lookup_data_valid = Signal()
        # Registered request/response metadata path for timing-critical device
        # serving. The selected descriptor base and length are captured before
        # any live stream offset reaches the block-RAM address input.
        self.serve_request = Signal()
        self.serve_type = Signal(8)
        self.serve_index = Signal(8)
        self.serve_w_index = Signal(16)
        self.serve_ready = Signal()
        self.serve_cancel = Signal()
        self.serve_response = Signal()
        self.serve_found = Signal()
        self.serve_length = Signal(13)
        self.serve_offset = Signal(13)
        self.serve_data = Signal(8)
        self.serve_read_enable = Signal()
        self.serve_data_valid = Signal()
        # Pipelined, slot-indexed source port used to clone this store into a
        # physically separate device-local store. Inputs cross only into
        # registers here; metadata selection and the block-RAM read then stay
        # local to this store's placement region.
        self.copy_read_enable = Signal()
        self.copy_request = Signal()
        self.copy_ready = Signal()
        self.copy_response = Signal()
        self.copy_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        self.copy_offset = Signal(13)
        self.copy_valid = Signal()
        self.copy_type = Signal(8)
        self.copy_index = Signal(8)
        self.copy_w_index = Signal(16)
        self.copy_length = Signal(13)
        self.copy_data = Signal(8)
        self.copy_data_valid = Signal()
        # Dedicated slot-indexed export port. Descriptor telemetry must not
        # contend with the timing-critical device serve/copy read port.
        self.export_read_enable = Signal()
        self.export_request = Signal()
        self.export_ready = Signal()
        self.export_response = Signal()
        self.export_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        self.export_offset = Signal(13)
        self.export_valid = Signal()
        self.export_type = Signal(8)
        self.export_index = Signal(8)
        self.export_w_index = Signal(16)
        self.export_length = Signal(13)
        self.export_data = Signal(8)
        self.descriptor_generation = Signal(16)
        #: One-cycle strobe, asserted on exactly the cycles where
        #: ``descriptor_generation`` takes a new value.
        #:
        #: Consumers used to re-derive this by keeping their own delayed copy
        #: and comparing (``generation != generation_d``). That put a 16-bit
        #: comparator, fed by a bus crossing most of the die, at the head of
        #: every cone that reacts to a descriptor change. The producer already
        #: knows when it increments, so it says so instead. Both registers
        #: update on the same edge, making the strobe cycle-identical to the
        #: comparison it replaces -- including when ``clear`` is held high for
        #: several cycles and the generation advances on each one.
        self.descriptor_generation_changed = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.descriptor_memory = descriptor_memory = Memory(
            shape=8, depth=DESCRIPTOR_STORE_SIZE, init=[]
        )
        lookup_payload_read = descriptor_memory.read_port(domain="usb")
        shared_payload_read = descriptor_memory.read_port(domain="usb")
        export_payload_read = descriptor_memory.read_port(domain="usb")
        payload_write = descriptor_memory.write_port(domain="usb")

        # The directory is intentionally duplicated. The serving FSM never
        # contends with capture, diagnostic lookup, export, or copy metadata.
        m.submodules.serve_directory = serve_directory = Memory(
            shape=64, depth=DESCRIPTOR_ENTRY_COUNT, init=[], attrs={"ram_style": "distributed"}
        )
        m.submodules.admin_directory = admin_directory = Memory(
            shape=64, depth=DESCRIPTOR_ENTRY_COUNT, init=[], attrs={"ram_style": "distributed"}
        )
        serve_directory_read = serve_directory.read_port(domain="usb")
        serve_directory_write = serve_directory.write_port(domain="usb")
        admin_directory_read = admin_directory.read_port(domain="usb")
        admin_directory_write = admin_directory.write_port(domain="usb")

        valid_bitmap = Signal(DESCRIPTOR_ENTRY_COUNT)
        allocator = Signal(range(DESCRIPTOR_STORE_SIZE + 1))

        capture_active = Signal()
        capture_alloc_pending = Signal()
        capture_allocated = Signal()
        capture_rejected = Signal()
        capture_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        capture_base = Signal(13)
        capture_length = Signal(13)
        captured_type = Signal(8)
        captured_index = Signal(8)
        captured_w_index = Signal(16)

        directory_record = Cat(
            captured_type,
            captured_index,
            captured_w_index,
            capture_base,
            capture_length,
            Const(0, 6),
        )
        capture_commit_ok = (
            capture_active
            & capture_allocated
            & ~capture_rejected
            & ~self.capture_overflow
            & self.capture_commit
            & ~self.clear
        )
        m.d.comb += [
            payload_write.en.eq(self.capture_valid & self.capture_ready),
            payload_write.addr.eq(allocator[:12]),
            payload_write.data.eq(self.capture_data),
            serve_directory_write.en.eq(capture_commit_ok),
            serve_directory_write.addr.eq(capture_slot),
            serve_directory_write.data.eq(directory_record),
            admin_directory_write.en.eq(capture_commit_ok),
            admin_directory_write.addr.eq(capture_slot),
            admin_directory_write.data.eq(directory_record),
            self.capture_busy.eq(capture_active),
            self.capture_ready.eq(
                capture_active
                & capture_allocated
                & ~capture_rejected
                & ~self.capture_overflow
                & (allocator < DESCRIPTOR_STORE_SIZE)
            ),
        ]

        # One-entry request queues prevent concurrent admin clients from losing
        # pulses while the single sequential directory port is occupied.
        lookup_pending = Signal()
        lookup_pending_type = Signal(8)
        lookup_pending_index = Signal(8)
        lookup_pending_w_index = Signal(16)
        device_pending = Signal()
        device_pending_type = Signal(8)
        device_pending_index = Signal(8)
        device_pending_w_index = Signal(16)
        export_pending = Signal()
        export_pending_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        copy_pending = Signal()
        copy_pending_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))

        _ADMIN_IDLE = 0
        _ADMIN_CAPTURE = 1
        _ADMIN_LOOKUP = 2
        _ADMIN_DEVICE = 3
        _ADMIN_EXPORT = 4
        _ADMIN_COPY = 5
        admin_state = Signal(range(6), init=_ADMIN_IDLE)
        admin_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        admin_primed = Signal()
        admin_type = Signal(8)
        admin_index = Signal(8)
        admin_w_index = Signal(16)

        # A client may queue one request while another client owns the scan,
        # but may not enqueue a second request while its own request is active.
        # Clear suppresses acceptance combinationally on the same cycle.
        m.d.comb += [
            self.lookup_ready.eq(~self.clear & ~lookup_pending & (admin_state != _ADMIN_LOOKUP)),
            self.device_lookup_ready.eq(
                ~self.clear & ~device_pending & (admin_state != _ADMIN_DEVICE)
            ),
            self.export_ready.eq(~self.clear & ~export_pending & (admin_state != _ADMIN_EXPORT)),
            self.copy_ready.eq(~self.clear & ~copy_pending & (admin_state != _ADMIN_COPY)),
        ]

        admin_record = admin_directory_read.data
        admin_record_type = admin_record[0:8]
        admin_record_index = admin_record[8:16]
        admin_record_w_index = admin_record[16:32]
        admin_record_start = admin_record[32:45]
        admin_record_length = admin_record[45:58]
        admin_record_valid = valid_bitmap.bit_select(admin_slot, 1)
        admin_record_matches = (
            admin_record_valid
            & (admin_record_type == admin_type)
            & (admin_record_index == admin_index)
            & (admin_record_w_index == admin_w_index)
        )
        # While a scan is primed, present the next slot one cycle early. The
        # synchronous read data then remains aligned with ``admin_slot`` as
        # that counter advances one entry per clock.
        m.d.comb += admin_directory_read.addr.eq(
            Mux(
                admin_primed & (admin_slot != DESCRIPTOR_ENTRY_COUNT - 1),
                admin_slot + 1,
                admin_slot,
            )
        )

        capture_match_found = Signal()
        capture_match_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        capture_free_found = Signal()
        capture_free_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))

        lookup_cache_valid = Signal()
        lookup_start = Signal(13)
        device_cache_valid = Signal()
        device_start = Signal(13)
        export_start = Signal(13)
        copy_start = Signal(13)

        lookup_in_range = (
            lookup_cache_valid & self.lookup_found & (self.lookup_offset < self.lookup_length)
        )
        lookup_in_range_q = Signal()
        m.d.comb += [
            lookup_payload_read.addr.eq(
                Mux(lookup_in_range, (lookup_start + self.lookup_offset)[:12], 0)
            ),
            self.lookup_data.eq(Mux(lookup_in_range_q, lookup_payload_read.data, 0)),
        ]
        m.d.usb += lookup_in_range_q.eq(lookup_in_range)

        export_in_range = (
            self.export_read_enable & self.export_valid & (self.export_offset < self.export_length)
        )
        export_in_range_q = Signal()
        m.d.comb += [
            export_payload_read.addr.eq(
                Mux(export_in_range, (export_start + self.export_offset)[:12], 0)
            ),
            self.export_data.eq(Mux(export_in_range_q, export_payload_read.data, 0)),
        ]
        m.d.usb += export_in_range_q.eq(export_in_range)

        # The serving directory has its own bounded ascending scan and cached
        # selection. It is cancelled by a new request, setup cancellation,
        # clear, or a successful descriptor mutation.
        serve_busy = Signal()
        serve_primed = Signal()
        serve_slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        serve_type_q = Signal(8)
        serve_index_q = Signal(8)
        serve_w_index_q = Signal(16)
        serve_selected = Signal()
        serve_start = Signal(13)

        serve_record = serve_directory_read.data
        serve_record_type = serve_record[0:8]
        serve_record_index = serve_record[8:16]
        serve_record_w_index = serve_record[16:32]
        serve_record_start = serve_record[32:45]
        serve_record_length = serve_record[45:58]
        serve_record_valid = valid_bitmap.bit_select(serve_slot, 1)
        serve_record_matches = (
            serve_record_valid
            & (serve_record_type == serve_type_q)
            & (serve_record_index == serve_index_q)
            & (serve_record_w_index == serve_w_index_q)
        )
        serve_match_now = serve_busy & serve_primed & serve_record_matches
        m.d.comb += [
            serve_directory_read.addr.eq(
                Mux(
                    serve_primed & (serve_slot != DESCRIPTOR_ENTRY_COUNT - 1),
                    serve_slot + 1,
                    serve_slot,
                )
            ),
            self.serve_ready.eq(
                ~self.clear
                & ~capture_commit_ok
                & ((~serve_busy & ~serve_selected) | self.serve_cancel)
            ),
        ]

        serve_payload_in_range = (
            ((serve_selected & self.serve_read_enable) | serve_match_now)
            & Mux(serve_match_now, 1, self.serve_found)
            & (self.serve_offset < Mux(serve_match_now, serve_record_length, self.serve_length))
        )
        copy_payload_in_range = (
            self.copy_read_enable & self.copy_valid & (self.copy_offset < self.copy_length)
        )
        device_payload_in_range = (
            device_cache_valid
            & self.device_lookup_found
            & (self.device_lookup_offset < self.device_lookup_length)
        )
        shared_owner = Signal(2)
        shared_owner_q = Signal(2)
        m.d.comb += [
            shared_owner.eq(
                Mux(
                    serve_payload_in_range,
                    1,
                    Mux(copy_payload_in_range, 2, Mux(device_payload_in_range, 3, 0)),
                )
            ),
            shared_payload_read.addr.eq(
                Mux(
                    serve_payload_in_range,
                    (Mux(serve_match_now, serve_record_start, serve_start) + self.serve_offset)[
                        :12
                    ],
                    Mux(
                        copy_payload_in_range,
                        (copy_start + self.copy_offset)[:12],
                        Mux(
                            device_payload_in_range,
                            (device_start + self.device_lookup_offset)[:12],
                            0,
                        ),
                    ),
                )
            ),
            self.serve_data.eq(Mux(shared_owner_q == 1, shared_payload_read.data, 0)),
            self.copy_data.eq(Mux(shared_owner_q == 2, shared_payload_read.data, 0)),
            self.device_lookup_data.eq(Mux(shared_owner_q == 3, shared_payload_read.data, 0)),
            self.serve_data_valid.eq((shared_owner_q == 1) & ~self.clear),
            self.copy_data_valid.eq((shared_owner_q == 2) & ~self.clear),
            self.device_lookup_data_valid.eq((shared_owner_q == 3) & ~self.clear),
        ]
        m.d.usb += shared_owner_q.eq(shared_owner)

        m.d.usb += [
            self.lookup_response.eq(0),
            self.device_lookup_response.eq(0),
            self.export_response.eq(0),
            self.copy_response.eq(0),
            self.serve_response.eq(0),
        ]

        m.d.usb += self.descriptor_generation_changed.eq(0)

        with m.If(self.clear):
            m.d.usb += [
                valid_bitmap.eq(0),
                allocator.eq(0),
                capture_active.eq(0),
                capture_alloc_pending.eq(0),
                capture_allocated.eq(0),
                capture_rejected.eq(0),
                capture_base.eq(0),
                capture_length.eq(0),
                self.capture_overflow.eq(0),
                admin_state.eq(_ADMIN_IDLE),
                admin_primed.eq(0),
                lookup_pending.eq(0),
                device_pending.eq(0),
                export_pending.eq(0),
                copy_pending.eq(0),
                lookup_cache_valid.eq(0),
                self.lookup_found.eq(0),
                self.lookup_length.eq(0),
                device_cache_valid.eq(0),
                self.device_lookup_found.eq(0),
                self.device_lookup_length.eq(0),
                self.export_valid.eq(0),
                self.export_length.eq(0),
                self.copy_valid.eq(0),
                self.copy_length.eq(0),
                serve_busy.eq(0),
                serve_selected.eq(0),
                serve_primed.eq(0),
                self.serve_found.eq(0),
                self.serve_length.eq(0),
                self.descriptor_generation.eq(self.descriptor_generation + 1),
                self.descriptor_generation_changed.eq(1),
            ]
        with m.Else():
            with m.If(self.lookup_request & self.lookup_ready):
                m.d.usb += [
                    lookup_pending.eq(1),
                    lookup_pending_type.eq(self.lookup_type),
                    lookup_pending_index.eq(self.lookup_index),
                    lookup_pending_w_index.eq(self.lookup_w_index),
                ]
            with m.If(self.device_lookup_request & self.device_lookup_ready):
                m.d.usb += [
                    device_pending.eq(1),
                    device_pending_type.eq(self.device_lookup_type),
                    device_pending_index.eq(self.device_lookup_index),
                    device_pending_w_index.eq(self.device_lookup_w_index),
                ]
            with m.If(self.export_request & self.export_ready):
                m.d.usb += [
                    export_pending.eq(1),
                    export_pending_slot.eq(self.export_slot),
                ]
            with m.If(self.copy_request & self.copy_ready):
                m.d.usb += [
                    copy_pending.eq(1),
                    copy_pending_slot.eq(self.copy_slot),
                ]

            with m.If(~capture_active & self.capture_start):
                m.d.usb += [
                    capture_active.eq(1),
                    capture_alloc_pending.eq(1),
                    capture_allocated.eq(0),
                    capture_rejected.eq(0),
                    capture_base.eq(allocator),
                    capture_length.eq(0),
                    captured_type.eq(self.capture_type),
                    captured_index.eq(self.capture_index),
                    captured_w_index.eq(self.capture_w_index),
                    self.capture_overflow.eq(0),
                ]
            with m.Elif(capture_active):
                with m.If(self.capture_abort):
                    m.d.usb += [
                        allocator.eq(capture_base),
                        capture_active.eq(0),
                        capture_alloc_pending.eq(0),
                        capture_allocated.eq(0),
                        capture_rejected.eq(0),
                        self.capture_overflow.eq(0),
                    ]
                with m.Elif(self.capture_commit):
                    m.d.usb += [
                        capture_active.eq(0),
                        capture_alloc_pending.eq(0),
                        capture_allocated.eq(0),
                    ]
                    with m.If(~capture_allocated | self.capture_overflow | capture_rejected):
                        m.d.usb += allocator.eq(capture_base)
                    with m.Else():
                        m.d.usb += [
                            valid_bitmap.bit_select(capture_slot, 1).eq(1),
                            lookup_cache_valid.eq(0),
                            device_cache_valid.eq(0),
                            self.export_valid.eq(0),
                            self.copy_valid.eq(0),
                            serve_busy.eq(0),
                            serve_selected.eq(0),
                            serve_primed.eq(0),
                            self.serve_found.eq(0),
                            self.serve_length.eq(0),
                        ]
                with m.Elif(self.capture_valid & capture_allocated):
                    with m.If(self.capture_ready):
                        m.d.usb += [
                            allocator.eq(allocator + 1),
                            capture_length.eq(capture_length + 1),
                        ]
                    with m.Else():
                        m.d.usb += self.capture_overflow.eq(1)

            with m.If(
                (admin_state == _ADMIN_CAPTURE)
                & (
                    (self.capture_abort & capture_active)
                    | (self.capture_commit & capture_active & capture_allocated)
                )
            ):
                m.d.usb += [admin_state.eq(_ADMIN_IDLE), admin_primed.eq(0)]
            with m.Else(), m.Switch(admin_state):
                with m.Case(_ADMIN_IDLE):
                    m.d.usb += admin_primed.eq(0)
                    with m.If(capture_alloc_pending):
                        m.d.usb += [
                            capture_alloc_pending.eq(0),
                            capture_match_found.eq(0),
                            capture_free_found.eq(0),
                            admin_type.eq(captured_type),
                            admin_index.eq(captured_index),
                            admin_w_index.eq(captured_w_index),
                            admin_slot.eq(0),
                            admin_state.eq(_ADMIN_CAPTURE),
                        ]
                    with m.Elif(lookup_pending):
                        m.d.usb += [
                            lookup_pending.eq(0),
                            admin_type.eq(lookup_pending_type),
                            admin_index.eq(lookup_pending_index),
                            admin_w_index.eq(lookup_pending_w_index),
                            admin_slot.eq(0),
                            admin_state.eq(_ADMIN_LOOKUP),
                        ]
                    with m.Elif(device_pending):
                        m.d.usb += [
                            device_pending.eq(0),
                            admin_type.eq(device_pending_type),
                            admin_index.eq(device_pending_index),
                            admin_w_index.eq(device_pending_w_index),
                            admin_slot.eq(0),
                            admin_state.eq(_ADMIN_DEVICE),
                        ]
                    with m.Elif(export_pending):
                        m.d.usb += [
                            export_pending.eq(0),
                            admin_slot.eq(export_pending_slot),
                            admin_state.eq(_ADMIN_EXPORT),
                        ]
                    with m.Elif(copy_pending):
                        m.d.usb += [
                            copy_pending.eq(0),
                            admin_slot.eq(copy_pending_slot),
                            admin_state.eq(_ADMIN_COPY),
                        ]

                with m.Case(_ADMIN_CAPTURE):
                    with m.If(~capture_active):
                        m.d.usb += [admin_state.eq(_ADMIN_IDLE), admin_primed.eq(0)]
                    with m.Elif(~admin_primed):
                        m.d.usb += admin_primed.eq(1)
                    with m.Else():
                        current_match = admin_record_matches
                        current_free = ~admin_record_valid
                        with m.If(current_match & ~capture_match_found):
                            m.d.usb += [
                                capture_match_found.eq(1),
                                capture_match_slot.eq(admin_slot),
                            ]
                        with m.If(current_free & ~capture_free_found):
                            m.d.usb += [
                                capture_free_found.eq(1),
                                capture_free_slot.eq(admin_slot),
                            ]
                        with m.If(admin_slot == DESCRIPTOR_ENTRY_COUNT - 1):
                            any_match = capture_match_found | current_match
                            any_free = capture_free_found | current_free
                            selected_match_slot = Mux(
                                capture_match_found, capture_match_slot, admin_slot
                            )
                            selected_free_slot = Mux(
                                capture_free_found, capture_free_slot, admin_slot
                            )
                            m.d.usb += [
                                capture_allocated.eq(1),
                                capture_rejected.eq(~any_match & ~any_free),
                                capture_slot.eq(
                                    Mux(any_match, selected_match_slot, selected_free_slot)
                                ),
                                self.capture_overflow.eq(~any_match & ~any_free),
                                admin_state.eq(_ADMIN_IDLE),
                                admin_primed.eq(0),
                            ]
                        with m.Else():
                            m.d.usb += admin_slot.eq(admin_slot + 1)

                with m.Case(_ADMIN_LOOKUP):
                    with m.If(~admin_primed):
                        m.d.usb += admin_primed.eq(1)
                    with m.Elif(admin_record_matches):
                        m.d.usb += [
                            lookup_cache_valid.eq(1),
                            self.lookup_found.eq(1),
                            self.lookup_length.eq(admin_record_length),
                            lookup_start.eq(admin_record_start),
                            self.lookup_response.eq(1),
                            admin_state.eq(_ADMIN_IDLE),
                            admin_primed.eq(0),
                        ]
                    with m.Elif(admin_slot == DESCRIPTOR_ENTRY_COUNT - 1):
                        m.d.usb += [
                            lookup_cache_valid.eq(1),
                            self.lookup_found.eq(0),
                            self.lookup_length.eq(0),
                            lookup_start.eq(0),
                            self.lookup_response.eq(1),
                            admin_state.eq(_ADMIN_IDLE),
                            admin_primed.eq(0),
                        ]
                    with m.Else():
                        m.d.usb += admin_slot.eq(admin_slot + 1)

                with m.Case(_ADMIN_DEVICE):
                    with m.If(~admin_primed):
                        m.d.usb += admin_primed.eq(1)
                    with m.Elif(admin_record_matches):
                        m.d.usb += [
                            device_cache_valid.eq(1),
                            self.device_lookup_found.eq(1),
                            self.device_lookup_length.eq(admin_record_length),
                            device_start.eq(admin_record_start),
                            self.device_lookup_response.eq(1),
                            admin_state.eq(_ADMIN_IDLE),
                            admin_primed.eq(0),
                        ]
                    with m.Elif(admin_slot == DESCRIPTOR_ENTRY_COUNT - 1):
                        m.d.usb += [
                            device_cache_valid.eq(1),
                            self.device_lookup_found.eq(0),
                            self.device_lookup_length.eq(0),
                            device_start.eq(0),
                            self.device_lookup_response.eq(1),
                            admin_state.eq(_ADMIN_IDLE),
                            admin_primed.eq(0),
                        ]
                    with m.Else():
                        m.d.usb += admin_slot.eq(admin_slot + 1)

                with m.Case(_ADMIN_EXPORT):
                    with m.If(~admin_primed):
                        m.d.usb += admin_primed.eq(1)
                    with m.Else():
                        m.d.usb += [
                            self.export_valid.eq(admin_record_valid),
                            self.export_type.eq(admin_record_type),
                            self.export_index.eq(admin_record_index),
                            self.export_w_index.eq(admin_record_w_index),
                            self.export_length.eq(Mux(admin_record_valid, admin_record_length, 0)),
                            export_start.eq(admin_record_start),
                            self.export_response.eq(1),
                            admin_state.eq(_ADMIN_IDLE),
                            admin_primed.eq(0),
                        ]

                with m.Case(_ADMIN_COPY):
                    with m.If(~admin_primed):
                        m.d.usb += admin_primed.eq(1)
                    with m.Else():
                        m.d.usb += [
                            self.copy_valid.eq(admin_record_valid),
                            self.copy_type.eq(admin_record_type),
                            self.copy_index.eq(admin_record_index),
                            self.copy_w_index.eq(admin_record_w_index),
                            self.copy_length.eq(Mux(admin_record_valid, admin_record_length, 0)),
                            copy_start.eq(admin_record_start),
                            self.copy_response.eq(1),
                            admin_state.eq(_ADMIN_IDLE),
                            admin_primed.eq(0),
                        ]

            # A successful directory mutation must dominate a serve match on
            # the same edge; otherwise the just-stale selection could be
            # revalidated by the scan FSM after the capture logic cancels it.
            with m.If(capture_commit_ok):
                m.d.usb += [
                    serve_busy.eq(0),
                    serve_selected.eq(0),
                    serve_primed.eq(0),
                    self.serve_found.eq(0),
                    self.serve_length.eq(0),
                ]
            with m.Elif(self.serve_cancel & ~(self.serve_request & self.serve_ready)):
                m.d.usb += [
                    serve_busy.eq(0),
                    serve_selected.eq(0),
                    serve_primed.eq(0),
                    self.serve_found.eq(0),
                    self.serve_length.eq(0),
                ]
            with m.Elif(self.serve_request & self.serve_ready):
                m.d.usb += [
                    serve_busy.eq(1),
                    serve_selected.eq(0),
                    serve_primed.eq(0),
                    serve_slot.eq(0),
                    serve_type_q.eq(self.serve_type),
                    serve_index_q.eq(self.serve_index),
                    serve_w_index_q.eq(self.serve_w_index),
                    self.serve_found.eq(0),
                    self.serve_length.eq(0),
                ]
            with m.Elif(serve_busy):
                with m.If(~serve_primed):
                    m.d.usb += serve_primed.eq(1)
                with m.Elif(serve_record_matches):
                    m.d.usb += [
                        serve_busy.eq(0),
                        serve_selected.eq(1),
                        serve_primed.eq(0),
                        serve_start.eq(serve_record_start),
                        self.serve_found.eq(1),
                        self.serve_length.eq(serve_record_length),
                        self.serve_response.eq(1),
                    ]
                with m.Elif(serve_slot == DESCRIPTOR_ENTRY_COUNT - 1):
                    m.d.usb += [
                        serve_busy.eq(0),
                        serve_selected.eq(0),
                        serve_primed.eq(0),
                        self.serve_found.eq(0),
                        self.serve_length.eq(0),
                        self.serve_response.eq(1),
                    ]
                with m.Else():
                    m.d.usb += serve_slot.eq(serve_slot + 1)

        return m


def parse_device_descriptor(data: bytes) -> DeviceDescriptor:
    if len(data) < 18:
        raise MalformedDescriptorError("device descriptor is truncated")
    if len(data) > 18:
        raise OversizedDescriptorError("device descriptor exceeds 18 bytes")
    if data[0] != 18 or data[1] != 1:
        raise MalformedDescriptorError("device descriptor has an invalid header")
    if data[7] not in (8, 16, 32, 64):
        raise MalformedDescriptorError("invalid endpoint-zero maximum packet size")
    if data[17] != 1:
        raise UnsupportedTopologyError("exactly one configuration is required")
    return DeviceDescriptor(
        ep0_max_packet_size=data[7],
        manufacturer_index=data[14],
        product_index=data[15],
        serial_number_index=data[16],
    )


def parse_mouse_configuration(data: bytes) -> MouseConfiguration:
    if len(data) > MAX_CONFIGURATION_SIZE:
        raise OversizedDescriptorError("configuration descriptor exceeds 1024 bytes")
    if len(data) < 9:
        raise MalformedDescriptorError("configuration descriptor is truncated")
    if data[0] != 9 or data[1] != 2:
        raise MalformedDescriptorError("configuration descriptor has an invalid header")

    total_length = int.from_bytes(data[2:4], "little")
    if total_length > MAX_CONFIGURATION_SIZE:
        raise OversizedDescriptorError("declared configuration length exceeds 1024 bytes")
    if total_length != len(data):
        raise MalformedDescriptorError("configuration total length does not match capture")
    declared_interfaces = data[4]
    if declared_interfaces < 1 or declared_interfaces > MAX_INTERFACES:
        raise UnsupportedTopologyError("unsupported interface count")
    if data[5] == 0:
        raise MalformedDescriptorError("configuration value must be nonzero")

    endpoints: list[EndpointBinding] = []
    interface_count = 0
    has_mouse = False
    current_is_hid = False
    current_interface = 0
    current_report_length = 0

    offset = data[0]
    while offset < total_length:
        if total_length - offset < 2:
            raise MalformedDescriptorError("descriptor header is truncated")
        descriptor_length = data[offset]
        descriptor_type = data[offset + 1]
        if descriptor_length < 2:
            raise MalformedDescriptorError("descriptor length must be at least two")
        descriptor_end = offset + descriptor_length
        if descriptor_end > total_length:
            raise MalformedDescriptorError("descriptor extends past configuration")

        if descriptor_type == 4:
            if descriptor_length != 9:
                raise MalformedDescriptorError("interface descriptor length must be nine")
            if data[offset + 3] != 0:
                raise UnsupportedTopologyError("alternate interface settings are unsupported")
            interface_count += 1
            if interface_count > MAX_INTERFACES:
                raise UnsupportedTopologyError("too many interfaces")
            current_interface = data[offset + 2]
            current_is_hid = data[offset + 5] == 3
            current_report_length = 0
            if current_is_hid and data[offset + 7] == 2:
                has_mouse = True

        elif descriptor_type == 0x21:
            # HID descriptor belongs to the current interface; non-HID interfaces skip it.
            if current_is_hid:
                if descriptor_length < 9:
                    raise MalformedDescriptorError("HID descriptor is truncated")
                hid_descriptor_count = data[offset + 5]
                if hid_descriptor_count == 0 or descriptor_length != 6 + 3 * hid_descriptor_count:
                    raise MalformedDescriptorError("HID subordinate descriptor table is malformed")
                for subordinate in range(hid_descriptor_count):
                    subordinate_offset = offset + 6 + subordinate * 3
                    subordinate_type = data[subordinate_offset]
                    subordinate_length = int.from_bytes(
                        data[subordinate_offset + 1 : subordinate_offset + 3], "little"
                    )
                    if subordinate_type == 0x22:
                        if current_report_length != 0:
                            raise MalformedDescriptorError(
                                "multiple report descriptors are invalid"
                            )
                        if subordinate_length == 0:
                            raise MalformedDescriptorError(
                                "report descriptor length must be nonzero"
                            )
                        if subordinate_length > MAX_REPORT_SIZE:
                            raise OversizedDescriptorError("report descriptor exceeds 2048 bytes")
                        current_report_length = subordinate_length

        elif descriptor_type == 5:
            if descriptor_length != 7:
                raise MalformedDescriptorError("endpoint descriptor length must be seven")
            endpoint_address = data[offset + 2]
            attributes = data[offset + 3]
            is_interrupt_in = bool(endpoint_address & 0x80) and (attributes & 0x03) == 3
            # Only a HID interface's interrupt-IN endpoints are captured; OUT and
            # non-interrupt endpoints (e.g. a keyboard's LED endpoint) are skipped.
            if current_is_hid and is_interrupt_in:
                if (endpoint_address & 0x70) != 0 or (endpoint_address & 0x0F) == 0:
                    raise UnsupportedTopologyError("endpoint must be a nonzero IN endpoint")
                maximum_packet_size = int.from_bytes(data[offset + 4 : offset + 6], "little")
                if maximum_packet_size == 0:
                    raise MalformedDescriptorError("endpoint maximum packet size must be nonzero")
                if maximum_packet_size > MAX_PACKET_SIZE:
                    raise UnsupportedTopologyError("endpoint maximum packet size exceeds 64 bytes")
                if data[offset + 6] == 0:
                    raise MalformedDescriptorError("endpoint interval must be nonzero")
                if current_report_length == 0:
                    raise MalformedDescriptorError("HID interface lacks a report descriptor")
                if len(endpoints) >= MAX_ENDPOINTS:
                    raise OversizedDescriptorError("too many interrupt-IN endpoints")
                endpoints.append(
                    EndpointBinding(
                        interface_number=current_interface,
                        endpoint_number=endpoint_address & 0x0F,
                        max_packet_size=maximum_packet_size,
                        interval=data[offset + 6],
                        report_length=current_report_length,
                    )
                )

        offset = descriptor_end

    if interface_count != declared_interfaces:
        raise UnsupportedTopologyError("declared interface count does not match descriptors")
    if not has_mouse:
        raise UnsupportedTopologyError("no HID mouse interface present")
    if not endpoints:
        raise MalformedDescriptorError("no interrupt-IN endpoint found")

    return MouseConfiguration(configuration_value=data[5], endpoints=tuple(endpoints))


def validate_string_descriptor(data: bytes, *, index: int) -> None:
    if len(data) > MAX_STRING_SIZE:
        raise OversizedDescriptorError("string descriptor exceeds 255 bytes")
    if len(data) < 2 or len(data) % 2 or data[0] != len(data) or data[1] != 3:
        raise MalformedDescriptorError("string descriptor has an invalid header or length")
    if index == 0:
        if len(data) < 4:
            raise MalformedDescriptorError("language table must contain a language ID")
        return
    try:
        data[2:].decode("utf-16-le", errors="strict")
    except UnicodeDecodeError as error:
        raise MalformedDescriptorError("string descriptor contains invalid UTF-16LE") from error
