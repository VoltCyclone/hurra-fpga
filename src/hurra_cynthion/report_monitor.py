from amaranth import Array, Cat, Elaboratable, Module, Mux, Signal
from amaranth.lib.memory import Memory

from .injection_wire import INJ_TYPE_REPORT_FRAGMENT

__all__ = ["ReportMonitor"]


class ReportMonitor(Elaboratable):
    """Transparent native-report tap with a bounded best-effort telemetry queue."""

    def __init__(self, depth: int = 2, *, drop_counter_bits: int = 32) -> None:
        if depth < 1:
            raise ValueError("depth must be positive")
        if drop_counter_bits < 1:
            raise ValueError("drop counter width must be positive")
        self.depth = depth
        self.drop_counter_bits = drop_counter_bits

        # Native input from the authoritative forwarding path.
        self.report_valid = Signal()
        self.report_ready = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()
        self.report_interface = Signal(8)
        self.report_endpoint = Signal(4)
        self.report_id = Signal(8)
        self.descriptor_generation = Signal(16)

        # Transparent native output. Telemetry occupancy never participates in
        # this ready/valid path.
        self.output_valid = Signal()
        self.output_ready = Signal()
        self.output_data = Signal(8)
        self.output_first = Signal()
        self.output_last = Signal()
        self.output_interface = Signal(8)
        self.output_endpoint = Signal(4)
        self.output_report_id = Signal(8)

        # Fragmented telemetry output.
        self.message_valid = Signal()
        self.message_ready = Signal()
        self.message_type = Signal(8)
        self.message_payload_request = Signal()
        self.message_payload_address = Signal(5)
        self.message_payload_response = Signal()
        self.message_payload_data = Signal(8)
        self.message_payload_cancel = Signal()
        self.monitoring_drops = Signal(drop_counter_bits)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        depth = self.depth

        capture_active = Signal()
        capture_admitted = Signal()
        capture_overflow = Signal()
        capture_index = Signal(range(64))
        capture_slot = Signal(range(depth))
        captured_generation = Signal(16)
        captured_interface = Signal(8)
        captured_endpoint = Signal(8)
        captured_report_id = Signal(8)

        queue_memory = Memory(
            shape=8,
            depth=depth * 64,
            init=[],
            attrs={"ram_style": "distributed"},
        )
        m.submodules.queue_memory = queue_memory
        queue_memory_read = queue_memory.read_port(domain="usb")
        queue_memory_write = queue_memory.write_port(domain="usb")
        queue_read_enable = Signal()
        queue_read_address = Signal(range(depth * 64))
        queue_write_enable = Signal()
        queue_write_address = Signal(range(depth * 64))

        queue_generation = [Signal(16, name=f"queue_{slot}_generation") for slot in range(depth)]
        queue_interface = [Signal(8, name=f"queue_{slot}_interface") for slot in range(depth)]
        queue_endpoint = [Signal(8, name=f"queue_{slot}_endpoint") for slot in range(depth)]
        queue_report_id = [Signal(8, name=f"queue_{slot}_report_id") for slot in range(depth)]
        queue_total = [Signal(16, name=f"queue_{slot}_total") for slot in range(depth)]

        head = Signal(range(depth))
        tail = Signal(range(depth))
        occupied = Signal(range(depth + 1))
        fragment_offset = Signal(16)

        accepted = self.report_valid & self.report_ready
        current_position = Mux(self.report_first, 0, capture_index + 1)
        current_slot = Mux(self.report_first, tail, capture_slot)
        complete_report = (
            accepted
            & self.report_last
            & (self.report_first | capture_active)
            & ~capture_overflow
            & (self.report_first | (capture_index != 63))
        )

        head_generation = Array(queue_generation)[head]
        head_interface = Array(queue_interface)[head]
        head_endpoint = Array(queue_endpoint)[head]
        head_report_id = Array(queue_report_id)[head]
        head_total = Array(queue_total)[head]
        payload_request_q = Signal()
        payload_address_q = Signal(5)
        payload_read_in_range_q = Signal()

        final_fragment = fragment_offset + 17 >= head_total
        dequeue = self.message_valid & self.message_ready & final_fragment
        capture_space_available = (occupied < depth) | dequeue
        report_admitted = Mux(self.report_first, capture_space_available, capture_admitted)
        enqueue = complete_report & report_admitted
        queue_full_drop = complete_report & ~report_admitted
        payload_data_byte = (self.message_payload_address >= 9) & (
            self.message_payload_address < 26
        )
        payload_position = fragment_offset + self.message_payload_address - 9
        payload_read_in_range = payload_data_byte & (payload_position < head_total)

        m.d.comb += [
            self.report_ready.eq(self.output_ready),
            self.output_valid.eq(self.report_valid),
            self.output_data.eq(self.report_data),
            self.output_first.eq(self.report_first),
            self.output_last.eq(self.report_last),
            self.output_interface.eq(self.report_interface),
            self.output_endpoint.eq(self.report_endpoint),
            self.output_report_id.eq(self.report_id),
            self.message_valid.eq(occupied != 0),
            self.message_type.eq(INJ_TYPE_REPORT_FRAGMENT),
            self.message_payload_response.eq(
                payload_request_q & (occupied != 0) & ~self.message_payload_cancel
            ),
            self.message_payload_data.eq(0),
            queue_read_enable.eq(
                self.message_payload_request & (occupied != 0) & payload_data_byte
            ),
            queue_read_address.eq(Cat(Mux(payload_read_in_range, payload_position[:6], 0), head)),
            queue_write_enable.eq(
                accepted
                & (self.report_first | capture_active)
                & (self.report_first | (capture_index != 63))
                & report_admitted
            ),
            queue_write_address.eq(Cat(current_position[:6], current_slot)),
            queue_memory_read.en.eq(queue_read_enable),
            queue_memory_read.addr.eq(queue_read_address),
            queue_memory_write.en.eq(queue_write_enable),
            queue_memory_write.addr.eq(queue_write_address),
            queue_memory_write.data.eq(self.report_data),
        ]

        with m.Switch(payload_address_q):
            with m.Case(0):
                m.d.comb += self.message_payload_data.eq(head_generation[:8])
            with m.Case(1):
                m.d.comb += self.message_payload_data.eq(head_generation[8:16])
            with m.Case(2):
                m.d.comb += self.message_payload_data.eq(head_interface)
            with m.Case(3):
                m.d.comb += self.message_payload_data.eq(head_endpoint)
            with m.Case(4):
                m.d.comb += self.message_payload_data.eq(head_report_id)
            with m.Case(5):
                m.d.comb += self.message_payload_data.eq(fragment_offset[:8])
            with m.Case(6):
                m.d.comb += self.message_payload_data.eq(fragment_offset[8:16])
            with m.Case(7):
                m.d.comb += self.message_payload_data.eq(head_total[:8])
            with m.Case(8):
                m.d.comb += self.message_payload_data.eq(head_total[8:16])
            with m.Case(*range(9, 26)):
                m.d.comb += self.message_payload_data.eq(
                    Mux(payload_read_in_range_q, queue_memory_read.data, 0)
                )

        # Metadata and the queue RAM share one exact one-cycle response
        # contract. Head and fragment offset remain stable until message_ready.
        with m.If(self.message_payload_cancel | (self.message_valid & self.message_ready)):
            m.d.usb += payload_request_q.eq(0)
        with m.Else():
            m.d.usb += [
                payload_request_q.eq(self.message_payload_request & (occupied != 0)),
                payload_address_q.eq(self.message_payload_address),
                payload_read_in_range_q.eq(payload_read_in_range),
            ]

        # Capture only bytes accepted by the authoritative native path.
        with m.If(accepted):
            with m.If(self.report_first):
                m.d.usb += [
                    capture_index.eq(0),
                    capture_active.eq(~self.report_last),
                    capture_admitted.eq(capture_space_available),
                    capture_overflow.eq(0),
                    capture_slot.eq(tail),
                    captured_generation.eq(self.descriptor_generation),
                    captured_interface.eq(self.report_interface),
                    captured_endpoint.eq(self.report_endpoint),
                    captured_report_id.eq(self.report_id),
                ]
            with m.Elif(capture_active):
                with m.If(capture_index == 63):
                    m.d.usb += capture_overflow.eq(1)
                with m.Else():
                    m.d.usb += capture_index.eq(capture_index + 1)
                with m.If(self.report_last):
                    m.d.usb += capture_active.eq(0)

        with m.If(enqueue):
            for slot in range(depth):
                with m.If(current_slot == slot):
                    m.d.usb += [
                        queue_generation[slot].eq(
                            Mux(
                                self.report_first,
                                self.descriptor_generation,
                                captured_generation,
                            )
                        ),
                        queue_interface[slot].eq(
                            Mux(self.report_first, self.report_interface, captured_interface)
                        ),
                        queue_endpoint[slot].eq(
                            Mux(self.report_first, self.report_endpoint, captured_endpoint)
                        ),
                        queue_report_id[slot].eq(
                            Mux(self.report_first, self.report_id, captured_report_id)
                        ),
                        queue_total[slot].eq(current_position + 1),
                    ]
            m.d.usb += tail.eq(Mux(tail == depth - 1, 0, tail + 1))

        with m.If(dequeue):
            m.d.usb += [
                head.eq(Mux(head == depth - 1, 0, head + 1)),
                fragment_offset.eq(0),
            ]
        with m.Elif(self.message_valid & self.message_ready):
            m.d.usb += fragment_offset.eq(fragment_offset + 17)

        with m.If(enqueue & ~dequeue):
            m.d.usb += occupied.eq(occupied + 1)
        with m.Elif(dequeue & ~enqueue):
            m.d.usb += occupied.eq(occupied - 1)

        with m.If(queue_full_drop & (self.monitoring_drops != (1 << self.drop_counter_bits) - 1)):
            m.d.usb += self.monitoring_drops.eq(self.monitoring_drops + 1)

        return m
