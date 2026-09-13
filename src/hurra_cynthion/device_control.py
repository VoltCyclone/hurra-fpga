"""Device-side control handlers that clone the captured mouse to the PC."""

# ruff: noqa: SIM117

from amaranth import Elaboratable, Module, Mux, Signal
from luna.gateware.usb.request.standard import StandardRequestHandler
from luna.gateware.usb.stream import USBInStreamInterface
from luna.gateware.usb.usb2.request import USBRequestHandler
from usb_protocol.emitters import DeviceDescriptorCollection
from usb_protocol.types import USBRequestType, USBStandardRequests

from .descriptors import DESCRIPTOR_STORE_SIZE

__all__ = ["ClonedDescriptorStreamer", "ClonedStandardRequestHandler", "HIDClassRequestHandler"]


class ClonedDescriptorStreamer(Elaboratable):
    def __init__(self, store, max_packet_size: int = 64):
        self._store = store
        self._max_packet_size = max_packet_size
        self.value = Signal(16)
        self.w_index = Signal(16)
        self.length = Signal(16)
        self.start_position = Signal(range(DESCRIPTOR_STORE_SIZE))
        self.prepare = Signal()
        self.cancel = Signal()
        self.start = Signal()
        self.tx = USBInStreamInterface()
        self.stall = Signal()

    def elaborate(self, platform):
        del platform
        m = Module()
        store = self._store

        prepared = Signal()
        prepared_found = Signal()
        prepare_queued = Signal()
        lookup_pending = Signal()
        start_pending = Signal()
        # A ``start`` coincident with ``serve_response`` loses the arming chain to
        # the response branch, which sets ``prepared`` but latches neither
        # ``request_length`` nor ``request_start``. Replaying it one cycle later is
        # safe because ``self.length`` and ``self.start_position`` are *held*
        # values - a setup-packet register and the parent FSM's per-ACK counter -
        # not pulses, and the design already tolerates arming an arbitrary number
        # of cycles after ``start`` (that is the existing ``start_pending`` path).
        start_deferred = Signal()
        m.d.comb += [
            store.serve_type.eq(self.value[8:16]),
            store.serve_index.eq(self.value[0:8]),
            store.serve_w_index.eq(self.w_index),
            # Every new descriptor SETUP supersedes any prior cached
            # selection or in-flight directory scan.
            store.serve_cancel.eq(self.cancel | self.prepare),
        ]

        request_length = Signal(16)
        request_start = Signal(range(DESCRIPTOR_STORE_SIZE))
        effective_length = Signal(16)
        m.d.comb += effective_length.eq(
            Mux(store.serve_length < request_length, store.serve_length, request_length)
        )
        words_remaining = Signal(16)
        m.d.comb += words_remaining.eq(
            Mux(request_start >= effective_length, 0, effective_length - request_start)
        )
        chunk_len = Signal(16)
        m.d.comb += chunk_len.eq(
            Mux(words_remaining < self._max_packet_size, words_remaining, self._max_packet_size)
        )
        next_chunk_end = Signal(16)
        m.d.comb += next_chunk_end.eq(request_start + chunk_len)

        start_effective_length = Signal(16)
        start_words_remaining = Signal(16)
        start_chunk_len = Signal(16)
        m.d.comb += [
            start_effective_length.eq(
                Mux(store.serve_length < self.length, store.serve_length, self.length)
            ),
            start_words_remaining.eq(
                Mux(
                    self.start_position >= start_effective_length,
                    0,
                    start_effective_length - self.start_position,
                )
            ),
            start_chunk_len.eq(
                Mux(
                    start_words_remaining < self._max_packet_size,
                    start_words_remaining,
                    self._max_packet_size,
                )
            ),
        ]

        pos = Signal(range(DESCRIPTOR_STORE_SIZE))
        chunk_end = Signal(16)
        active = Signal()
        zlp = Signal()
        stall_r = Signal()

        start_now = self.start | start_deferred

        need_prepare = self.prepare | (self.start & ~prepared & ~lookup_pending & ~prepare_queued)
        m.d.comb += [
            # Explicitly claim the shared payload port only while a transfer
            # needs its lookahead byte. Directory-match prefetch is handled
            # inside the store. ``start_deferred`` must be included: without it
            # the replay cycle drops the claim, ``serve_offset`` stops driving
            # ``start_position``, and the first payload byte is 0 while
            # ``tx.valid`` is already high.
            store.serve_read_enable.eq(active | self.start | start_pending | start_deferred),
            self.stall.eq(stall_r),
            store.serve_request.eq(need_prepare | prepare_queued),
        ]
        serve_fire = store.serve_request & store.serve_ready

        # Idle cycles prefetch the next packet's first byte. During an
        # accepted byte, drive the next address one cycle early so the stream
        # never drops tx.valid between payload bytes.
        m.d.comb += store.serve_offset.eq(
            Mux(
                active & self.tx.ready & (pos != (chunk_end - 1)),
                pos + 1,
                Mux(active, pos, self.start_position),
            )
        )

        m.d.comb += [
            # TODO(ep0-size): if a cloned mouse reports bMaxPacketSize0 != 64
            # (device-descriptor byte 7), special-case that offset within a
            # DEVICE descriptor to emit 64 instead of the captured value, so
            # it matches the fixed 64-byte EP0 this streamer's caller uses.
            self.tx.payload.eq(store.serve_data),
            self.tx.valid.eq(active | zlp),
            self.tx.first.eq(active & (pos == request_start)),
            self.tx.last.eq((active & (pos == (chunk_end - 1))) | zlp),
        ]

        with m.If(self.cancel):
            m.d.usb += [
                active.eq(0),
                zlp.eq(0),
                stall_r.eq(0),
                prepared.eq(0),
                prepared_found.eq(0),
                prepare_queued.eq(0),
                lookup_pending.eq(0),
                start_pending.eq(0),
                start_deferred.eq(0),
            ]
        with m.Else():
            with m.If(self.prepare):
                m.d.usb += [
                    active.eq(0),
                    zlp.eq(0),
                    stall_r.eq(0),
                    prepared.eq(0),
                    prepared_found.eq(0),
                    prepare_queued.eq(1),
                    start_pending.eq(0),
                    start_deferred.eq(0),
                ]
            with m.If(serve_fire):
                m.d.usb += [
                    prepare_queued.eq(0),
                    lookup_pending.eq(1),
                    prepared.eq(0),
                    prepared_found.eq(0),
                ]

            # A deferred start is consumed on the very next cycle and can never
            # stick; the arming chain below re-asserts it only on a fresh collision.
            m.d.usb += start_deferred.eq(0)

            with m.If(active & self.tx.ready):
                with m.If(pos == (chunk_end - 1)):
                    m.d.usb += active.eq(0)
                with m.Else():
                    m.d.usb += pos.eq(pos + 1)
            with m.Elif(zlp & self.tx.ready):
                m.d.usb += zlp.eq(0)
            # A replacement prepare accepted on this edge owns the lookup
            # state. Ignore any coincident response from the superseded scan.
            with m.Elif(store.serve_response & lookup_pending & ~serve_fire):
                m.d.usb += [
                    lookup_pending.eq(0),
                    prepared.eq(1),
                    prepared_found.eq(store.serve_found),
                ]
                with m.If(start_pending):
                    m.d.usb += start_pending.eq(0)
                    with m.If(~store.serve_found | (effective_length == 0)):
                        m.d.usb += stall_r.eq(1)
                    with m.Elif(words_remaining == 0):
                        m.d.usb += [zlp.eq(1), stall_r.eq(0)]
                    with m.Else():
                        m.d.usb += [
                            active.eq(1),
                            pos.eq(request_start),
                            chunk_end.eq(next_chunk_end),
                            stall_r.eq(0),
                        ]
                # This branch swallows a coincident ``start``: it sets ``prepared``
                # but arms nothing. Replay it next cycle, when ``prepared`` is
                # visible and the already-working prepared path takes it. Guarding
                # on ``~start_pending`` makes the two arming paths mutually
                # exclusive by construction, so no double-arm is possible.
                with m.If(self.start & ~start_pending):
                    m.d.usb += start_deferred.eq(1)
            with m.Elif(start_now & ~active & ~zlp):
                m.d.usb += [
                    request_length.eq(self.length),
                    request_start.eq(self.start_position),
                    stall_r.eq(0),
                ]
                with m.If(prepared):
                    with m.If(~prepared_found | (start_effective_length == 0)):
                        m.d.usb += stall_r.eq(1)
                    with m.Elif(start_words_remaining == 0):
                        m.d.usb += zlp.eq(1)
                    with m.Else():
                        m.d.usb += [
                            active.eq(1),
                            pos.eq(self.start_position),
                            chunk_end.eq(self.start_position + start_chunk_len),
                        ]
                with m.Else():
                    m.d.usb += start_pending.eq(1)

        return m


