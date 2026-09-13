"""Bounded interrupt-IN polling for an enumerated USB mouse."""

from amaranth import Const, Elaboratable, Module, Mux, Signal
from amaranth.lib.memory import Memory

from .timing import HostTiming
from .transaction import USBHostTransactionEngine
from .types import TransactionStatus

_DEFAULT_TIMING = HostTiming.hardware()
IN_PID = 0x9


class InterruptInPoller(Elaboratable):
    """Poll one interrupt-IN endpoint and expose complete buffered reports."""

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
        self.sof_tick = Signal()
        self.address = Signal(7)
        self.endpoint = Signal(4)
        self.max_packet_size = Signal(7)
        #: bInterval exactly as the device declared it, in *its* encoding.
        #: Decoded against ``high_speed`` below rather than at capture, because
        #: the AUX clone hands this same byte to the PC and needs the original.
        self.interval = Signal(8)
        #: Negotiated link speed, from the enumerator. Selects how ``interval``
        #: is read.
        self.high_speed = Signal()

        self.report_valid = Signal()
        self.report_ready = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()

        self.active = Signal()
        self.failed = Signal()
        self.last_status = Signal(3)
        self.transport_error_count = Signal(range(timing.max_transport_errors + 1))
        self.report_length = Signal(7)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        transaction = self.transaction
        if self.add_transaction_submodule:
            m.submodules.transaction = transaction

        m.submodules.report_memory = report_memory = Memory(shape=8, depth=64, init=[])
        report_read = report_memory.read_port(domain="comb")
        report_write = report_memory.write_port(domain="usb")

        interval_counter = Signal(16)
        poll_due = Signal()
        poll_active = Signal()
        expected_toggle = Signal()
        copy_active = Signal()
        copy_index = Signal(6)
        copy_length = Signal(7)
        buffer_full = Signal()
        stream_index = Signal(6)

        # bInterval means different things at the two speeds.
        #
        # Full Speed: a direct count of 1 ms frames, 1-255.
        # High Speed: an *exponent*. The period is 2**(bInterval-1) microframes,
        #   so bInterval=4 is 8 microframes = 1 ms, not 4 ms.
        #
        # Reading a High Speed value as a direct count polls a device that asked
        # for 1 ms at 500 us -- twice as fast as it asked for. The device still
        # works, so nothing looks broken; it is just out of spec.
        #
        # High Speed bInterval is only legal in 1-16. Clamp upward to 16, which
        # yields the slowest legal period rather than letting the shift overflow
        # and flood the bus on a malformed descriptor. Zero is malformed at both
        # speeds and falls back to polling every tick.
        hs_clamped = Mux(self.interval == 0, 1, Mux(self.interval > 16, 16, self.interval))
        # Width 1 << 15 fits the 16-bit counter: exponent 0-15 gives 1-32768.
        hs_interval = Const(1, 1) << (hs_clamped - 1)[:4]
        effective_interval = Signal(16)
        m.d.comb += effective_interval.eq(
            Mux(self.high_speed, hs_interval, Mux(self.interval == 0, 1, self.interval))
        )
        transaction_start_ready = getattr(transaction, "start_ready", ~transaction.busy)
        interval_elapsed = self.sof_tick & (interval_counter == effective_interval - 1)
        issue_poll = (
            self.enable
            & self.connected
            & ~self.failed
            & poll_due
            & ~poll_active
            & ~copy_active
            & ~buffer_full
            & ~transaction.busy
            & transaction_start_ready
        )

        m.d.comb += [
            self.active.eq(poll_active | copy_active),
            self.report_valid.eq(buffer_full),
            self.report_data.eq(report_read.data),
            self.report_first.eq(buffer_full & (stream_index == 0)),
            self.report_last.eq(buffer_full & (stream_index == self.report_length - 1)),
            report_read.addr.eq(stream_index),
            report_write.en.eq(copy_active),
            report_write.addr.eq(copy_index),
            report_write.data.eq(transaction.rx_read_data),
            transaction.start.eq(issue_poll),
            transaction.token_pid.eq(IN_PID),
            transaction.address.eq(self.address),
            transaction.endpoint.eq(self.endpoint),
            transaction.data_toggle.eq(expected_toggle),
            transaction.tx_length.eq(0),
            transaction.tx_payload.eq(0),
            transaction.connected.eq(self.connected),
            transaction.rx_read_index.eq(copy_index),
        ]

        def clear_buffer() -> None:
            m.d.usb += [
                copy_active.eq(0),
                copy_index.eq(0),
                copy_length.eq(0),
                buffer_full.eq(0),
                stream_index.eq(0),
                self.report_length.eq(0),
            ]

        def fail(status: TransactionStatus) -> None:
            m.d.usb += [
                poll_active.eq(0),
                poll_due.eq(0),
                self.failed.eq(1),
                self.last_status.eq(status.value),
            ]
            clear_buffer()

        def record_transport_error(status: TransactionStatus) -> None:
            m.d.usb += [
                poll_active.eq(0),
                self.last_status.eq(status.value),
            ]
            with m.If(self.transport_error_count == self.timing.max_transport_errors - 1):
                m.d.usb += [
                    self.transport_error_count.eq(self.timing.max_transport_errors),
                    self.failed.eq(1),
                    poll_due.eq(0),
                ]
            with m.Else():
                m.d.usb += self.transport_error_count.eq(self.transport_error_count + 1)

        with m.If(~self.enable):
            m.d.usb += [
                interval_counter.eq(0),
                poll_due.eq(0),
                poll_active.eq(0),
                expected_toggle.eq(0),
                self.failed.eq(0),
                self.last_status.eq(TransactionStatus.SUCCESS.value),
                self.transport_error_count.eq(0),
            ]
            clear_buffer()
        with m.Elif(~self.connected):
            fail(TransactionStatus.DISCONNECTED)
        with m.Elif(~self.failed):
            with m.If(self.sof_tick):
                with m.If(interval_elapsed):
                    m.d.usb += [
                        interval_counter.eq(0),
                        poll_due.eq(1),
                    ]
                with m.Else():
                    m.d.usb += interval_counter.eq(interval_counter + 1)

            with m.If(issue_poll):
                m.d.usb += [
                    poll_active.eq(1),
                    poll_due.eq(0),
                ]
                with m.If(interval_elapsed):
                    m.d.usb += poll_due.eq(1)

            with m.If(poll_active & transaction.done):
                m.d.usb += [
                    poll_active.eq(0),
                    self.last_status.eq(transaction.status),
                ]
                with m.Switch(transaction.status):
                    with m.Case(TransactionStatus.SUCCESS.value):
                        m.d.usb += self.transport_error_count.eq(0)
                        with m.If(transaction.duplicate):
                            pass
                        with m.Elif(transaction.rx_length > self.max_packet_size):
                            # This expected packet was already ACKed by the
                            # transaction engine, so the USB data toggle still
                            # advances even though the endpoint contract rejects it.
                            m.d.usb += expected_toggle.eq(~expected_toggle)
                            record_transport_error(TransactionStatus.OVERFLOW)
                        with m.Else():
                            m.d.usb += expected_toggle.eq(~expected_toggle)
                            with m.If(transaction.rx_length != 0):
                                m.d.usb += [
                                    copy_active.eq(1),
                                    copy_index.eq(0),
                                    copy_length.eq(transaction.rx_length),
                                    self.report_length.eq(transaction.rx_length),
                                ]
                    with m.Case(TransactionStatus.NAK.value):
                        m.d.usb += self.transport_error_count.eq(0)
                    with m.Case(TransactionStatus.STALL.value):
                        fail(TransactionStatus.STALL)
                    with m.Case(TransactionStatus.DISCONNECTED.value):
                        fail(TransactionStatus.DISCONNECTED)
                    with m.Case(TransactionStatus.TIMEOUT.value):
                        record_transport_error(TransactionStatus.TIMEOUT)
                    with m.Case(TransactionStatus.CRC_ERROR.value):
                        record_transport_error(TransactionStatus.CRC_ERROR)
                    with m.Default():
                        record_transport_error(TransactionStatus.OVERFLOW)

            with m.If(copy_active):
                with m.If(copy_index == copy_length - 1):
                    m.d.usb += [
                        copy_active.eq(0),
                        copy_index.eq(0),
                        buffer_full.eq(1),
                        stream_index.eq(0),
                    ]
                with m.Else():
                    m.d.usb += copy_index.eq(copy_index + 1)

            with m.If(buffer_full & self.report_ready):
                with m.If(stream_index == self.report_length - 1):
                    m.d.usb += [
                        buffer_full.eq(0),
                        stream_index.eq(0),
                        self.report_length.eq(0),
                    ]
                with m.Else():
                    m.d.usb += stream_index.eq(stream_index + 1)

        return m
