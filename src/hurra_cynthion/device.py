"""AUX-side USB device that clones the captured mouse and relays its reports."""

from amaranth import Cat, Elaboratable, Module, Mux, ResetInserter, Signal
from luna.gateware.usb.usb2.device import USBDevice
from luna.gateware.usb.usb2.endpoints.stream import USBStreamInEndpoint, USBStreamOutEndpoint

from .descriptors import (
    DESCRIPTOR_ENTRY_COUNT,
    MAX_ENDPOINTS,
    MAX_PACKET_SIZE,
    UNMATCHABLE_ENDPOINT_NUMBER,
)
from .device_control import ClonedStandardRequestHandler, HIDClassRequestHandler
from .relay import ReportRelay

__all__ = ["DescriptorStoreCopyEngine", "MouseCloneDevice"]


class DescriptorStoreCopyEngine(Elaboratable):
    """Copy committed descriptors into the clone's private store."""

    def __init__(self, *, source, destination):
        self._source = source
        self._destination = destination

        self.enable = Signal()
        self.done = Signal()
        self.failed = Signal()

    def elaborate(self, platform):
        del platform
        m = Module()

        source = self._source
        destination = self._destination
        slot = Signal(range(DESCRIPTOR_ENTRY_COUNT))
        offset = Signal(13)

        m.d.comb += [
            source.copy_read_enable.eq(self.enable & ~self.done & ~self.failed),
            source.copy_request.eq(0),
            source.copy_slot.eq(slot),
            source.copy_offset.eq(offset),
            destination.clear.eq(~self.enable),
            destination.capture_start.eq(0),
            destination.capture_type.eq(source.copy_type),
            destination.capture_index.eq(source.copy_index),
            destination.capture_w_index.eq(source.copy_w_index),
            destination.capture_valid.eq(0),
            destination.capture_data.eq(source.copy_data),
            destination.capture_commit.eq(0),
            destination.capture_abort.eq(~self.enable),
        ]

        with m.FSM(domain="usb", init="RESET"):
            with m.State("RESET"):
                m.d.comb += destination.clear.eq(1)
                m.d.usb += [
                    self.done.eq(0),
                    self.failed.eq(0),
                    slot.eq(0),
                    offset.eq(0),
                ]
                with m.If(self.enable):
                    m.next = "METADATA_WAIT_1"

            with m.State("METADATA_WAIT_1"):
                m.d.comb += source.copy_request.eq(1)
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Elif(source.copy_ready):
                    m.next = "METADATA_WAIT_2"

            with m.State("METADATA_WAIT_2"):
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Elif(source.copy_response):
                    m.next = "CHECK_SLOT"

            with m.State("CHECK_SLOT"):
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Elif(source.copy_valid):
                    m.next = "START_CAPTURE"
                with m.Else():
                    m.next = "ADVANCE_SLOT"

            with m.State("START_CAPTURE"):
                m.d.comb += destination.capture_start.eq(1)
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Elif(destination.capture_overflow):
                    m.d.comb += destination.capture_abort.eq(1)
                    m.d.usb += self.failed.eq(1)
                    m.next = "FAILED"
                with m.Elif(destination.capture_ready):
                    with m.If(source.copy_length == 0):
                        m.next = "COMMIT_CAPTURE"
                    with m.Else():
                        m.d.usb += offset.eq(0)
                        m.next = "DATA_WAIT_1"

            with m.State("DATA_WAIT_1"):
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Else():
                    m.next = "DATA_WAIT_2"

            with m.State("DATA_WAIT_2"):
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Else():
                    m.next = "WRITE_BYTE"

            with m.State("WRITE_BYTE"):
                # The shared source RAM may be preempted by a live device
                # serve. Hold the byte address and destination write until
                # the source explicitly grants this copy read.
                m.d.comb += destination.capture_valid.eq(source.copy_data_valid)
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Elif(~source.copy_data_valid):
                    pass
                with m.Elif(~destination.capture_ready):
                    m.d.comb += destination.capture_abort.eq(1)
                    m.d.usb += self.failed.eq(1)
                    m.next = "FAILED"
                with m.Elif(offset + 1 >= source.copy_length):
                    m.next = "COMMIT_CAPTURE"
                with m.Else():
                    m.d.usb += offset.eq(offset + 1)
                    m.next = "DATA_WAIT_1"

            with m.State("COMMIT_CAPTURE"):
                m.d.comb += destination.capture_commit.eq(1)
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Else():
                    m.next = "ADVANCE_SLOT"

            with m.State("ADVANCE_SLOT"):
                with m.If(~self.enable):
                    m.next = "RESET"
                with m.Elif(slot == DESCRIPTOR_ENTRY_COUNT - 1):
                    m.d.usb += self.done.eq(1)
                    m.next = "DONE"
                with m.Else():
                    m.d.usb += [slot.eq(slot + 1), offset.eq(0)]
                    m.next = "METADATA_WAIT_1"

            with m.State("DONE"), m.If(~self.enable):
                m.d.usb += self.done.eq(0)
                m.next = "RESET"

            with m.State("FAILED"), m.If(~self.enable):
                m.d.usb += self.failed.eq(0)
                m.next = "RESET"

        return m


