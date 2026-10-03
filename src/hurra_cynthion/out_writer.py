"""Relay one HID interrupt-OUT endpoint's packets to the real device."""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from amaranth import Elaboratable, Module, Signal
from amaranth.lib.memory import Memory

from .intervals import IntervalPacer
from .timing import HostTiming
from .transaction import OUT_PID, USBHostTransactionEngine
from .types import TransactionStatus

_DEFAULT_TIMING = HostTiming.hardware()


class InterruptOutWriter(Elaboratable):
    """Buffer one OUT packet from the clone and write it to the real device.

    The mirror of ``InterruptInPoller``: one packet at a time, paced by the
    endpoint's bInterval, on the shared transaction engine. While a packet is
    held ``out_ready`` stays low, so the clone's OUT FIFO fills and the PC is
    NAKed -- legal flow control for interrupt OUT.

    There is deliberately no ``failed`` output. A STALL on a rumble or LED
    endpoint must never be able to stop the input path, so a halted real
    endpoint simply stays halted: every later packet is dropped and counted
    on ``pulse_stall``/``pulse_dropped``. Clearing the halt through the control
    relay is out of scope.

    ``flush`` pulses when the clone resets its OUT FIFO (a PC bus reset, or
    SET_CONFIGURATION). Bytes already taken out of that FIFO belong to the
    PC's old session, so a partly filled packet is discarded, and a whole one
    still waiting for its interval is dropped and counted. A packet already on
    the wire finishes. The toggle is kept: it is the real device's sequence,
    which the PC's reset of the clone never touches.

    A disable releases ``active`` at once, mid-OUT included, exactly as the IN
    poller's does. That is safe because the enumerator leaves READY only by
    detaching, which drops ``connected`` in the same cycle, and the engine then
    aborts the transaction itself (DISCONNECTED) instead of sending the rest of
    a payload whose source the arbiter has just handed back to control.

    A zero-length OUT from the PC never reaches the device: the clone ACKs it,
    but LUNA's stream carries no packet for it. No HID output report is zero
    bytes long.

    ``max_packet_size`` must be 1-64, the bound the enumerator captures under.
    """

    def __init__(
        self,
        transaction=None,
        timing: HostTiming = _DEFAULT_TIMING,
        add_transaction_submodule: bool = True,
    ) -> None:
        self.transaction = (
            transaction if transaction is not None else USBHostTransactionEngine(timing=timing)
        )
        self.timing = timing
        self.add_transaction_submodule = add_transaction_submodule

        self.enable = Signal()
        self.connected = Signal()
        self.flush = Signal()
        self.sof_tick = Signal()
        self.address = Signal(7)
        self.endpoint = Signal(4)
        self.max_packet_size = Signal(7)
        #: bInterval exactly as the device declared it; see intervals.py.
        self.interval = Signal(8)
        self.high_speed = Signal()

        self.out_valid = Signal()
        self.out_data = Signal(8)
        self.out_last = Signal()
        self.out_ready = Signal()

        self.active = Signal()
        self.last_status = Signal(3)
        #: Data toggle of the next write to the real device.
        self.toggle = Signal()

        self.pulse_written = Signal()
        self.pulse_nak = Signal()
        self.pulse_stall = Signal()
        #: The packet exhausted its transport-error budget (any of TIMEOUT,
        #: CRC_ERROR or OVERFLOW).
        self.pulse_timeout = Signal()
        self.pulse_dropped = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        transaction = self.transaction
        if self.add_transaction_submodule:
            m.submodules.transaction = transaction

        m.submodules.packet_memory = packet_memory = Memory(shape=8, depth=64, init=[])
        packet_read = packet_memory.read_port(domain="comb")
        packet_write = packet_memory.write_port(domain="usb")

        pacer = IntervalPacer(
            interval=self.interval,
            high_speed=self.high_speed,
            sof_tick=self.sof_tick,
            due_name="write_due",
        )
        live = Signal()
        fill_count = Signal(6)
        packet_length = Signal(range(65))
        attempts = Signal(range(self.timing.max_transport_errors + 1))
        issue_write = Signal()

        pacer.decode(m)
        transaction_start_ready = getattr(transaction, "start_ready", ~transaction.busy)

        m.d.comb += [
            live.eq(self.enable & self.connected),
            # Accept while disabled so the clone's FIFO drains instead of
            # carrying stale bytes across a reconnect; FILL discards them.
            self.out_ready.eq(~live),
            packet_write.addr.eq(fill_count),
            packet_write.data.eq(self.out_data),
            # The engine steps tx_index as its data generator accepts bytes.
            packet_read.addr.eq(transaction.tx_index),
            transaction.start.eq(issue_write),
            transaction.token_pid.eq(OUT_PID),
            transaction.address.eq(self.address),
            transaction.endpoint.eq(self.endpoint),
            transaction.data_toggle.eq(self.toggle),
            transaction.tx_length.eq(packet_length),
            transaction.tx_payload.eq(packet_read.data),
            transaction.connected.eq(self.connected),
            # OUT receives nothing; the arbiter still muxes this per port.
            transaction.rx_read_index.eq(0),
        ]
        m.d.usb += [
            self.pulse_written.eq(0),
            self.pulse_nak.eq(0),
            self.pulse_stall.eq(0),
            self.pulse_timeout.eq(0),
            self.pulse_dropped.eq(0),
        ]

        with m.If(~live):
            pacer.clear(m)
            m.d.usb += [
                fill_count.eq(0),
                attempts.eq(0),
                self.active.eq(0),
                self.last_status.eq(TransactionStatus.SUCCESS.value),
                # This toggle is the host->real-device sequence, so it resets
                # only with the real device's own session. The PC's
                # ClearFeature(HALT) on the clone is answered locally and never
                # forwarded, leaving the real device's toggle untouched, so it
                # must not reset this.
                self.toggle.eq(0),
            ]
        with m.Else():
            pacer.advance(m)

        with m.FSM(domain="usb"):
            with m.State("FILL"):
                m.d.comb += self.out_ready.eq(1)
                with m.If(self.flush):
                    m.d.usb += fill_count.eq(0)
                with m.Elif(live & self.out_valid):
                    m.d.comb += packet_write.en.eq(1)
                    # LUNA's OUT endpoint does not mark ``last`` on a full
                    # max-packet-size packet, so the count frames it too.
                    with m.If(self.out_last | (fill_count == self.max_packet_size - 1)):
                        m.d.usb += [
                            packet_length.eq(fill_count + 1),
                            fill_count.eq(0),
                            attempts.eq(0),
                        ]
                        m.next = "READY"
                    with m.Else():
                        m.d.usb += fill_count.eq(fill_count + 1)

            with m.State("READY"):
                with m.If(~live):
                    m.next = "FILL"
                with m.Elif(self.flush):
                    m.d.usb += self.pulse_dropped.eq(1)
                    m.next = "FILL"
                with m.Elif(pacer.due & ~transaction.busy & transaction_start_ready):
                    m.d.comb += issue_write.eq(1)
                    m.d.usb += self.active.eq(1)
                    pacer.consume(m)
                    m.next = "SEND"

            with m.State("SEND"):
                with m.If(~live):
                    m.next = "FILL"
                with m.Elif(transaction.done):
                    m.d.usb += [
                        self.active.eq(0),
                        self.last_status.eq(transaction.status),
                    ]
                    with m.Switch(transaction.status):
                        with m.Case(TransactionStatus.SUCCESS.value):
                            m.d.usb += [
                                self.toggle.eq(~self.toggle),
                                self.pulse_written.eq(1),
                            ]
                            m.next = "FILL"
                        with m.Case(TransactionStatus.NAK.value):
                            # Same packet and toggle, at the next due. A NAK
                            # proves the device is answering, so it restarts the
                            # transport-error budget exactly as the IN poller's
                            # does: only consecutive failures drop a packet.
                            m.d.usb += [
                                self.pulse_nak.eq(1),
                                attempts.eq(0),
                            ]
                            m.next = "READY"
                        with m.Case(TransactionStatus.STALL.value):
                            m.d.usb += [
                                self.pulse_stall.eq(1),
                                self.pulse_dropped.eq(1),
                            ]
                            m.next = "FILL"
                        with m.Case(TransactionStatus.DISCONNECTED.value):
                            m.d.usb += self.pulse_dropped.eq(1)
                            m.next = "FILL"
                        with m.Default():
                            with m.If(attempts == self.timing.max_transport_errors - 1):
                                m.d.usb += [
                                    self.pulse_timeout.eq(1),
                                    self.pulse_dropped.eq(1),
                                ]
                                m.next = "FILL"
                            with m.Else():
                                # Resend with the same toggle: if only the
                                # ACK was lost, the device discards the
                                # duplicate and ACKs it.
                                m.d.usb += attempts.eq(attempts + 1)
                                m.next = "READY"

        return m
