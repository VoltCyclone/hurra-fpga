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

from amaranth import Elaboratable, Module, Mux, Signal, unsigned
from amaranth.lib.memory import Memory


class ControlRelay(Elaboratable):
    def __init__(self, *, buffer_bytes: int = 64):
        self._buffer_bytes = buffer_bytes

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
        #: Abandon a request whose OUT payload is still being captured -- the
        #: AUX host moved on (new SETUP, bus reset) before sending it all.
        #: Ignored once the engine has been started: an in-flight engine
        #: transfer cannot be cancelled, only waited out.
        self.abort = Signal()
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
        #: High only while the control engine is actually working for us.
        #: This, not request_pending, is what pre-empts the interrupt
        #: pollers: request_pending also covers time spent waiting on the AUX
        #: host's data and status stages, and starving the pollers for that
        #: would let a slow or absent AUX host silence the controller.
        self.engine_owned = Signal()
        #: The enumerator hands the engine over only once TARGET is
        #: enumerated. Issuing before then would drop our start pulse and let
        #: the enumerator's own traffic be taken as our answer.
        self.enable = Signal()

    def elaborate(self, platform):
        del platform
        m = Module()

        buffer = Memory(shape=unsigned(8), depth=self._buffer_bytes, init=[])
        m.submodules.buffer = buffer
        write_port = buffer.write_port(domain="usb")
        read_port = buffer.read_port(domain="comb")
        out_read_port = buffer.read_port(domain="comb")

        latched_type = Signal(8)
        latched_request = Signal(8)
        latched_value = Signal(16)
        latched_index = Signal(16)
        latched_length = Signal(16)
        cursor = Signal(range(self._buffer_bytes + 1))
        # Set for one cycle immediately after a byte is captured, so a
        # data-valid strobe that stays asserted across multiple cycles
        # (as the target's IN data stage may hold it) is not consumed twice.
        gap = Signal()

        # Direction lives in bit 7 of bmRequestType: 1 = device->host.
        is_in_transfer = latched_type[7]

        # The control engine indexes the OUT payload with a 16-bit counter,
        # but the buffer address is only wide enough for buffer_bytes. Slicing
        # the low bits would silently wrap past the end and serve an unrelated
        # byte as if it were payload, so bound it explicitly and serve zero
        # out of range -- a wrong byte is far worse than a zero byte, because
        # the target would sign it as if it were the host's data.
        out_index_in_range = self.ctl_out_index < self._buffer_bytes

        m.d.comb += [
            read_port.addr.eq(self.read_addr),
            self.read_data.eq(read_port.data),
            out_read_port.addr.eq(Mux(out_index_in_range, self.ctl_out_index, 0)),
            self.ctl_out_payload.eq(Mux(out_index_in_range, out_read_port.data, 0)),
            self.ctl_request_type.eq(latched_type),
            self.ctl_request.eq(latched_request),
            self.ctl_value.eq(latched_value),
            self.ctl_index.eq(latched_index),
            self.ctl_length.eq(latched_length),
        ]

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.comb += self.request_ready.eq(self.enable)
                with m.If(self.request_valid & self.enable):
                    m.d.usb += [
                        latched_type.eq(self.request_type),
                        latched_request.eq(self.request),
                        latched_value.eq(self.value),
                        latched_index.eq(self.index),
                        latched_length.eq(self.length),
                        cursor.eq(0),
                        gap.eq(0),
                        self.overflow.eq(0),
                        self.response_error.eq(0),
                        self.response_length.eq(0),
                        self.request_pending.eq(1),
                    ]
                    with m.If(self.length > self._buffer_bytes):
                        m.d.usb += [self.overflow.eq(1), self.response_error.eq(1)]
                        m.next = "COMPLETE"
                    with m.Elif(self.request_type[7] | (self.length == 0)):
                        # Device->host, or no data stage at all: nothing to
                        # collect from the AUX side before issuing.
                        m.next = "ISSUE"
                    with m.Else():
                        m.next = "CAPTURE_OUT"

            with m.State("CAPTURE_OUT"):
                # Host->device. Take the payload from the AUX side into the
                # bounce buffer before issuing, so the control engine can
                # index it at its own pace during the OUT data stage.
                with m.If(self.out_valid):
                    m.d.comb += [
                        write_port.addr.eq(cursor),
                        write_port.data.eq(self.out_data),
                        write_port.en.eq(1),
                    ]
                    m.d.usb += cursor.eq(cursor + 1)
                with m.If(self.abort):
                    # Nothing has reached the engine yet, so giving up here is
                    # safe. Without this exit a host that abandoned the
                    # transfer left us here forever, holding request_pending.
                    m.d.usb += [cursor.eq(0), self.response_error.eq(1)]
                    m.next = "COMPLETE"
                with m.Elif(cursor + self.out_valid >= latched_length):
                    # Rewind for the engine's own indexing. This assignment is
                    # later than the increment above, and Amaranth takes the
                    # last, so the final byte is still written.
                    m.d.usb += cursor.eq(0)
                    m.next = "ISSUE"

            with m.State("ISSUE"):
                m.d.comb += self.engine_owned.eq(1)
                # The engine samples start only in its own IDLE state, so a
                # pulse while it is busy would simply be lost -- and we would
                # then wait on a done that belongs to someone else.
                with m.If(~self.enable):
                    m.d.usb += self.response_error.eq(1)
                    m.next = "COMPLETE"
                with m.Elif(~self.ctl_busy):
                    m.d.comb += self.ctl_start.eq(1)
                    m.next = "AWAIT"

            with m.State("AWAIT"):
                # No timeout of our own. The engine is guaranteed to finish --
                # every one of its wait states ends in TIMEOUT (500 ms of NAKs)
                # or DISCONNECTED -- whereas giving up first would return us to
                # IDLE with the engine still mid-transfer, and its late done
                # and data would then be taken as the NEXT request's answer.
                # Keeping the AUX host from waiting too long is the handler's
                # job; it stalls the host and drains us separately.
                m.d.comb += self.engine_owned.eq(1)
                with m.If(~gap & is_in_transfer):
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

            with m.State("COMPLETE"):
                m.d.comb += self.response_valid.eq(1)
                with m.If(self.response_ack):
                    m.d.usb += self.request_pending.eq(0)
                    m.next = "IDLE"

        return m