class _RelayInEndpoint(USBStreamInEndpoint):
    """LUNA's IN stream endpoint, returned whole to its initial state by ``reset``.

    As for ``_RelayOutEndpoint``: USB requires DATA0 after a bus reset and after
    SET_CONFIGURATION [USB2.0 9.1.1.5, 9.4.5], and LUNA resets the toggle only
    on ClearFeature(ENDPOINT_HALT). The reset also empties the transfer
    manager's packet buffers, which fill from the relay whether or not the PC
    is polling -- and the PC polls nothing until it configures the clone.
    """

    def __init__(self, *, endpoint_number: Signal, max_packet_size: int) -> None:
        super().__init__(endpoint_number=endpoint_number, max_packet_size=max_packet_size)
        self.reset = Signal()

    def elaborate(self, platform):
        return ResetInserter({"usb": self.reset})(super().elaborate(platform))


#: One distinct subclass per relay endpoint, so that synthesis is reproducible.
#:
#: LUNA names endpoint submodules after their class, and falls back to
#: ``f"{name}_{id(endpoint)}"`` for the second and later instance of the same
#: class (``luna/gateware/usb/usb2/device.py``). ``id()`` is an object address,
#: so every build emitted different instance names, a different RTLIL, and --
#: because yosys cell naming feeds abc9's cut selection -- a different netlist.
#: Every relay endpoint shares a base class, so all but the first were named
#: this way.
#:
#: That is not cosmetic here. Measured 2026-09-15: two builds from identical
#: source and an identical pinned toolchain produced netlists of the same size
#: but different content (sha f896e7d3 vs f2cf4f37), and seed 9 came out at
#: 59.69 MHz FAIL on one and 62.21 MHz PASS on the other. Run-to-run naming
#: churn alone moved the pinned seed across the 60.00 MHz constraint, so no
#: build was reproducible and no seed sweep described the design rather than
#: one throwaway netlist.
#:
#: Giving each endpoint its own class keeps LUNA on its stable-name path;
#: they differ from LUNA's only by ``_RelayInEndpoint``'s reset. One class per
#: relay SLOT: a slot's endpoint number is bound at runtime (see
#: MouseCloneDevice), so the classes no longer carry one.
_RELAY_ENDPOINT_CLASSES = tuple(
    type(f"USBStreamInEndpointSlot{slot}", (_RelayInEndpoint,), {}) for slot in range(MAX_ENDPOINTS)
)


class _RelayOutEndpoint(USBStreamOutEndpoint):
    """LUNA's OUT stream endpoint on a runtime endpoint number.

    LUNA reads ``_endpoint_number`` only in two Amaranth ``==`` comparisons
    (the token's endpoint, and ClearFeature(ENDPOINT_HALT)'s), so a Signal
    serves as well as an int -- for its IN endpoints too, which are bound the
    same way. The clone learns its number from the captured device, after
    elaboration.

    Exactly one instance of this distinct class exists, which keeps LUNA on its
    stable class-name path; see ``_RELAY_ENDPOINT_CLASSES``.

    ``reset`` returns the whole endpoint -- expected data toggle and FIFO -- to
    its initial state. LUNA resets the toggle only on ClearFeature(ENDPOINT_HALT),
    but USB requires DATA0 after a bus reset and after SET_CONFIGURATION
    [USB2.0 9.1.1.5, 9.4.5]. Without it, a session that ended on the odd toggle
    made the PC's first OUT of the next one look like a resend: ACKed, and
    silently discarded.

    The reset is applied inside ``elaborate`` rather than by wrapping the
    instance, so USBDevice still finds ``.interface`` and still names the
    submodule after this class.
    """

    def __init__(self, *, endpoint_number: Signal) -> None:
        # max_packet_size is the relay's bound, not the device's
        # wMaxPacketSize: LUNA marks ``last`` only on a short packet, and the
        # writer frames a full one by its own count.
        super().__init__(endpoint_number=endpoint_number, max_packet_size=MAX_PACKET_SIZE)
        self.reset = Signal()

    def elaborate(self, platform):
        return ResetInserter({"usb": self.reset})(super().elaborate(platform))