class ClonedStandardRequestHandler(StandardRequestHandler):
    """Standard-request handler serving descriptors cloned from the capture.

    A thin subclass of LUNA's ``StandardRequestHandler``
    (luna/gateware/usb/request/standard.py). The parent already implements the
    full GET_STATUS / CLEAR_FEATURE / SET_ADDRESS / SET_CONFIGURATION /
    GET_DESCRIPTOR / GET_CONFIGURATION / UNHANDLED state machine; the only
    piece we override is ``get_descriptor_handler_submodule()``, which the
    parent's own ``elaborate()`` calls to build the submodule it wires up as
    ``m.submodules.get_descriptor`` (driving ``.value``/``.length`` from the
    setup packet, pulsing ``.start`` on ``data_requested``, walking
    ``.start_position`` forward by ``max_packet_size`` per ACK, attaching
    ``.tx`` to the control endpoint's IN stream, and routing ``.stall`` to the
    handshake generator). Normally that returns a ROM-backed
    ``GetDescriptorHandlerBlock``/``Distributed`` built from a fixed
    ``DeviceDescriptorCollection``; here it returns a
    :class:`ClonedDescriptorStreamer` that reads descriptor bytes live out of
    the captured-device ``DescriptorStore`` instead.
    """

    def __init__(self, store, max_packet_size: int = 64):
        self._store = store
        # The parent's __init__ only stores this collection; it's read back
        # solely inside get_descriptor_handler_submodule(), which we override
        # below, so the empty collection is never actually consulted.
        super().__init__(DeviceDescriptorCollection(), max_packet_size=max_packet_size)

    def get_descriptor_handler_submodule(self):
        self._get_descriptor = ClonedDescriptorStreamer(
            self._store, max_packet_size=self._max_packet_size
        )
        return self._get_descriptor

    def elaborate(self, platform):
        # Builds the full standard-request FSM and, via
        # get_descriptor_handler_submodule() above, wires our streamer in as
        # m.submodules.get_descriptor with .value/.length/.start/
        # .start_position/.tx/.stall all connected.
        m = super().elaborate(platform)

        # The parent only knows about the generic .value/.length inputs
        # shared by every LUNA descriptor-handler submodule; wire the one
        # extra signal our streamer needs (wIndex -- e.g. a STRING
        # descriptor's language ID) that the parent has no notion of.
        setup = self.interface.setup
        get_descriptor_setup = (
            setup.received
            & (setup.type == USBRequestType.STANDARD)
            & (setup.request == USBStandardRequests.GET_DESCRIPTOR)
            & setup.is_in_request
        )
        m.d.comb += [
            self._get_descriptor.w_index.eq(setup.index),
            self._get_descriptor.prepare.eq(get_descriptor_setup),
            self._get_descriptor.cancel.eq(setup.received & ~get_descriptor_setup),
        ]

        return m


