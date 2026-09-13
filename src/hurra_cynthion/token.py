"""UTMI-facing USB token packet generator."""

from amaranth import Cat, Const, Elaboratable, Module, Mux, Signal

SOF_PID = 0x5


def _usb_crc5_value(module: Module, payload):
    """Build the combinational USB CRC-5 expression for an 11-bit value."""
    remainder = Const(0x1F, 5)
    for bit_number in range(11):
        feedback = payload[bit_number] ^ remainder[0]
        next_remainder = Signal(5, name=f"crc_stage_{bit_number + 1}")
        module.d.comb += next_remainder.eq(
            (remainder >> 1) ^ Mux(feedback, Const(0x14, 5), Const(0, 5))
        )
        remainder = next_remainder
    return remainder ^ Const(0x1F, 5)


class USBTokenGenerator(Elaboratable):
    """Emit a three-byte USB token packet using a UTMI-style handshake."""

    def __init__(self) -> None:
        self.start = Signal()
        self.abort = Signal()
        self.pid = Signal(4)
        self.address = Signal(7)
        self.endpoint = Signal(4)
        self.frame = Signal(11)

        self.tx_valid = Signal()
        self.tx_ready = Signal()
        self.tx_data = Signal(8)

        self.busy = Signal()
        self.done = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        module = Module()

        sending = Signal()
        byte_index = Signal(range(3))
        packet = Signal(24)

        payload = Mux(self.pid == SOF_PID, self.frame, Cat(self.address, self.endpoint))
        crc = _usb_crc5_value(module, payload)

        module.d.comb += [
            self.busy.eq(sending),
            self.tx_valid.eq(sending),
            self.tx_data.eq(packet.word_select(byte_index, 8)),
        ]
        module.d.sync += self.done.eq(0)

        with module.If(self.abort):
            module.d.sync += [sending.eq(0), byte_index.eq(0)]
        # Keep the outer condition separate so the following Elif retains send priority.
        with module.Elif(~sending):  # noqa: SIM117
            with module.If(self.start):
                module.d.sync += [
                    packet.eq(Cat(self.pid, ~self.pid, payload, crc)),
                    byte_index.eq(0),
                    sending.eq(1),
                ]
        with module.Elif(self.tx_ready):
            with module.If(byte_index == 2):
                module.d.sync += [
                    sending.eq(0),
                    self.done.eq(1),
                ]
            with module.Else():
                module.d.sync += byte_index.eq(byte_index + 1)

        return module