class MouseCloneDevice(Elaboratable):
    """Present captured descriptors and reports as a PC-facing USB device.

    LUNA's ``USBDevice`` keeps its configuration number internal, so
    ``configured`` follows the standard request handler's configuration
    change output.
    """

    def __init__(self, bus, store, control_relay=None):
        self._bus = bus
        self.store = store
        # The host-side ControlRelay that forwards HID class control requests
        # to the real device on TARGET. None keeps the relay-less behaviour
        # (STALL unknown class requests), which the diagnostic tops use.
        # Distinct from ``self.relay`` below, which relays *reports*.
        self.control_relay = control_relay

        self.connect = Signal()
        self.configured = Signal()
        self.copy_enable = Signal()
        self.copy_done = Signal()
        self.copy_failed = Signal()

        # Passive device/control observations for diagnostics.
        self.debug_reset_detected = Signal()
        self.debug_setup_received = Signal()
        self.debug_setup_request_type = Signal(8)
        self.debug_setup_request = Signal(8)
        self.debug_setup_value = Signal(16)
        self.debug_setup_index = Signal(16)
        self.debug_setup_length = Signal(16)
        self.debug_address_changed = Signal()
        self.debug_new_address = Signal(7)
        self.debug_config_changed = Signal()
        self.debug_new_config = Signal(8)
        self.debug_control_tx_valid = Signal()
        self.debug_control_stall = Signal()

        # Speed policy and observability for the AUX (device) port.
        #
        # ``full_speed_only`` is an input rather than a constant so the AUX
        # speed can be chosen independently of what TARGET negotiated: the two
        # ports negotiate separately and need not match. A PC that polls faster
        # than the mouse supplies simply collects NAKs, which is normal traffic
        # on an interrupt IN endpoint, not an error.
        #
        # ``speed`` is LUNA's negotiated result (USBSpeed: 0=HIGH, 1=FULL,
        # 2=LOW), driven by its USBResetSequencer after the chirp handshake.
        self.full_speed_only = Signal(init=1)
        self.speed = Signal(2, init=1)

        self.report_valid = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()
        self.report_endpoint = Signal(4)
        self.report_ready = Signal()

        # The real device's bMaxPacketSize0, from the host's enumerator.
        #
        # The clone serves that device's descriptor verbatim, byte 7 included,
        # so the PC splits every EP0 data stage at this size and a longer
        # packet is babble. Both control handlers packetise their IN data at
        # it. Unconnected, it keeps the fixed 64 used before it existed.
        self.ep0_max_packet = Signal(7, init=MAX_PACKET_SIZE)
        # Registered and legalised copy the handlers actually use. Registering
        # keeps the legality check -- and the wire from the host side of the
        # die -- out of the handlers' packet arithmetic; the value only
        # changes before the clone connects.
        self._ep0_mps = Signal(7, init=MAX_PACKET_SIZE)

        # The four relay IN endpoints' numbers: the captured device's IN
        # endpoints, slot k serving ``in_endpoint_number[k]`` for every
        # k < ``in_endpoint_count``. Unbound slots are parked where they answer
        # nothing. Registered before use, like the OUT endpoint's below.
        self.in_endpoint_count = Signal(range(MAX_ENDPOINTS + 1))
        self.in_endpoint_number = [
            Signal(4, name=f"in_endpoint_number_{slot}") for slot in range(MAX_ENDPOINTS)
        ]
        self._in_endpoint_number = [
            Signal(5, init=UNMATCHABLE_ENDPOINT_NUMBER, name=f"in_endpoint_bound_{slot}")
            for slot in range(MAX_ENDPOINTS)
        ]

        # The one relayed interrupt-OUT endpoint, PC -> clone -> real device.
        #
        # ``out_present``/``out_endpoint_number`` are the captured device's OUT
        # endpoint. Absent, or number 0, parks the endpoint where it answers
        # nothing. Registered before use: they come from the host side of the
        # die and only change before the clone connects.
        self.out_present = Signal()
        self.out_endpoint_number = Signal(4)
        self._out_endpoint_number = Signal(5, init=UNMATCHABLE_ENDPOINT_NUMBER)
        #: Registered: the PC bus-reset or (re)configured the clone. Every
        #: relay endpoint resets on it and the report relay flushes, so a new
        #: session is served no report queued before it; so must whatever
        #: downstream holds that PC session's state: the OUT writer's partial
        #: packet, and a boot protocol the PC left the real device in.
        self.session_reset = Signal()
        self.out_valid = Signal()
        self.out_data = Signal(8)
        self.out_last = Signal()
        self.out_ready = Signal()

        self.relay = ReportRelay()

        self._std_handler = ClonedStandardRequestHandler(self.store, max_packet_size=self._ep0_mps)

    def elaborate(self, platform):
        del platform
        m = Module()

        m.submodules.device = device = USBDevice(bus=self._bus)
        m.submodules.relay = relay = self.relay

        control_ep = device.add_control_endpoint()
        control_ep.add_request_handler(self._std_handler)
        control_ep.add_request_handler(
            HIDClassRequestHandler(relay=self.control_relay, max_packet_size=self._ep0_mps)
        )
        setup = self._std_handler.interface.setup

        for slot, endpoint_class in enumerate(_RELAY_ENDPOINT_CLASSES):
            bound = self._in_endpoint_number[slot]
            m.d.usb += bound.eq(
                Mux(
                    self.in_endpoint_count > slot,
                    self.in_endpoint_number[slot],
                    UNMATCHABLE_ENDPOINT_NUMBER,
                )
            )
            ep = endpoint_class(endpoint_number=bound, max_packet_size=MAX_PACKET_SIZE)
            device.add_endpoint(ep)
            m.d.comb += [
                relay.slot_numbers[slot].eq(bound),
                ep.stream.stream_eq(relay.streams[slot]),
                ep.reset.eq(self.session_reset),
            ]

        out_ep = _RelayOutEndpoint(endpoint_number=self._out_endpoint_number)
        device.add_endpoint(out_ep)
        out_number = self.out_endpoint_number
        m.d.usb += self._out_endpoint_number.eq(
            Mux(self.out_present & (out_number != 0), out_number, UNMATCHABLE_ENDPOINT_NUMBER)
        )
        # Registered: it fans out to every register in the endpoint and its
        # FIFO, and across the die to the host, and the PC cannot send another
        # OUT within a cycle of either event anyway.
        m.d.usb += self.session_reset.eq(
            device.reset_detected | self._std_handler.interface.config_changed
        )
        m.d.comb += [
            relay.flush.eq(self.session_reset),
            out_ep.reset.eq(self.session_reset),
            self.out_valid.eq(out_ep.stream.valid),
            self.out_data.eq(out_ep.stream.payload),
            self.out_last.eq(out_ep.stream.last),
            out_ep.stream.ready.eq(self.out_ready),
        ]

        # The enumerator already refuses any other value; this is defence in
        # depth. 0 would never end a packet and 12 is no size a host accepts.
        ep0_legal = (
            (self.ep0_max_packet == 8)
            | (self.ep0_max_packet == 16)
            | (self.ep0_max_packet == 32)
            | (self.ep0_max_packet == 64)
        )
        m.d.usb += self._ep0_mps.eq(Mux(ep0_legal, self.ep0_max_packet, MAX_PACKET_SIZE))

        source_mutating = self.store.clear | self.store.capture_start | self.store.capture_busy
        copy_rearm_armed = Signal(init=1)
        with m.If(~self.copy_enable):
            m.d.usb += copy_rearm_armed.eq(1)
        with m.Elif(source_mutating):
            m.d.usb += copy_rearm_armed.eq(0)

        m.d.comb += [
            # The host owns and elaborates the shared store. Preserve the old
            # copy handshake as a readiness contract. Any descriptor mutation
            # disconnects in its first cycle and fails readiness closed until
            # the host drops copy_enable to arm a new stable generation.
            self.copy_done.eq(self.copy_enable & copy_rearm_armed & ~source_mutating),
            self.copy_failed.eq(0),
            self.debug_reset_detected.eq(device.reset_detected),
            self.debug_setup_received.eq(setup.received),
            self.debug_setup_request_type.eq(Cat(setup.recipient, setup.type, setup.is_in_request)),
            self.debug_setup_request.eq(setup.request),
            self.debug_setup_value.eq(setup.value),
            self.debug_setup_index.eq(setup.index),
            self.debug_setup_length.eq(setup.length),
            self.debug_address_changed.eq(self._std_handler.interface.address_changed),
            self.debug_new_address.eq(self._std_handler.interface.new_address),
            self.debug_config_changed.eq(self._std_handler.interface.config_changed),
            self.debug_new_config.eq(self._std_handler.interface.new_config),
            self.debug_control_tx_valid.eq(self._std_handler.interface.tx.valid),
            self.debug_control_stall.eq(self._std_handler.interface.handshakes_out.stall),
            device.connect.eq(self.connect & self.copy_done),
            device.full_speed_only.eq(self.full_speed_only),
            self.speed.eq(device.speed),
            relay.report_valid.eq(self.report_valid),
            relay.report_data.eq(self.report_data),
            relay.report_first.eq(self.report_first),
            relay.report_last.eq(self.report_last),
            relay.report_endpoint.eq(self.report_endpoint),
            self.report_ready.eq(relay.report_ready),
        ]

        # USBDevice does not expose its configuration number, so track the
        # handler's SET_CONFIGURATION result and clear it on bus reset.
        with m.If(device.reset_detected):
            m.d.usb += self.configured.eq(0)
        with m.Elif(self._std_handler.interface.config_changed):
            m.d.usb += self.configured.eq(self._std_handler.interface.new_config != 0)

        return m
