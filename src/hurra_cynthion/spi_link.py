"""Deterministic fixed-slot SPI master for the report-injection link."""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from amaranth import Cat, Const, Elaboratable, Module, Mux, Signal
from amaranth.lib.memory import Memory

from .injection_wire import (
    INJ_TYPE_IDLE,
    MAX_PAYLOAD,
    MESSAGE_TYPES,
    SOF,
    pack_slot,
)

__all__ = ["SPISlotMaster"]


def _crc16_byte(remainder, byte):
    """Return one CCITT-FALSE byte update as an Amaranth value."""
    mixed = remainder[8:16] ^ byte
    folded = mixed ^ (mixed >> 4)
    return ((remainder << 8) ^ (folded << 12) ^ (folded << 5) ^ folded)[:16]


class SPISlotMaster(Elaboratable):
    """Deterministic 32-byte mode-0 SPI master in the 60 MHz USB domain."""

    def __init__(
        self,
        *,
        clock_hz: int = 60_000_000,
        slot_cycles: int = 7_500,
        sck_div: int = 4,
        cs_hold_cycles: int = 3,
        cs_setup_cycles: int = 3,
    ) -> None:
        if clock_hz <= 0:
            raise ValueError("clock_hz must be positive")
        if slot_cycles <= 0:
            raise ValueError("slot_cycles must be positive")
        if sck_div < 2 or sck_div % 2:
            raise ValueError("sck_div must be a positive even divider")
        if cs_hold_cycles < 1:
            raise ValueError("cs_hold_cycles must be at least 1")
        if cs_setup_cycles < 2:
            raise ValueError("cs_setup_cycles must be at least 2")
        self.clock_hz = clock_hz
        self.slot_cycles = slot_cycles
        self.sck_div = sck_div
        # A parameter rather than a constant so the requirement can be
        # re-derived if the CH32 clock plan changes, and so a test can
        # construct the 1-cycle (defective) shape explicitly.
        self._cs_hold_cycles = cs_hold_cycles
        # tSU(NSS) >= 2*tHCLK, the same requirement as the hold side. Two
        # cycles (33.33 ns) clears it at 100 MHz (1.67x) and 70 MHz (1.17x),
        # but 1.17x leaves only 4.7 ns for the unconstrained FF-to-pad skew
        # between the SCK and CS_N pads. Three (50.00 ns) is the recommended
        # 3+3 guard band and costs one cycle in 7500.
        self._cs_setup_cycles = cs_setup_cycles

        # Physical mode-0 SPI and sideband pins.
        self.sck = Signal(init=0)
        self.mosi = Signal(init=0)
        self.miso = Signal()
        self.cs_n = Signal(init=1)
        self.mcu_ready = Signal()
        self.sof_tick = Signal()
        self.usb_sync = Signal()

        # One queued decoded transmit message. The master owns framing, CRC,
        # and its direction-local sequence.
        self.tx_valid = Signal()
        self.tx_ready = Signal()
        self.tx_type = Signal(8)
        self.tx_fill_start = Signal()
        self.tx_payload_request = Signal()
        self.tx_payload_address = Signal(5)
        self.tx_payload_response = Signal()
        self.tx_payload_data = Signal(8)

        # One queued decoded receive message.
        self.rx_valid = Signal()
        self.rx_ready = Signal()
        self.rx_type = Signal(8)
        self.rx_sequence = Signal(8)
        self.rx_payload_address = Signal(5)
        self.rx_payload_read_enable = Signal()
        self.rx_payload_data = Signal(8)

        self.slot_start = Signal()
        self.slot_counter = Signal(32)
        self.busy = Signal()
        self.bad_sof_count = Signal(32)
        self.bad_crc_count = Signal(32)
        self.bad_length_count = Signal(32)
        self.bad_type_count = Signal(32)
        self.rx_queue_full_count = Signal(32)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()

        half_period = self.sck_div // 2
        cadence = Signal(range(self.slot_cycles))
        edge_divider = Signal(range(half_period))
        # Holds off edge generation for cs_setup_cycles - 2 cycles after CS
        # falls; at the minimum of 2 it is always zero and the edge generator
        # behaves exactly as it did before the parameter existed.
        setup_pending = Signal(range(max(2, self._cs_setup_cycles - 1)))
        bit_index = Signal(8)
        active = Signal()
        transfer_ready = Signal()

        # The sole queued TX frame is built one byte per USB clock. It remains
        # occupied until the final falling SCK edge of its actual transfer.
        tx_memory = Memory(shape=8, depth=32, init=[], attrs={"ram_style": "distributed"})
        m.submodules.tx_memory = tx_memory
        tx_memory_read = tx_memory.read_port(domain="usb")
        tx_memory_write = tx_memory.write_port(domain="usb")
        tx_sequence = Signal(8)
        tx_building = Signal()
        tx_build_index = Signal(5)
        tx_build_type = Signal(8)
        tx_build_sequence = Signal(8)
        tx_build_crc = Signal(16, init=0xFFFF)
        tx_payload_waiting = Signal()
        tx_queued = Signal()
        tx_byte_shift = Signal(8)
        active_send_message = Signal()

        # The two RX banks share one block RAM. The published bank is never
        # overwritten; every later slot lands in the opposite scratch bank.
        rx_memory = Memory(shape=8, depth=64, init=[], attrs={"ram_style": "distributed"})
        m.submodules.rx_memory = rx_memory
        rx_memory_read = rx_memory.read_port(domain="usb")
        rx_memory_write = rx_memory.write_port(domain="usb")
        rx_published_bank = Signal()
        rx_capture_bank = Signal()
        rx_byte_shift = Signal(8)
        # The synchronizer stage for the raw miso pad, and the single net every
        # RX capture point reads. Eight capture points each sampling the pad
        # directly can resolve one metastable bit differently, so the CRC could
        # validate a byte that was stored differently in RX RAM. miso_q has a
        # full usb cycle (16.67 ns) to settle before any consumer clocks it,
        # and the functional register downstream serves as the second flop.
        miso_q = Signal(name="miso_q")
        rx_crc = Signal(16, init=0xFFFF)
        rx_sof = Signal(8)
        rx_type = Signal(8)
        rx_sequence = Signal(8)
        rx_length = Signal(8)
        rx_crc_low = Signal(8)
        rx_crc_high = Signal(8)
        complete_pending = Signal(range(self._cs_hold_cycles + 1))
        validate_pending = Signal()

        idle_slot = pack_slot(INJ_TYPE_IDLE, 0, b"")
        send_message = self.mcu_ready & tx_queued

        next_bit_index = bit_index + 1
        next_rx_byte = ((rx_byte_shift << 1) | miso_q)[:8]
        rx_byte_index = bit_index[3:8]
        next_tx_byte_index = bit_index[3:8] + 1
        next_idle_byte = Mux(
            next_tx_byte_index == 30,
            idle_slot[30],
            Mux(next_tx_byte_index == 31, idle_slot[31], 0),
        )

        rx_received_crc = Cat(rx_crc_low, rx_crc_high)
        known_type = Const(0)
        for type_ in MESSAGE_TYPES.values():
            known_type = known_type | (rx_type == type_)
        expected_length = Mux(rx_type == INJ_TYPE_IDLE, 0, MAX_PAYLOAD)
        valid_return = (
            transfer_ready
            & (rx_sof == SOF)
            & known_type
            & (rx_length == expected_length)
            & (rx_received_crc == rx_crc)
            & (rx_type != INJ_TYPE_IDLE)
        )

        tx_build_payload = (tx_build_index >= 4) & (tx_build_index < 30)
        tx_payload_accept = tx_build_payload & tx_payload_waiting & self.tx_payload_response
        tx_build_advance = tx_building & (~tx_build_payload | tx_payload_accept)
        tx_build_byte = Mux(
            tx_build_index == 0,
            SOF,
            Mux(
                tx_build_index == 1,
                tx_build_type,
                Mux(
                    tx_build_index == 2,
                    tx_build_sequence,
                    Mux(
                        tx_build_index == 3,
                        MAX_PAYLOAD,
                        Mux(
                            tx_build_payload,
                            self.tx_payload_data,
                            Mux(tx_build_index == 30, tx_build_crc[:8], tx_build_crc[8:16]),
                        ),
                    ),
                ),
            ),
        )
        tx_can_fill = ~tx_building & ~tx_queued
        tx_read_address = Mux(active, next_tx_byte_index, 0)
        rx_payload_slot_address = (self.rx_payload_address + 4)[:5]

        sof_tick_d = Signal()
        m.d.comb += [
            self.tx_fill_start.eq(self.tx_valid & tx_can_fill),
            self.tx_payload_request.eq(
                tx_building & self.tx_valid & tx_build_payload & ~tx_payload_waiting
            ),
            self.tx_payload_address.eq(Mux(tx_build_payload, tx_build_index - 4, 0)),
            self.busy.eq(active | (complete_pending != 0)),
            self.usb_sync.eq(self.sof_tick & ~sof_tick_d),
            tx_memory_write.en.eq(tx_build_advance),
            tx_memory_write.addr.eq(tx_build_index),
            tx_memory_write.data.eq(tx_build_byte),
            tx_memory_read.en.eq(tx_queued | active_send_message),
            tx_memory_read.addr.eq(tx_read_address),
            rx_memory_read.en.eq(self.rx_payload_read_enable & self.rx_valid),
            rx_memory_read.addr.eq(Cat(rx_payload_slot_address, rx_published_bank)),
            self.rx_payload_data.eq(rx_memory_read.data),
            rx_memory_write.en.eq(
                active & (edge_divider == half_period - 1) & self.sck & (bit_index[:3] == 7)
            ),
            rx_memory_write.addr.eq(Cat(rx_byte_index, rx_capture_bank)),
            rx_memory_write.data.eq(next_rx_byte),
        ]
        m.d.usb += [
            miso_q.eq(self.miso),
            sof_tick_d.eq(self.sof_tick),
            self.slot_start.eq(0),
            self.tx_ready.eq(0),
        ]

        # Lock type and sequence on fill-start. Payload bytes are addressed
        # from the plane; CRC and both CRC bytes complete the exact 32 clocks.
        with m.If(self.tx_fill_start):
            m.d.usb += [
                tx_build_type.eq(self.tx_type),
                tx_build_sequence.eq(tx_sequence),
                tx_build_index.eq(0),
                tx_build_crc.eq(0xFFFF),
                tx_payload_waiting.eq(0),
                tx_building.eq(1),
            ]
        with m.Elif(tx_building):
            with m.If(~self.tx_valid):
                m.d.usb += [
                    tx_building.eq(0),
                    tx_build_index.eq(0),
                    tx_build_crc.eq(0xFFFF),
                    tx_payload_waiting.eq(0),
                ]
            with m.Else():
                with m.If(self.tx_payload_request):
                    m.d.usb += tx_payload_waiting.eq(1)
                with m.If(tx_build_advance):
                    with m.If(tx_build_payload):
                        m.d.usb += tx_payload_waiting.eq(0)
                    with m.If(tx_build_index < 30):
                        m.d.usb += tx_build_crc.eq(_crc16_byte(tx_build_crc, tx_build_byte))
                    with m.If(tx_build_index == 31):
                        m.d.usb += [
                            tx_building.eq(0),
                            tx_queued.eq(1),
                            self.tx_ready.eq(1),
                        ]
                    with m.Else():
                        m.d.usb += tx_build_index.eq(tx_build_index + 1)

        # A fixed slot boundary is generated independently of transfer state.
        with m.If(cadence == self.slot_cycles - 1):
            m.d.usb += [
                cadence.eq(0),
                self.slot_start.eq(1),
                self.slot_counter.eq(self.slot_counter + 1),
            ]
            with m.If(~active):
                m.d.usb += [
                    active.eq(1),
                    transfer_ready.eq(self.mcu_ready),
                    self.cs_n.eq(0),
                    self.sck.eq(0),
                    edge_divider.eq(0),
                    setup_pending.eq(self._cs_setup_cycles - 2),
                    bit_index.eq(0),
                    active_send_message.eq(send_message),
                    # Byte zero is always the wire SOF, for idle and message
                    # frames alike. Drive it directly so a queue becoming
                    # occupied one clock before this boundary cannot expose
                    # stale synchronous-RAM read data.
                    tx_byte_shift.eq(SOF),
                    self.mosi.eq((SOF >> 7) & 1),
                    rx_capture_bank.eq(~rx_published_bank),
                    rx_byte_shift.eq(0),
                    rx_crc.eq(0xFFFF),
                    rx_sof.eq(0),
                    rx_type.eq(0),
                    rx_sequence.eq(0),
                    rx_length.eq(0),
                    rx_crc_low.eq(0),
                    rx_crc_high.eq(0),
                ]
                with m.If(send_message):
                    m.d.usb += tx_sequence.eq(tx_sequence + 1)
        with m.Else():
            m.d.usb += cadence.eq(cadence + 1)

        # CPOL=0, CPHA=0: the slave changes MISO on each falling edge and MOSI
        # changes there too. The RX capture is taken in the falling-edge
        # decision cycle reading miso_q, which is the pad as of one cycle
        # earlier. Taking t=0 at the SCK fall and 16.67 ns per usb cycle, the
        # clock edge that latches the pad into miso_q is at t = 50.0 ns; the
        # value reaches rx_byte_shift at the t = 66.67 ns edge. Against the
        # CH32's tV(SO) max 25 ns and th(SO) min 15 ns (Table 3-26) that is
        # +25.0 ns of setup and +31.7 ns of hold.
        #
        # Do not quote 50.0 ns as the usable budget when selecting an MCU. It
        # is the position of the latching edge, so a slave that only becomes
        # valid *at* it has zero setup and fails. Swept in simulation: 0, 1 and
        # 2 usb cycles (0 / 16.7 / 33.3 ns) decode; 3 cycles (50.0 ns) does
        # not. The quotable number is the last passing one, **33.3 ns**, and
        # the gap up to 50.0 ns is setup time, not headroom.
        # ``test_slave_presenting_at_the_latching_edge_does_not_decode`` pins
        # the boundary so a change to the sample phase fails CI.
        #
        # Capturing in the rising-edge cycle from the raw pad sampled it at
        # t = 33.33 ns: +8.3 ns setup, +48.3 ns hold. Positive, but the whole
        # +8.3 ns had to cover board delay, package delay and FPGA input
        # setup, spread across eight separate capture points each reaching the
        # pad through different routing. That is thin, not violating -- commit
        # 665e5de's message and this comment's first version both said "plain
        # setup violation" and both measured the sample instant one usb cycle
        # early (16.67/33.3 rather than 33.33/50.0). Independently re-derived
        # from the RTL and corrected here; the fix stands on tripling the
        # margin and collapsing eight pad readers onto one register, not on
        # the pad timing having been out of spec.
        #
        # The last falling edge restores idle-low SCK.
        with m.If(active):
            with m.If(setup_pending != 0):
                m.d.usb += setup_pending.eq(setup_pending - 1)
            with m.Elif(edge_divider == half_period - 1):
                m.d.usb += edge_divider.eq(0)
                with m.If(~self.sck):
                    m.d.usb += self.sck.eq(1)
                with m.Else():
                    m.d.usb += self.sck.eq(0)
                    # Placed before the bit_index == 255 split: the final bit
                    # must be captured on the same cycle `active` is cleared,
                    # and rx_memory_write.en still reads `active` as 1 there.
                    # The byte-boundary tests read the pre-increment bit_index
                    # exactly as they did in the rising branch -- bit_index is
                    # assigned in m.d.usb in this same cycle, so ordering
                    # within the block is irrelevant. Do not reorder.
                    m.d.usb += rx_byte_shift.eq(next_rx_byte)
                    with m.If(bit_index[:3] == 7):
                        m.d.usb += rx_byte_shift.eq(0)
                        with m.If(bit_index < 30 * 8):
                            m.d.usb += rx_crc.eq(_crc16_byte(rx_crc, next_rx_byte))
                        with m.Switch(rx_byte_index):
                            with m.Case(0):
                                m.d.usb += rx_sof.eq(next_rx_byte)
                            with m.Case(1):
                                m.d.usb += rx_type.eq(next_rx_byte)
                            with m.Case(2):
                                m.d.usb += rx_sequence.eq(next_rx_byte)
                            with m.Case(3):
                                m.d.usb += rx_length.eq(next_rx_byte)
                            with m.Case(30):
                                m.d.usb += rx_crc_low.eq(next_rx_byte)
                            with m.Case(31):
                                m.d.usb += rx_crc_high.eq(next_rx_byte)
                    with m.If(bit_index == 255):
                        m.d.usb += [
                            active.eq(0),
                            complete_pending.eq(self._cs_hold_cycles),
                        ]
                        with m.If(active_send_message):
                            m.d.usb += tx_queued.eq(0)
                    with m.Else():
                        m.d.usb += bit_index.eq(next_bit_index)
                        with m.If(bit_index[:3] == 7):
                            m.d.usb += [
                                tx_byte_shift.eq(
                                    Mux(
                                        active_send_message,
                                        tx_memory_read.data,
                                        next_idle_byte,
                                    )
                                ),
                                self.mosi.eq(
                                    Mux(
                                        active_send_message,
                                        tx_memory_read.data[7],
                                        next_idle_byte[7],
                                    )
                                ),
                            ]
                        with m.Else():
                            m.d.usb += [
                                tx_byte_shift.eq(Cat(Const(0, 1), tx_byte_shift[:7])),
                                self.mosi.eq(tx_byte_shift[6]),
                            ]
            with m.Else():
                m.d.usb += edge_divider.eq(edge_divider + 1)

        # The CH32 SPI slave needs th(NSS) >= 2*tHCLK measured from the last SCK
        # edge (datasheet Table 3-26, Figures 3-10/3-11). Hold CS_N low for
        # cs_hold_cycles usb cycles past the final falling edge; SCK is already
        # idle-low throughout, so this extends only the CS tail. The default 3
        # (50.00 ns) satisfies the pessimistic reading for any HCLK >= 40 MHz
        # and gives 2.5x margin at the intended 100 MHz.
        with m.If(complete_pending != 0):
            m.d.usb += complete_pending.eq(complete_pending - 1)
            with m.If(complete_pending == 1):
                m.d.usb += [self.cs_n.eq(1), validate_pending.eq(1)]

        with m.If(self.rx_valid & self.rx_ready):
            m.d.usb += self.rx_valid.eq(0)
        with m.If(validate_pending):
            m.d.usb += validate_pending.eq(0)
            with m.If(transfer_ready & (rx_sof != SOF)):
                with m.If(self.bad_sof_count != 0xFFFF_FFFF):
                    m.d.usb += self.bad_sof_count.eq(self.bad_sof_count + 1)
            with m.Elif(transfer_ready & ~known_type):
                with m.If(self.bad_type_count != 0xFFFF_FFFF):
                    m.d.usb += self.bad_type_count.eq(self.bad_type_count + 1)
            with m.Elif(transfer_ready & (rx_length != expected_length)):
                with m.If(self.bad_length_count != 0xFFFF_FFFF):
                    m.d.usb += self.bad_length_count.eq(self.bad_length_count + 1)
            with m.Elif(transfer_ready & (rx_received_crc != rx_crc)):
                with m.If(self.bad_crc_count != 0xFFFF_FFFF):
                    m.d.usb += self.bad_crc_count.eq(self.bad_crc_count + 1)
            with m.Elif(valid_return & (~self.rx_valid | self.rx_ready)):
                m.d.usb += [
                    self.rx_valid.eq(1),
                    self.rx_type.eq(rx_type),
                    self.rx_sequence.eq(rx_sequence),
                    rx_published_bank.eq(rx_capture_bank),
                ]
            with m.Elif(valid_return):
                with m.If(self.rx_queue_full_count != 0xFFFF_FFFF):
                    m.d.usb += self.rx_queue_full_count.eq(self.rx_queue_full_count + 1)

        return m
