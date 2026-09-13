"""Standard USB control-transfer sequencing."""

from amaranth import Array, Elaboratable, Module, Mux, Signal

from .timing import HostTiming
from .transaction import USBHostTransactionEngine
from .types import TransactionStatus

OUT_PID = 0x1
IN_PID = 0x9
SETUP_PID = 0xD
_DEFAULT_TIMING = HostTiming.hardware()


class USBControlTransferEngine(Elaboratable):
    """Sequence SETUP, optional data, and status transactions on endpoint zero.

    Read data is exposed directly from the transaction engine's bounded packet
    buffer. A new USB transaction is not issued until the consumer accepts the
    complete packet, so arbitrary stream backpressure cannot overwrite it.
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

        self.start = Signal()
        self.connected = Signal()
        self.address = Signal(7)
        self.request_type = Signal(8)
        self.request = Signal(8)
        self.value = Signal(16)
        self.index = Signal(16)
        self.length = Signal(16)
        self.max_packet_size = Signal(7)
        self.out_payload = Signal(8)
        self.out_index = Signal(16)

        self.busy = Signal()
        self.done = Signal()
        self.status = Signal(3)
        self.transferred = Signal(16)
        self.data_valid = Signal()
        self.data_ready = Signal()
        self.data = Signal(8)
        self.data_first = Signal()
        self.data_last = Signal()
        self.set_address_valid = Signal()
        self.set_address = Signal(7)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        transaction = self.transaction
        if self.add_transaction_submodule:
            m.submodules.transaction = transaction
        transaction_start_ready = getattr(transaction, "start_ready", ~transaction.busy)

        active = Signal()
        address = Signal(7)
        request_type = Signal(8)
        request = Signal(8)
        value = Signal(16)
        index = Signal(16)
        length = Signal(16)
        max_packet_size = Signal(7)
        read_transfer = Signal()
        set_address_pending = Signal()

        data_toggle = Signal()
        offset = Signal(16)
        packet_length = Signal(range(65))
        stream_index = Signal(6)
        packet_is_final = Signal()
        nak_timeout_active = Signal()
        nak_timeout_enable = Signal()
        nak_timeout_counter = Signal(range(self.timing.control_nak_timeout_cycles + 1))
        nak_timeout = Signal()

        remaining = Signal(16)
        write_packet_length = Signal(range(65))
        setup_payload = Array(
            [
                request_type,
                request,
                value[:8],
                value[8:16],
                index[:8],
                index[8:16],
                length[:8],
                length[8:16],
            ]
        )

        m.d.comb += [
            self.busy.eq(active),
            self.out_index.eq(offset + transaction.tx_index),
            self.data_valid.eq(0),
            self.data.eq(transaction.rx_read_data),
            self.data_first.eq(0),
            self.data_last.eq(0),
            transaction.start.eq(0),
            transaction.token_pid.eq(0),
            transaction.address.eq(address),
            transaction.endpoint.eq(0),
            transaction.data_toggle.eq(data_toggle),
            transaction.tx_length.eq(0),
            transaction.tx_payload.eq(0),
            transaction.connected.eq(self.connected),
            transaction.rx_read_index.eq(stream_index),
            nak_timeout_enable.eq(0),
            nak_timeout.eq(
                nak_timeout_active
                & nak_timeout_enable
                & (nak_timeout_counter >= self.timing.control_nak_timeout_cycles - 1)
            ),
            remaining.eq(length - offset),
            write_packet_length.eq(
                Mux(remaining > max_packet_size, max_packet_size, remaining[:7])
            ),
        ]
        m.d.usb += [
            self.done.eq(0),
            self.set_address_valid.eq(0),
        ]
        with m.If(nak_timeout_active & nak_timeout_enable & ~nak_timeout):
            m.d.usb += nak_timeout_counter.eq(nak_timeout_counter + 1)

        def finish(status: TransactionStatus) -> None:
            m.d.usb += [
                active.eq(0),
                self.done.eq(1),
                self.status.eq(status.value),
            ]
            m.next = "IDLE"

        def finish_transaction_status() -> None:
            m.d.usb += [
                active.eq(0),
                self.done.eq(1),
                self.status.eq(transaction.status),
            ]
            m.next = "IDLE"

        def abort_if_disconnected() -> None:
            finish(TransactionStatus.DISCONNECTED)

        with m.FSM(domain="usb"):
            with m.State("IDLE"), m.If(self.start):
                with m.If(~self.connected):
                    m.d.usb += [
                        self.done.eq(1),
                        self.status.eq(TransactionStatus.DISCONNECTED.value),
                        self.transferred.eq(0),
                    ]
                with m.Else():
                    m.d.usb += [
                        active.eq(1),
                        address.eq(self.address),
                        request_type.eq(self.request_type),
                        request.eq(self.request),
                        value.eq(self.value),
                        index.eq(self.index),
                        length.eq(self.length),
                        max_packet_size.eq(self.max_packet_size),
                        read_transfer.eq(self.request_type[7]),
                        set_address_pending.eq(
                            (self.request_type == 0) & (self.request == 5) & (self.value < 128)
                        ),
                        self.set_address.eq(self.value[:7]),
                        offset.eq(0),
                        self.transferred.eq(0),
                        data_toggle.eq(1),
                        nak_timeout_active.eq(0),
                        nak_timeout_counter.eq(0),
                    ]
                    m.next = "ISSUE_SETUP"

            with m.State("ISSUE_SETUP"):
                m.d.comb += nak_timeout_enable.eq(1)
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Else():
                    m.d.comb += [
                        transaction.start.eq(1),
                        transaction.token_pid.eq(SETUP_PID),
                        transaction.data_toggle.eq(0),
                        transaction.tx_length.eq(8),
                        transaction.tx_payload.eq(setup_payload[transaction.tx_index]),
                    ]
                    with m.If(transaction_start_ready):
                        m.next = "WAIT_SETUP"

            with m.State("WAIT_SETUP"):
                m.d.comb += nak_timeout_enable.eq(1)
                m.d.comb += transaction.tx_payload.eq(setup_payload[transaction.tx_index])
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Elif(transaction.done):
                    with m.If(transaction.status == TransactionStatus.NAK.value):
                        m.d.usb += nak_timeout_active.eq(1)
                        m.next = "ISSUE_SETUP"
                    with m.Elif(transaction.status != TransactionStatus.SUCCESS.value):
                        finish_transaction_status()
                    with m.Elif(length == 0):
                        m.d.usb += [
                            nak_timeout_active.eq(0),
                            nak_timeout_counter.eq(0),
                        ]
                        m.next = "ISSUE_STATUS"
                    with m.Elif(read_transfer):
                        m.d.usb += [
                            nak_timeout_active.eq(0),
                            nak_timeout_counter.eq(0),
                        ]
                        m.next = "ISSUE_READ"
                    with m.Else():
                        m.d.usb += [
                            nak_timeout_active.eq(0),
                            nak_timeout_counter.eq(0),
                        ]
                        m.next = "ISSUE_WRITE"

            with m.State("ISSUE_READ"):
                m.d.comb += nak_timeout_enable.eq(1)
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Else():
                    m.d.comb += [
                        transaction.start.eq(1),
                        transaction.token_pid.eq(IN_PID),
                        transaction.data_toggle.eq(data_toggle),
                    ]
                    with m.If(transaction_start_ready):
                        m.next = "WAIT_READ"

            with m.State("WAIT_READ"):
                m.d.comb += nak_timeout_enable.eq(1)
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Elif(transaction.done):
                    with m.If(transaction.status == TransactionStatus.NAK.value):
                        m.d.usb += nak_timeout_active.eq(1)
                        m.next = "ISSUE_READ"
                    with m.Elif(transaction.status != TransactionStatus.SUCCESS.value):
                        finish_transaction_status()
                    with m.Elif(transaction.duplicate):
                        m.next = "ISSUE_READ"
                    with m.Elif(
                        (transaction.rx_length > remaining)
                        | (transaction.rx_length > max_packet_size)
                    ):
                        finish(TransactionStatus.OVERFLOW)
                    with m.Else():
                        m.d.usb += [
                            data_toggle.eq(~data_toggle),
                            nak_timeout_active.eq(0),
                            nak_timeout_counter.eq(0),
                        ]
                        with m.If(transaction.rx_length == 0):
                            m.next = "ISSUE_STATUS"
                        with m.Else():
                            m.d.usb += [
                                packet_length.eq(transaction.rx_length),
                                stream_index.eq(0),
                                packet_is_final.eq(
                                    (transaction.rx_length < max_packet_size)
                                    | (transaction.rx_length == remaining)
                                ),
                            ]
                            m.next = "STREAM_READ"

            with m.State("STREAM_READ"):
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Else():
                    m.d.comb += [
                        self.data_valid.eq(1),
                        self.data_first.eq((offset == 0) & (stream_index == 0)),
                        self.data_last.eq(packet_is_final & (stream_index == packet_length - 1)),
                    ]
                    with m.If(self.data_ready):
                        with m.If(stream_index == packet_length - 1):
                            m.d.usb += [
                                offset.eq(offset + packet_length),
                                self.transferred.eq(offset + packet_length),
                            ]
                            with m.If(packet_is_final):
                                m.next = "ISSUE_STATUS"
                            with m.Else():
                                m.next = "ISSUE_READ"
                        with m.Else():
                            m.d.usb += stream_index.eq(stream_index + 1)

            with m.State("ISSUE_WRITE"):
                m.d.comb += nak_timeout_enable.eq(1)
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Else():
                    m.d.comb += [
                        transaction.start.eq(1),
                        transaction.token_pid.eq(OUT_PID),
                        transaction.data_toggle.eq(data_toggle),
                        transaction.tx_length.eq(write_packet_length),
                        transaction.tx_payload.eq(self.out_payload),
                    ]
                    with m.If(transaction_start_ready):
                        m.next = "WAIT_WRITE"

            with m.State("WAIT_WRITE"):
                m.d.comb += nak_timeout_enable.eq(1)
                m.d.comb += transaction.tx_payload.eq(self.out_payload)
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Elif(transaction.done):
                    with m.If(transaction.status == TransactionStatus.NAK.value):
                        m.d.usb += nak_timeout_active.eq(1)
                        m.next = "ISSUE_WRITE"
                    with m.Elif(transaction.status != TransactionStatus.SUCCESS.value):
                        finish_transaction_status()
                    with m.Else():
                        m.d.usb += [
                            data_toggle.eq(~data_toggle),
                            offset.eq(offset + write_packet_length),
                            self.transferred.eq(offset + write_packet_length),
                            nak_timeout_active.eq(0),
                            nak_timeout_counter.eq(0),
                        ]
                        with m.If(write_packet_length == remaining):
                            m.next = "ISSUE_STATUS"
                        with m.Else():
                            m.next = "ISSUE_WRITE"

            with m.State("ISSUE_STATUS"):
                m.d.comb += nak_timeout_enable.eq(1)
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Else():
                    m.d.comb += [
                        transaction.start.eq(1),
                        transaction.token_pid.eq(
                            Mux((length != 0) & read_transfer, OUT_PID, IN_PID)
                        ),
                        transaction.data_toggle.eq(1),
                        transaction.tx_length.eq(0),
                    ]
                    with m.If(transaction_start_ready):
                        m.next = "WAIT_STATUS"

            with m.State("WAIT_STATUS"):
                m.d.comb += nak_timeout_enable.eq(1)
                with m.If(~self.connected):
                    abort_if_disconnected()
                with m.Elif(nak_timeout):
                    finish(TransactionStatus.TIMEOUT)
                with m.Elif(transaction.done):
                    with m.If(transaction.status == TransactionStatus.NAK.value):
                        m.d.usb += nak_timeout_active.eq(1)
                        m.next = "ISSUE_STATUS"
                    with m.Elif(transaction.status != TransactionStatus.SUCCESS.value):
                        finish_transaction_status()
                    with m.Elif(~((length != 0) & read_transfer) & transaction.duplicate):
                        m.next = "ISSUE_STATUS"
                    with m.Elif(~((length != 0) & read_transfer) & (transaction.rx_length != 0)):
                        finish(TransactionStatus.OVERFLOW)
                    with m.Else():
                        m.d.usb += [
                            active.eq(0),
                            self.done.eq(1),
                            self.status.eq(TransactionStatus.SUCCESS.value),
                        ]
                        with m.If(set_address_pending):
                            m.d.usb += self.set_address_valid.eq(1)
                        m.next = "IDLE"

        return m
