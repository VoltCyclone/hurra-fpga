from amaranth import DomainRenamer, Elaboratable, Module, Mux, Signal
from amaranth.lib.memory import Memory
from luna.gateware.interface.utmi import UTMIInterface
from luna.gateware.usb.usb2 import USBSpeed
from luna.gateware.usb.usb2.packet import (
    InterpacketTimerInterface,
    USBDataPacketCRC,
    USBDataPacketGenerator,
    USBDataPacketReceiver,
    USBHandshakeDetector,
    USBHandshakeGenerator,
    USBInterpacketTimer,
)

from .timing import HostTiming
from .token import SOF_PID, USBTokenGenerator
from .types import TransactionStatus

OUT_PID = 0x1
IN_PID = 0x9
SETUP_PID = 0xD
DATA0_PID = 0x3
DATA1_PID = 0xB
_DEFAULT_TIMING = HostTiming.hardware()
_FULL_SPEED_BIT_RATE = 12_000_000
_MAX_RX_PACKET_BYTES = 1 + 64 + 2  # PID, payload, and CRC16.


def _response_deadline_cycles(timing: HostTiming) -> int:
    """Bound response start plus a worst-case full-speed packet on UTMI."""
    clocks_per_bit = max(1, (timing.clock_hz + _FULL_SPEED_BIT_RATE - 1) // _FULL_SPEED_BIT_RATE)
    unstuffed_bits = 8 + (_MAX_RX_PACKET_BYTES * 8)  # SYNC plus packet bytes.
    wire_bits = unstuffed_bits + ((unstuffed_bits + 5) // 6) + 3  # Bit stuffing and EOP.
    detector_grace_cycles = 2
    return timing.transaction_timeout_cycles + wire_bits * clocks_per_bit + detector_grace_cycles


class USBHostTransactionPort:
    """Signal-only command/result port for one host transaction engine."""

    def __init__(self) -> None:
        self.start = Signal()
        self.start_ready = Signal()
        self.sof = Signal()
        self.frame = Signal(11)
        self.token_pid = Signal(4)
        self.address = Signal(7)
        self.endpoint = Signal(4)
        self.data_toggle = Signal()
        self.tx_length = Signal(range(65))
        self.tx_payload = Signal(8)
        self.tx_index = Signal(6)
        self.connected = Signal()
        #: Negotiated link speed, from the enumerator. Selects the interpacket
        #: timing; the byte-level data path is speed-agnostic because UTMI is
        #: always 8 bits wide and the packet stages are ready/valid streams.
        self.high_speed = Signal()
        #: Raw transmitter access for the enumerator's high-speed chirp, which
        #: is line signalling rather than a packet and so has no place in this
        #: engine's FSM. It passes through here only because this engine owns
        #: ``utmi.tx`` and Amaranth permits one driver per signal. Honoured only
        #: while the engine is idle, so a chirp can never corrupt a transaction;
        #: the enumerator only chirps during bus reset, when traffic is off.
        self.chirp_valid = Signal()
        self.chirp_data = Signal(8)

        self.busy = Signal()
        self.done = Signal()
        self.status = Signal(3)
        self.rx_length = Signal(range(65))
        self.rx_read_index = Signal(6)
        self.rx_read_data = Signal(8)
        self.rx_data_toggle = Signal()
        self.duplicate = Signal()


class USBHostTransactionEngine(USBHostTransactionPort, Elaboratable):
    """Execute one bounded full-speed USB host transaction at a time."""

    def __init__(self, utmi=None, timing: HostTiming = _DEFAULT_TIMING) -> None:
        super().__init__()
        self.utmi = utmi if utmi is not None else UTMIInterface()
        self.timing = timing

        self._token_generator = USBTokenGenerator()
        self._tx_data_crc = USBDataPacketCRC()
        self._rx_data_crc = USBDataPacketCRC()
        self._data_generator = USBDataPacketGenerator(standalone=False)
        # No ``speed`` kwarg: USBDataPacketReceiver only reads it inside
        # ``if self.standalone:``, to build its own timer. We pass standalone
        # False and supply the timer ourselves, so passing it was misleading --
        # it suggested the receiver was pinned to Full Speed when in fact the
        # receive path is speed-agnostic.
        self._data_receiver = USBDataPacketReceiver(
            utmi=self.utmi,
            standalone=False,
        )
        self._handshake_generator = USBHandshakeGenerator()
        self._handshake_detector = USBHandshakeDetector(utmi=self.utmi)
        # fs_only=False is load-bearing, not tidiness. It is a *construction*
        # parameter: with it set, LUNA omits the High Speed branch from the
        # elaborated timer entirely, so driving speed=HIGH would leave every
        # strobe permanently deasserted and the engine would wait forever
        # rather than fail visibly.
        self._interpacket_timer = USBInterpacketTimer(domain_clock=60e6, fs_only=False)
        self._engine_timer = InterpacketTimerInterface()
        self._tx_data_crc.add_interface(self._data_generator.crc)
        self._rx_data_crc.add_interface(self._data_receiver.data_crc)
        self._interpacket_timer.add_interface(self._engine_timer)
        self._interpacket_timer.add_interface(self._data_receiver.timer)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.token_generator = DomainRenamer({"sync": "usb"})(self._token_generator)
        m.submodules.tx_data_crc = self._tx_data_crc
        m.submodules.rx_data_crc = self._rx_data_crc
        m.submodules.data_generator = self._data_generator
        m.submodules.data_receiver = self._data_receiver
        m.submodules.handshake_generator = self._handshake_generator
        m.submodules.handshake_detector = self._handshake_detector
        m.submodules.interpacket_timer = self._interpacket_timer
        m.submodules.rx_memory = rx_memory = Memory(shape=8, depth=64, init=[])
        rx_read_port = rx_memory.read_port(domain="comb")
        rx_write_port = rx_memory.write_port(domain="usb")

        token_pid = Signal(4)
        token_address = Signal(7)
        token_endpoint = Signal(4)
        token_frame = Signal(11)
        sof_operation = Signal()
        tx_length = Signal(range(65))
        tx_toggle = Signal()
        active = Signal()
        data_started = Signal()
        payload_complete = Signal()
        ack_started = Signal()
        response_started = Signal()
        response_counter = Signal(range(self.timing.transaction_timeout_cycles + 1))
        response_deadline_cycles = _response_deadline_cycles(self.timing)
        response_deadline_counter = Signal(range(response_deadline_cycles + 1))
        packet_count = Signal(range(65))
        overflow_seen = Signal()
        packet_duplicate = Signal()
        timer_start = Signal()
        # Sized on whichever bound is larger, not on the Full Speed one. In
        # hardware Full Speed is larger (200 against 12), but sizing on it would
        # bake that in: a counter too narrow for the selected limit never
        # reaches it, and the state waits forever instead of failing visibly.
        # The comparisons below are already ``>=``, so a speed change mid-count
        # settles rather than stalling.
        _gap_width = range(
            max(self.timing.interpacket_delay_cycles, self.timing.hs_interpacket_delay_cycles) + 1
        )
        data_gap_counter = Signal(_gap_width)
        ack_drain_counter = Signal(_gap_width)
        sof_drain_counter = Signal(_gap_width)
        interpacket_limit = Mux(
            self.high_speed,
            self.timing.hs_interpacket_delay_cycles,
            self.timing.interpacket_delay_cycles,
        )

        expected_pid = Mux(tx_toggle, DATA1_PID, DATA0_PID)
        received_pid_is_data = (self._data_receiver.packet_id == DATA0_PID) | (
            self._data_receiver.packet_id == DATA1_PID
        )

        m.d.comb += [
            self.busy.eq(active),
            self.start_ready.eq(~active),
            self.utmi.tx_valid.eq(self.chirp_valid & ~active),
            self.utmi.tx_data.eq(self.chirp_data),
            self._token_generator.start.eq(0),
            self._token_generator.abort.eq(0),
            self._token_generator.pid.eq(token_pid),
            self._token_generator.address.eq(token_address),
            self._token_generator.endpoint.eq(token_endpoint),
            self._token_generator.frame.eq(token_frame),
            self._token_generator.tx_ready.eq(0),
            self._data_generator.data_pid.eq(tx_toggle),
            self._data_generator.stream.payload.eq(self.tx_payload),
            self._data_generator.stream.valid.eq(0),
            self._data_generator.stream.first.eq(0),
            self._data_generator.stream.last.eq(0),
            self._data_generator.tx.ready.eq(0),
            self._handshake_generator.issue_ack.eq(0),
            self._handshake_generator.issue_nak.eq(0),
            self._handshake_generator.issue_stall.eq(0),
            self._handshake_generator.tx.ready.eq(0),
            self._interpacket_timer.speed.eq(Mux(self.high_speed, USBSpeed.HIGH, USBSpeed.FULL)),
            self._engine_timer.start.eq(timer_start),
            timer_start.eq(0),
            self._tx_data_crc.rx_valid.eq(0),
            self._tx_data_crc.tx_data.eq(self._data_generator.stream.payload),
            self._tx_data_crc.tx_valid.eq(
                self._data_generator.stream.valid & self._data_generator.stream.ready
            ),
            self._rx_data_crc.rx_data.eq(self.utmi.rx_data),
            self._rx_data_crc.rx_valid.eq(self.utmi.rx_valid),
            self._rx_data_crc.tx_valid.eq(0),
            rx_read_port.addr.eq(self.rx_read_index),
            self.rx_read_data.eq(rx_read_port.data),
            rx_write_port.en.eq(0),
            rx_write_port.addr.eq(packet_count),
            rx_write_port.data.eq(self._data_receiver.stream.payload),
        ]
        m.d.usb += [
            self.done.eq(0),
            self.duplicate.eq(0),
        ]

        def finish(status: TransactionStatus) -> None:
            m.d.usb += [
                active.eq(0),
                self.done.eq(1),
                self.status.eq(status.value),
            ]
            m.next = "IDLE"

        def abort_disconnected() -> None:
            m.d.comb += self._token_generator.abort.eq(1)
            m.d.usb += self.rx_length.eq(0)
            finish(TransactionStatus.DISCONNECTED)

        with m.FSM(domain="usb"):
            with m.State("IDLE"), m.If(self.start):
                with m.If(~self.connected):
                    m.d.usb += [
                        self.done.eq(1),
                        self.status.eq(TransactionStatus.DISCONNECTED.value),
                        self.rx_length.eq(0),
                    ]
                with m.Else():
                    m.d.usb += [
                        active.eq(1),
                        token_pid.eq(Mux(self.sof, SOF_PID, self.token_pid)),
                        token_address.eq(self.address),
                        token_endpoint.eq(self.endpoint),
                        token_frame.eq(self.frame),
                        sof_operation.eq(self.sof),
                        tx_length.eq(self.tx_length),
                        tx_toggle.eq(Mux(self.token_pid == SETUP_PID, 0, self.data_toggle)),
                        self.tx_index.eq(0),
                        self.rx_length.eq(0),
                        packet_count.eq(0),
                        overflow_seen.eq(0),
                        packet_duplicate.eq(0),
                        response_started.eq(0),
                        payload_complete.eq(0),
                    ]
                    m.next = "TOKEN_START"

            with m.State("TOKEN_START"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Else():
                    m.d.comb += self._token_generator.start.eq(1)
                    m.next = "TOKEN_SEND"

            with m.State("TOKEN_SEND"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Else():
                    m.d.comb += [
                        self.utmi.tx_valid.eq(self._token_generator.tx_valid),
                        self.utmi.tx_data.eq(self._token_generator.tx_data),
                        self._token_generator.tx_ready.eq(self.utmi.tx_ready),
                    ]
                    with m.If(self._token_generator.done):
                        m.d.usb += [
                            response_counter.eq(0),
                            response_deadline_counter.eq(0),
                            response_started.eq(0),
                        ]
                        with m.If(sof_operation):
                            m.d.usb += [
                                self.rx_length.eq(0),
                                sof_drain_counter.eq(0),
                            ]
                            m.next = "WAIT_SOF_DRAIN"
                        with m.Elif(token_pid == IN_PID):
                            m.next = "WAIT_RESPONSE"
                        with m.Else():
                            m.d.usb += data_gap_counter.eq(0)
                            m.next = "WAIT_DATA_GAP"

            # Hold DATA off until the token packet (its last byte + EOP) has fully
            # drained on the wire, plus a real interpacket gap. Using the RX->TX
            # turnaround timer here is far too short — it is measured from the last
            # byte being *accepted*, not from the token's EOP finishing — so the
            # PHY would merge the token and DATA into one unterminated packet.
            with m.State("WAIT_DATA_GAP"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Elif(data_gap_counter >= interpacket_limit - 1):
                    m.d.usb += data_started.eq(0)
                    m.next = "SEND_DATA"
                with m.Else():
                    m.d.usb += data_gap_counter.eq(data_gap_counter + 1)

            with m.State("SEND_DATA"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Else():
                    m.d.comb += [
                        self._data_generator.stream.valid.eq(
                            Mux(tx_length == 0, ~data_started, ~payload_complete)
                        ),
                        self._data_generator.stream.first.eq(
                            (tx_length != 0) & (self.tx_index == 0)
                        ),
                        self._data_generator.stream.last.eq(
                            (tx_length == 0) | (self.tx_index == tx_length - 1)
                        ),
                        self.utmi.tx_valid.eq(self._data_generator.tx.valid),
                        self.utmi.tx_data.eq(self._data_generator.tx.data),
                        self._data_generator.tx.ready.eq(self.utmi.tx_ready),
                    ]
                    with m.If(
                        self._data_generator.stream.valid
                        & self._data_generator.stream.ready
                        & (tx_length != 0)
                    ):
                        with m.If(self._data_generator.stream.last):
                            m.d.usb += payload_complete.eq(1)
                        with m.Else():
                            m.d.usb += self.tx_index.eq(self.tx_index + 1)
                    with m.If(self._data_generator.tx.valid):
                        m.d.usb += data_started.eq(1)
                    with m.If(data_started & ~self._data_generator.tx.valid):
                        m.d.usb += [
                            response_counter.eq(0),
                            response_deadline_counter.eq(0),
                            response_started.eq(0),
                        ]
                        m.d.comb += timer_start.eq(1)
                        m.next = "WAIT_RESPONSE"

            with m.State("WAIT_RESPONSE"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Elif(self._handshake_detector.detected.nak):
                    m.d.usb += self.rx_length.eq(0)
                    finish(TransactionStatus.NAK)
                with m.Elif(self._handshake_detector.detected.stall):
                    m.d.usb += self.rx_length.eq(0)
                    finish(TransactionStatus.STALL)
                with m.Elif(self._handshake_detector.detected.ack & (token_pid != IN_PID)):
                    m.d.usb += self.rx_length.eq(0)
                    finish(TransactionStatus.SUCCESS)
                with m.Elif((token_pid == IN_PID) & self._data_receiver.crc_mismatch):
                    m.d.usb += self.rx_length.eq(0)
                    finish(TransactionStatus.CRC_ERROR)
                with m.Elif((token_pid == IN_PID) & self._data_receiver.packet_complete):
                    with m.If(overflow_seen):
                        m.d.usb += self.rx_length.eq(0)
                        finish(TransactionStatus.OVERFLOW)
                    with m.Elif(received_pid_is_data):
                        m.d.usb += [
                            packet_duplicate.eq(self._data_receiver.packet_id != expected_pid),
                            self.rx_data_toggle.eq(self._data_receiver.packet_id == DATA1_PID),
                        ]
                        m.next = "WAIT_ACK_GAP"
                    with m.Else():
                        m.d.usb += self.rx_length.eq(0)
                        finish(TransactionStatus.CRC_ERROR)
                with m.Else():
                    with m.If(response_deadline_counter == response_deadline_cycles - 1):
                        m.d.usb += self.rx_length.eq(0)
                        finish(TransactionStatus.TIMEOUT)
                    with m.Else():
                        m.d.usb += response_deadline_counter.eq(response_deadline_counter + 1)

                        with m.If((token_pid == IN_PID) & self._data_receiver.stream.next):
                            with m.If(packet_count < 64):
                                m.d.usb += packet_count.eq(packet_count + 1)
                                with m.If(self._data_receiver.active_pid == expected_pid):
                                    m.d.comb += rx_write_port.en.eq(1)
                            with m.Else():
                                m.d.usb += overflow_seen.eq(1)

                        with m.If(self.utmi.rx_active):
                            m.d.usb += [
                                response_started.eq(1),
                                response_counter.eq(0),
                            ]
                        with m.Elif(response_started):
                            # Give the LUNA detectors one cycle after RXActive falls
                            # to publish their registered completion strobes.
                            m.d.usb += [
                                response_started.eq(0),
                                response_counter.eq(0),
                            ]
                        with m.Else():
                            with m.If(
                                response_counter == self.timing.transaction_timeout_cycles - 1
                            ):
                                m.d.usb += self.rx_length.eq(0)
                                finish(TransactionStatus.TIMEOUT)
                            with m.Else():
                                m.d.usb += response_counter.eq(response_counter + 1)

            with m.State("WAIT_ACK_GAP"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Elif(self._data_receiver.ready_for_response):
                    m.d.comb += self._handshake_generator.issue_ack.eq(1)
                    m.d.usb += ack_started.eq(0)
                    m.next = "SEND_ACK"

            with m.State("SEND_ACK"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Else():
                    m.d.comb += [
                        self.utmi.tx_valid.eq(self._handshake_generator.tx.valid),
                        self.utmi.tx_data.eq(self._handshake_generator.tx.data),
                        self._handshake_generator.tx.ready.eq(self.utmi.tx_ready),
                    ]
                    with m.If(self._handshake_generator.tx.valid):
                        m.d.usb += ack_started.eq(1)
                    with m.If(ack_started & ~self._handshake_generator.tx.valid):
                        m.d.usb += [
                            self.rx_length.eq(Mux(packet_duplicate, 0, packet_count)),
                            ack_drain_counter.eq(0),
                        ]
                        m.next = "WAIT_ACK_DRAIN"

            # The ULPI PHY accepts the ACK byte well before it has serialized
            # the handshake and EOP onto D+/D-. Keep ownership until the ACK has
            # drained, or the next transaction's token can be appended to the
            # same PHY FIFO entry and lose its packet boundary.
            with m.State("WAIT_ACK_DRAIN"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Elif(ack_drain_counter >= interpacket_limit - 1):
                    m.d.usb += self.duplicate.eq(packet_duplicate)
                    finish(TransactionStatus.SUCCESS)
                with m.Else():
                    m.d.usb += ack_drain_counter.eq(ack_drain_counter + 1)

            # A SOF has no response phase, but its token bytes are likewise
            # accepted by ULPI before the PHY has emitted the EOP. Retain engine
            # ownership until it drains so a following control token cannot be
            # appended to the still-active SOF packet.
            with m.State("WAIT_SOF_DRAIN"):
                with m.If(~self.connected):
                    abort_disconnected()
                with m.Elif(sof_drain_counter >= interpacket_limit - 1):
                    finish(TransactionStatus.SUCCESS)
                with m.Else():
                    m.d.usb += sof_drain_counter.eq(sof_drain_counter + 1)

        return m