_HID_SET_IDLE = 0x0A
_HID_SET_PROTOCOL = 0x0B


class HIDClassRequestHandler(USBRequestHandler):
    """Answers the HID class control requests an OS issues while binding.

    Claims only ``setup.type == USBRequestType.CLASS`` requests, so it
    coexists alongside a STANDARD-request handler (and a catch-all) attached
    to the same control endpoint. ACKs the status phase of SET_IDLE (0x0A)
    and SET_PROTOCOL (0x0B) -- the two class requests most OSes require a
    HID device to acknowledge before it will bind -- and STALLs everything
    else (e.g. GET_REPORT/GET_PROTOCOL). Stalling is a safe default for v1:
    most OSes fall back to reading the interrupt IN endpoint instead of
    GET_REPORT.
    """

    def elaborate(self, platform):
        del platform
        m = Module()
        interface = self.interface
        setup = interface.setup
        handshake_generator = interface.handshakes_out

        with m.If(setup.type == USBRequestType.CLASS):
            m.d.comb += interface.claim.eq(1)
            with m.FSM(domain="usb"):
                with m.State("IDLE"):
                    with m.If(setup.received):
                        with m.Switch(setup.request):
                            with m.Case(_HID_SET_IDLE, _HID_SET_PROTOCOL):
                                m.next = "ACK_STATUS"
                            with m.Default():
                                m.next = "STALL"
                with m.State("ACK_STATUS"):
                    with m.If(interface.status_requested):
                        m.d.comb += handshake_generator.ack.eq(1)
                        m.next = "IDLE"
                with m.State("STALL"):
                    with m.If(interface.data_requested | interface.status_requested):
                        m.d.comb += handshake_generator.stall.eq(1)
                        m.next = "IDLE"
        return m
