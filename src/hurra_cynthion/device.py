"""AUX-side USB device that clones the captured mouse and relays its reports."""

from amaranth import Cat, Elaboratable, Module, Signal
from luna.gateware.usb.usb2.device import USBDevice
from luna.gateware.usb.usb2.endpoints.stream import USBStreamInEndpoint

from .descriptors import DESCRIPTOR_ENTRY_COUNT, MAX_PACKET_SIZE, RELAY_ENDPOINT_NUMBERS
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


class MouseCloneDevice(Elaboratable):
    """Present captured descriptors and reports as a PC-facing USB device.

    LUNA's ``USBDevice`` keeps its configuration number internal, so
    ``configured`` follows the standard request handler's configuration
    change output.
    """

    def __init__(self, bus, store):
        self._bus = bus
        self.store = store

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

        self.relay = ReportRelay(endpoint_numbers=RELAY_ENDPOINT_NUMBERS)

        self._std_handler = ClonedStandardRequestHandler(
            self.store, max_packet_size=MAX_PACKET_SIZE
        )

    def elaborate(self, platform):
        del platform
        m = Module()

        m.submodules.device = device = USBDevice(bus=self._bus)
        m.submodules.relay = relay = self.relay

        control_ep = device.add_control_endpoint()
        control_ep.add_request_handler(self._std_handler)
        control_ep.add_request_handler(HIDClassRequestHandler())
        setup = self._std_handler.interface.setup

        for index, epnum in enumerate(RELAY_ENDPOINT_NUMBERS):
            ep = USBStreamInEndpoint(endpoint_number=epnum, max_packet_size=MAX_PACKET_SIZE)
            device.add_endpoint(ep)
            m.d.comb += ep.stream.stream_eq(relay.streams[index])

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
