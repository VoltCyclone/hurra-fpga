"""Forward HID class control transfers from the AUX clone to the TARGET device.

The relay is deliberately protocol-agnostic. It never inspects bRequest or
wValue, so a console's authentication handshake -- or any vendor scheme --
passes through as opaque bytes. Correctness is "bytes in equal bytes out",
which is a property simulation can assert without the console present.

It drives the existing ``USBControlTransferEngine`` unmodified; that engine
already decodes direction from ``request_type[7]`` and already has an OUT
data stage.
"""

# ruff: noqa: SIM117

from amaranth import Elaboratable, Module, Signal, unsigned
from amaranth.lib.memory import Memory


class ControlRelay(Elaboratable):
    def __init__(self, *, buffer_bytes: int = 64, timeout_cycles: int = 1_200_000):
        self._buffer_bytes = buffer_bytes
        self._timeout_cycles = timeout_cycles

        # AUX-facing request
        self.request_valid = Signal()
        self.request_ready = Signal()
        self.request_type = Signal(8)
        self.request = Signal(8)
        self.value = Signal(16)
        self.index = Signal(16)
        self.length = Signal(16)
        self.out_valid = Signal()
        self.out_data = Signal(8)

        # AUX-facing response
        self.response_valid = Signal()
        self.response_error = Signal()
        self.response_length = Signal(16)
        self.response_ack = Signal()
        self.overflow = Signal()
        self.timed_out = Signal()
        self.read_addr = Signal(range(buffer_bytes))
        self.read_data = Signal(8)

        # TARGET-facing (drives USBControlTransferEngine)
        self.ctl_start = Signal()
        self.ctl_request_type = Signal(8)
        self.ctl_request = Signal(8)
        self.ctl_value = Signal(16)
        self.ctl_index = Signal(16)
        self.ctl_length = Signal(16)
        self.ctl_out_payload = Signal(8)
        self.ctl_out_index = Signal(16)
        self.ctl_data_ready = Signal()
        self.ctl_busy = Signal()
        self.ctl_done = Signal()
        self.ctl_status = Signal(3)
        self.ctl_transferred = Signal(16)
        self.ctl_data = Signal(8)
        self.ctl_data_valid = Signal()
        self.ctl_data_first = Signal()
        self.ctl_data_last = Signal()

        self.request_pending = Signal()

    def elaborate(self, platform):
        del platform
        m = Module()

        buffer = Memory(shape=unsigned(8), depth=self._buffer_bytes, init=[])
        m.submodules.buffer = buffer
        write_port = buffer.write_port(domain="usb")
        read_port = buffer.read_port(domain="comb")

        latched_type = Signal(8)
        latched_request = Signal(8)
        latched_value = Signal(16)
        latched_index = Signal(16)
        latched_length = Signal(16)
        cursor = Signal(range(self._buffer_bytes + 1))
        timer = Signal(range(self._timeout_cycles + 1))
        # Set for one cycle immediately after a byte is captured, so a
        # data-valid strobe that stays asserted across multiple cycles
        # (as the target's IN data stage may hold it) is not consumed twice.
        gap = Signal()

        m.d.comb += [
            read_port.addr.eq(self.read_addr),
            self.read_data.eq(read_port.data),
            self.ctl_request_type.eq(latched_type),
            self.ctl_request.eq(latched_request),
            self.ctl_value.eq(latched_value),
            self.ctl_index.eq(latched_index),
            self.ctl_length.eq(latched_length),
        ]

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.comb += self.request_ready.eq(1)
                with m.If(self.request_valid):
                    m.d.usb += [
                        latched_type.eq(self.request_type),
                        latched_request.eq(self.request),
                        latched_value.eq(self.value),
                        latched_index.eq(self.index),
                        latched_length.eq(self.length),
                        cursor.eq(0),
                        timer.eq(0),
                        gap.eq(0),
                        self.overflow.eq(0),
                        self.timed_out.eq(0),
                        self.response_error.eq(0),
                        self.response_length.eq(0),
                        self.request_pending.eq(1),
                    ]
                    with m.If(self.length > self._buffer_bytes):
                        m.d.usb += [self.overflow.eq(1), self.response_error.eq(1)]
                        m.next = "COMPLETE"
                    with m.Else():
                        m.next = "ISSUE"

            with m.State("ISSUE"):
                m.d.comb += self.ctl_start.eq(1)
                m.next = "AWAIT"

            with m.State("AWAIT"):
                m.d.usb += timer.eq(timer + 1)
                with m.If(~gap):
                    m.d.comb += self.ctl_data_ready.eq(1)
                    with m.If(self.ctl_data_valid):
                        m.d.comb += [
                            write_port.addr.eq(cursor),
                            write_port.data.eq(self.ctl_data),
                            write_port.en.eq(1),
                        ]
                        m.d.usb += [
                            cursor.eq(cursor + 1),
                            gap.eq(1),
                        ]
                with m.Else():
                    m.d.usb += gap.eq(0)
                with m.If(self.ctl_done):
                    m.d.usb += [
                        self.response_length.eq(self.ctl_transferred),
                        self.response_error.eq(self.ctl_status != 0),
                    ]
                    m.next = "COMPLETE"
                with m.Elif(timer == self._timeout_cycles):
                    m.d.usb += [
                        self.timed_out.eq(1),
                        self.response_error.eq(1),
                    ]
                    m.next = "COMPLETE"

            with m.State("COMPLETE"):
                m.d.comb += self.response_valid.eq(1)
                with m.If(self.response_ack):
                    m.d.usb += self.request_pending.eq(0)
                    m.next = "IDLE"

        return m
