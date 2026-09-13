from amaranth import Elaboratable, Module, Mux, Signal

from .descriptors import DESCRIPTOR_ENTRY_COUNT
from .injection_wire import INJ_TYPE_DESCRIPTOR_FRAGMENT

__all__ = ["DescriptorExportEngine"]


class DescriptorExportEngine(Elaboratable):
    """Export committed HID report descriptors through a best-effort message stream."""

    _IDLE = 0
    _SLOT_WAIT = 1
    _SLOT_PIPELINE = 2
    _SLOT_CHECK = 3
    _EMIT = 4

    def __init__(self, store) -> None:
        self.store = store

        self.start = Signal()
        self.busy = Signal()
        self.done = Signal()

        self.message_valid = Signal()
        self.message_ready = Signal()
        self.message_type = Signal(8)
        self.message_payload_request = Signal()
        self.message_payload_address = Signal(5)
        self.message_payload_response = Signal()
        self.message_payload_data = Signal(8)
        self.message_payload_cancel = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        store = self.store

        state = Signal(range(5), init=self._IDLE)
        slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        fragment_offset = Signal(16)
        # The store bounds lengths to 13 bits, but the frozen wire field is
        # exactly 16 bits wide.
        descriptor_length = Signal(16)
        descriptor_interface = Signal(8)
        export_generation = Signal(16)
        fragment_pending = Signal()
        payload_address_q = Signal(5)
        _PAYLOAD_IDLE = 0
        _PAYLOAD_PIPELINE = 1
        _PAYLOAD_RESPONSE = 2
        payload_state = Signal(range(3), init=_PAYLOAD_IDLE)
        payload_data_byte = (payload_address_q >= 8) & (payload_address_q < 26)

        # These outputs are deliberately not gated with ``store.clear``.
        #
        # ``clear`` is a combinational decode of the enumerator FSM, and the
        # enumerator is placed at the opposite end of the die from the SPI link.
        # Gating here put
        #   enumerator.fsm_state -> clear -> message_valid -> TX arbitration
        #   -> spi_link.tx_memory.WRE
        # on a single combinational path spanning three placement regions, with
        # a 3.17 ns route for ``clear`` alone -- 19% of the 60 MHz period in one
        # wire, and the binding critical path of the whole design.
        #
        # The gate bought exactly one cycle of earlier deassertion over the
        # registered reset below, which clears ``fragment_pending`` and
        # ``payload_state`` on the very next edge. That cycle is not
        # load-bearing: ``spi_link`` abandons an in-progress build whenever
        # ``tx_valid`` drops (spi_link.py, "Elif(tx_building) / If(~tx_valid)"),
        # nothing is queued before byte 31, and the arbiter releases
        # ``tx_owner_locked`` on the same event. A frame started during the
        # clear cycle is therefore unwound and never sent, and the export is
        # re-run anyway once ``export_trigger`` re-arms on the generation bump.
        m.d.comb += [
            store.export_read_enable.eq(
                self.busy & fragment_pending & (payload_state != _PAYLOAD_IDLE) & payload_data_byte
            ),
            store.export_request.eq(state == self._SLOT_WAIT),
            store.export_slot.eq(slot),
            store.export_offset.eq(
                fragment_offset + Mux(payload_data_byte, payload_address_q - 8, 0)
            ),
            self.message_valid.eq(fragment_pending),
            self.message_type.eq(INJ_TYPE_DESCRIPTOR_FRAGMENT),
            self.message_payload_response.eq(
                (payload_state == _PAYLOAD_RESPONSE) & fragment_pending
            ),
            self.message_payload_data.eq(0),
        ]
        with m.Switch(payload_address_q):
            with m.Case(0):
                m.d.comb += self.message_payload_data.eq(export_generation[:8])
            with m.Case(1):
                m.d.comb += self.message_payload_data.eq(export_generation[8:16])
            with m.Case(2):
                m.d.comb += self.message_payload_data.eq(descriptor_interface)
            with m.Case(3):
                m.d.comb += self.message_payload_data.eq(0)
            with m.Case(4):
                m.d.comb += self.message_payload_data.eq(fragment_offset[:8])
            with m.Case(5):
                m.d.comb += self.message_payload_data.eq(fragment_offset[8:16])
            with m.Case(6):
                m.d.comb += self.message_payload_data.eq(descriptor_length[:8])
            with m.Case(7):
                m.d.comb += self.message_payload_data.eq(descriptor_length[8:16])
            with m.Case(*range(8, 26)):
                m.d.comb += self.message_payload_data.eq(store.export_data)

        m.d.usb += self.done.eq(0)

        with m.If(store.clear):
            m.d.usb += [
                state.eq(self._IDLE),
                self.busy.eq(0),
                fragment_pending.eq(0),
                slot.eq(0),
                fragment_offset.eq(0),
                payload_state.eq(_PAYLOAD_IDLE),
            ]
        with m.Else():
            with m.If(
                self.message_payload_cancel
                | (fragment_pending & self.message_ready)
                | ~fragment_pending
            ):
                m.d.usb += payload_state.eq(_PAYLOAD_IDLE)
            with m.Else(), m.Switch(payload_state):
                with m.Case(_PAYLOAD_IDLE), m.If(self.message_payload_request):
                    m.d.usb += [
                        payload_address_q.eq(self.message_payload_address),
                        payload_state.eq(_PAYLOAD_PIPELINE),
                    ]
                with m.Case(_PAYLOAD_PIPELINE):
                    m.d.usb += payload_state.eq(_PAYLOAD_RESPONSE)
                with m.Case(_PAYLOAD_RESPONSE):
                    m.d.usb += payload_state.eq(_PAYLOAD_IDLE)

            with m.Switch(state):
                with m.Case(self._IDLE), m.If(self.start):
                    m.d.usb += [
                        self.busy.eq(1),
                        slot.eq(0),
                        fragment_offset.eq(0),
                        export_generation.eq(store.descriptor_generation),
                        state.eq(self._SLOT_WAIT),
                    ]

                with m.Case(self._SLOT_WAIT), m.If(store.export_ready):
                    m.d.usb += state.eq(self._SLOT_PIPELINE)

                with m.Case(self._SLOT_PIPELINE), m.If(store.export_response):
                    m.d.usb += state.eq(self._SLOT_CHECK)

                with m.Case(self._SLOT_CHECK):
                    with m.If(
                        store.export_valid
                        & (store.export_type == 0x22)
                        & (store.export_length != 0)
                    ):
                        m.d.usb += [
                            descriptor_length.eq(store.export_length),
                            descriptor_interface.eq(store.export_w_index[:8]),
                            fragment_offset.eq(0),
                            fragment_pending.eq(1),
                            state.eq(self._EMIT),
                        ]
                    with m.Elif(slot == DESCRIPTOR_ENTRY_COUNT - 1):
                        m.d.usb += [
                            self.busy.eq(0),
                            self.done.eq(1),
                            state.eq(self._IDLE),
                        ]
                    with m.Else():
                        m.d.usb += [
                            slot.eq(slot + 1),
                            fragment_offset.eq(0),
                            state.eq(self._SLOT_WAIT),
                        ]

                with m.Case(self._EMIT), m.If(fragment_pending & self.message_ready):
                    with m.If(fragment_offset + 18 >= descriptor_length):
                        m.d.usb += fragment_pending.eq(0)
                        with m.If(slot == DESCRIPTOR_ENTRY_COUNT - 1):
                            m.d.usb += [
                                self.busy.eq(0),
                                self.done.eq(1),
                                state.eq(self._IDLE),
                            ]
                        with m.Else():
                            m.d.usb += [
                                slot.eq(slot + 1),
                                fragment_offset.eq(0),
                                state.eq(self._SLOT_WAIT),
                            ]
                    with m.Else():
                        m.d.usb += fragment_offset.eq(fragment_offset + 18)

        return m
