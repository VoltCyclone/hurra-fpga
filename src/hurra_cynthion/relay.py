"""Relay the host's tagged HID report stream to per-endpoint device streams."""

from amaranth import Array, Elaboratable, Module, Mux, Signal
from amaranth.lib.memory import Memory
from luna.gateware.stream import StreamInterface

from .descriptors import MAX_PACKET_SIZE, RELAY_ENDPOINT_NUMBERS

__all__ = ["ReportRelay"]


class ReportRelay(Elaboratable):
    def __init__(
        self,
        endpoint_numbers=RELAY_ENDPOINT_NUMBERS,
        fifo_depth: int = 128,
        max_report_bytes: int = MAX_PACKET_SIZE,
    ):
        if fifo_depth < max_report_bytes:
            raise ValueError(
                f"fifo_depth {fifo_depth} cannot hold a whole {max_report_bytes}-byte report; "
                "whole-report admission would refuse every report"
            )
        self.endpoint_numbers = tuple(endpoint_numbers)
        self._fifo_depth = fifo_depth
        self._max_report_bytes = max_report_bytes

        self.report_valid = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()
        self.report_endpoint = Signal(4)
        self.report_ready = Signal()

        # One pulse per *dropped report*, not per dropped byte.
        self.unmatched_report = Signal()
        self.congested_report = Signal()

        self.streams = [StreamInterface() for _ in self.endpoint_numbers]

    def elaborate(self, platform):
        del platform
        m = Module()

        # Each endpoint owns one independent 9-bit by depth block-memory ring.
        # The synchronous read port itself is the held output register: when
        # stalled, no new read is issued and payload/last remain stable.
        read_ports = []
        write_ports = []
        read_pointers = []
        write_pointers = []
        levels = []
        head_valids = []
        dequeues = []
        admit = []
        for index, _epnum in enumerate(self.endpoint_numbers):
            memory = Memory(
                shape=9,
                depth=self._fifo_depth,
                init=[],
                attrs={"ram_style": "distributed"},
            )
            m.submodules[f"queue_memory_{index}"] = memory
            read_ports.append(memory.read_port(domain="usb"))
            write_ports.append(memory.write_port(domain="usb"))
            read_pointers.append(
                Signal(range(self._fifo_depth), name=f"queue_{index}_read_pointer")
            )
            write_pointers.append(
                Signal(range(self._fifo_depth), name=f"queue_{index}_write_pointer")
            )
            levels.append(Signal(range(self._fifo_depth + 1), name=f"queue_{index}_level"))
            head_valids.append(Signal(name=f"queue_{index}_head_valid"))

        for index, stream in enumerate(self.streams):
            dequeue = head_valids[index] & stream.ready
            dequeues.append(dequeue)
            # Whole-report admission: a report is taken only if all of it fits.
            # No ``| dequeue`` term -- that existed to squeeze one more byte
            # into a full queue and has no meaning once admission is decided
            # for a whole report at its first byte.
            admit.append(levels[index] + self._max_report_bytes <= self._fifo_depth)

        # Select the target queue by matching endpoint number.
        selected = Signal(range(len(self.endpoint_numbers)))
        matched = Signal()
        default_index = 0
        sel_expr = default_index
        match_expr = 0
        for index, epnum in enumerate(self.endpoint_numbers):
            is_match = self.report_endpoint == epnum
            sel_expr = Mux(is_match, index, sel_expr)
            match_expr = match_expr | is_match
        m.d.comb += [selected.eq(sel_expr), matched.eq(match_expr)]

        # Per-bucket admission state. The extra bucket absorbs reports for
        # endpoint numbers this relay does not serve, so an unmatched report
        # cannot be mistaken for a continuation of a matched one.
        buckets = len(self.endpoint_numbers) + 1
        bucket = Mux(matched, selected, buckets - 1)
        in_report = Array([Signal(name=f"in_report_{index}") for index in range(buckets)])
        accepting = Array([Signal(name=f"accepting_{index}") for index in range(buckets)])

        # The relay never backpressures: it is the sink in front of a single
        # shared injection engine, and any stall here stops every endpoint at
        # injection.py's OUTPUT state, which for an unmapped report has no
        # other exit.
        m.d.comb += self.report_ready.eq(1)
        admit_bucket = Mux(matched, Array(admit)[selected], 0)
        starting = self.report_valid & ~in_report[bucket]
        taking = self.report_valid & Mux(in_report[bucket], accepting[bucket], admit_bucket)
        m.d.comb += [
            self.unmatched_report.eq(starting & ~matched),
            self.congested_report.eq(starting & matched & ~admit_bucket),
        ]
        with m.If(self.report_valid):
            with m.If(starting):
                m.d.usb += accepting[bucket].eq(admit_bucket)
            # Tracks the byte stream regardless of admission, so a dropped
            # report's remaining bytes are not each re-evaluated as a fresh
            # report start.
            m.d.usb += in_report[bucket].eq(~self.report_last)

        # Write and read sides remain fully independent across endpoints.
        for index, stream in enumerate(self.streams):
            enqueue = taking & matched & (selected == index)
            read_issue = (~head_valids[index] & (levels[index] != 0)) | (
                dequeues[index] & (levels[index] > 1)
            )
            next_read_pointer = Mux(
                read_pointers[index] == self._fifo_depth - 1,
                0,
                read_pointers[index] + 1,
            )
            next_write_pointer = Mux(
                write_pointers[index] == self._fifo_depth - 1,
                0,
                write_pointers[index] + 1,
            )
            m.d.comb += [
                write_ports[index].en.eq(enqueue),
                write_ports[index].addr.eq(write_pointers[index]),
                write_ports[index].data.eq((self.report_last << 8) | self.report_data),
                read_ports[index].en.eq(read_issue),
                read_ports[index].addr.eq(read_pointers[index]),
                stream.valid.eq(head_valids[index]),
                stream.payload.eq(read_ports[index].data[:8]),
                stream.last.eq(read_ports[index].data[8]),
            ]

            with m.If(enqueue):
                m.d.usb += write_pointers[index].eq(next_write_pointer)
            with m.If(read_issue):
                m.d.usb += [
                    read_pointers[index].eq(next_read_pointer),
                    head_valids[index].eq(1),
                ]
            with m.Elif(dequeues[index]):
                m.d.usb += head_valids[index].eq(0)

            with m.If(enqueue & ~dequeues[index]):
                m.d.usb += levels[index].eq(levels[index] + 1)
            with m.Elif(dequeues[index] & ~enqueue):
                m.d.usb += levels[index].eq(levels[index] - 1)

        return m
