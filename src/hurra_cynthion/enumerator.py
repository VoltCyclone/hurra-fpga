# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from amaranth import Array, Elaboratable, Module, Mux, Signal

from .control import USBControlTransferEngine
from .descriptors import (
    MAX_CONFIGURATION_SIZE,
    MAX_ENDPOINTS,
    MAX_INTERFACES,
    MAX_PACKET_SIZE,
    MAX_RELAY_ENDPOINT_NUMBER,
    MAX_REPORT_SIZE,
    MAX_STRING_SIZE,
    DescriptorStore,
)
from .timing import HostTiming
from .types import HostError, TransactionStatus

GET_DESCRIPTOR = 6
SET_ADDRESS = 5
SET_CONFIGURATION = 9

# Some devices are slow to come up and fail the first enumeration attempt(s);
# re-reset and retry the whole sequence a bounded number of times before giving
# up (mirrors WCH USBH_EnumRootDevice's 6-attempt loop used by Hurra-v3).
MAX_ENUM_ATTEMPTS = 6


#: UTMI ``line_state`` encodings. K and J are swapped between Full and Low
#: Speed; this host is Full/High Speed only, so these are the FS/HS senses.
_LINE_STATE_SE0 = 0b00
_LINE_STATE_J = 0b01
_LINE_STATE_K = 0b10

#: K-J pairs the host drives during its chirp. USB 2.0 section 7.1.7.5 requires
#: at least three before the device may conclude the host is High Speed capable.
_HOST_CHIRP_PAIRS = 3


class BoundedMouseEnumerator(Elaboratable):
    """Power, reset, and enumerate one bounded full-speed HID boot mouse."""

    def __init__(self, timing=None, control=None, descriptor_store=None) -> None:
        self.timing = timing if timing is not None else HostTiming.hardware()
        self.control = (
            control if control is not None else USBControlTransferEngine(timing=self.timing)
        )
        self.descriptor_store = (
            descriptor_store if descriptor_store is not None else DescriptorStore()
        )

        self.enable = Signal()
        self.line_state = Signal(2)
        #: The PHY's high-speed disconnect detection. At High Speed the bus
        #: rests at SE0, so a disconnect cannot be read off ``line_state``; the
        #: PHY measures it as a voltage during the EOP of a SOF and reports it
        #: here. Unused at Full Speed.
        self.host_disconnect = Signal()
        #: Set by the host when a poller has exhausted its transport-error
        #: budget on timeouts -- three polls in a row that nothing answered.
        #: Treated as a detach, because that is what it is evidence of.
        self.device_unresponsive = Signal()
        #: Runtime override: never attempt the high-speed handshake, so the link
        #: stays Full Speed. The escape hatch for a device that misbehaves at
        #: High Speed, without building a second bitstream.
        self.force_full_speed = Signal()
        # This host sources port power itself (AUX -> TARGET-A). The target PHY
        # only senses TARGET-C VBUS, so its vbus_valid does not reflect the power
        # this host applies. Presence is therefore gated on the host-commanded
        # power state (see ``powered`` in elaborate), not on a PHY VBUS input.

        self.target_discharge = Signal()
        self.aux_vbus_en = Signal()
        self.dp_pulldown = Signal()
        self.dm_pulldown = Signal()
        self.phy_xcvr_select = Signal(2)
        self.phy_term_select = Signal()
        self.phy_op_mode = Signal(2)
        self.traffic_enable = Signal()
        #: Raw transmitter drive for the high-speed chirp. Routed through the
        #: transaction engine, which owns ``utmi.tx``; honoured only while that
        #: engine is idle, which bus reset guarantees.
        self.chirp_tx_valid = Signal()
        self.chirp_tx_data = Signal(8)

        self.connected = Signal()
        self.enumerating = Signal()
        self.ready = Signal()
        self.error_code = Signal(4)
        self.enum_attempt = Signal(range(MAX_ENUM_ATTEMPTS + 1))
        self.device_address = Signal(7)
        self.ep0_max_packet = Signal(7, init=8)
        self.configuration_value = Signal(8)
        #: The negotiated TARGET link speed: 1 once a High Speed chirp handshake
        #: has completed, 0 for Full Speed. This is the single source of truth
        #: for speed across the design -- the frame scheduler, the interrupt
        #: poller, the transaction engine's interpacket timing and the AUX
        #: device's own speed policy all key off it.
        #:
        #: Full Speed is the default *and* the fallback, never an error path:
        #: most HID devices are Full Speed only and simply do not chirp, so this
        #: staying 0 is the ordinary outcome. It is asserted only on a completed
        #: handshake, and cleared on every detach and every enumeration retry --
        #: a retry re-runs the bus reset, which is when speed is negotiated, so
        #: the previous result must not carry over.
        self.high_speed = Signal()
        # Endpoint table: one row per captured interrupt-IN endpoint across all
        # HID interfaces of the composite device.
        self.ep_count = Signal(range(MAX_ENDPOINTS + 1))
        self.ep_interface = Array(
            [Signal(8, name=f"ep_interface_{k}") for k in range(MAX_ENDPOINTS)]
        )
        self.ep_number = Array([Signal(4, name=f"ep_number_{k}") for k in range(MAX_ENDPOINTS)])
        self.ep_max_packet = Array(
            [Signal(7, name=f"ep_max_packet_{k}") for k in range(MAX_ENDPOINTS)]
        )
        self.ep_interval = Array([Signal(8, name=f"ep_interval_{k}") for k in range(MAX_ENDPOINTS)])
        self.ep_report_length = Array(
            [Signal(16, name=f"ep_report_length_{k}") for k in range(MAX_ENDPOINTS)]
        )

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.control = control = self.control
        m.submodules.descriptor_store = store = self.descriptor_store

        timing = self.timing
        # Host-commanded TARGET-A port power. Asserted after the discharge
        # window and used as the port-power-present gate in place of any PHY
        # VBUS sense.
        powered = Signal()
        power_counter = Signal(range(timing.vbus_discharge_cycles + 1))
        attach_counter = Signal(range(timing.attach_stable_cycles + 1))
        attach_state = Signal(2)
        reset_counter = Signal(range(timing.reset_cycles + 1))
        reset_recovery_counter = Signal(range(timing.reset_recovery_cycles + 1))
        address_recovery_counter = Signal(range(timing.address_recovery_cycles + 1))
        detach_counter = Signal(range(timing.detach_stable_cycles + 1))
        reset_active = Signal()
        # High-speed detection handshake state (USB 2.0 section 7.1.7.5).
        chirp_counter = Signal(
            range(max(timing.chirp_detect_cycles, timing.chirp_drive_cycles) + 1)
        )
        chirp_pairs = Signal(range(_HOST_CHIRP_PAIRS + 1))
        # Latched once the handshake has been attempted, either way. Without it
        # the reset window, which continues after the chirp, would re-enter the
        # sequence on the device's next K.
        chirp_done = Signal()
        enum_attempt = self.enum_attempt
        # Overall time budget for the device to present a stable J after reset,
        # tolerating the SE0 it emits while rebooting. ~500 ms at the usb clock.
        recovery_timeout_cycles = timing.clock_hz // 2
        recovery_wait = Signal(range(recovery_timeout_cycles + 1))
        # The host emits a SOF every frame during recovery, which briefly drives the
        # bus off idle. Tolerate non-J shorter than one frame so those SOFs do not
        # reset the stable-J debounce; only a non-J sustained beyond a frame counts
        # as the device dropping its pull-up.
        nonj_tolerance_cycles = timing.frame_cycles
        nonj_counter = Signal(range(nonj_tolerance_cycles + 1))

        byte_count = Signal(16)
        declared_length = Signal(16)
        header_length = Signal(8)
        header_type = Signal(8)
        malformed = Signal()
        oversized = Signal()
        unsupported = Signal()

        manufacturer_index = Signal(8)
        product_index = Signal(8)
        serial_index = Signal(8)
        string_indexes = Array([manufacturer_index, product_index, serial_index])
        string_cursor = Signal(range(4))
        current_string_index = Signal(8)
        language_id = Signal(16)
        language_low = Signal(8)

        descriptor_position = Signal(8)
        current_descriptor_length = Signal(8)
        current_descriptor_type = Signal(8)
        interface_count = Signal(range(MAX_INTERFACES + 2))
        declared_interfaces = Signal(8)
        cur_is_hid = Signal()
        cur_interface = Signal(8)
        cur_report_length = Signal(16)
        mouse_seen = Signal()
        endpoint_mps_low = Signal(8)
        ep_addr = Signal(8)
        ep_attrs = Signal(8)
        ep_mps = Signal(16)
        hid_declared_count = Signal(8)
        hid_subordinate_phase = Signal(2)
        hid_subordinate_type = Signal(8)
        hid_subordinate_length_low = Signal(8)
        report_cursor = Signal(range(MAX_ENDPOINTS + 1))
        # A report descriptor is a per-interface property. Endpoints of one
        # interface are contiguous in the table, so tracking the interface of
        # the last fetch lets the report loop skip a redundant GET (and store
        # commit) for a second interrupt-IN endpoint on the same interface.
        report_fetched = Signal()
        report_fetched_interface = Signal(8)

        stream_accepted = control.data_valid & control.data_ready
        # A device is gone when host power is off, or when the bus has held SE0
        # long enough to qualify a disconnect (the host pull-downs drive both
        # lines low once the device pull-up is removed).
        # What "the device is gone" looks like depends on the negotiated speed.
        #
        # At Full Speed the host pull-downs drive both lines low as soon as the
        # device removes its pull-up, so a sustained SE0 is a disconnect.
        #
        # At High Speed SE0 is the *idle* state -- the bus rests there between
        # packets -- so the same test would declare a disconnect the instant the
        # link entered High Speed. High Speed disconnect is instead a voltage
        # the PHY measures during the EOP of a SOF, reported on host_disconnect.
        # The debounce is shared; only the condition being debounced changes.
        #
        # A second, speed-independent trigger: a poller that has run out of
        # transport-error budget on *timeouts* has had three polls in a row
        # answered by nothing at all, which is evidence of a gone device that
        # does not depend on the PHY reporting anything.
        #
        # This is not belt and braces. ``detached`` is the only way out of
        # READY, and out of ERROR, so it is the single recovery path for the
        # whole design -- and at High Speed ``host_disconnect`` was its only
        # source. On hardware that signal did not assert on unplug, and the
        # host stayed in READY with polling permanently failed: the poller's
        # ``failed`` latch clears only when its ``enable`` drops, ``enable``
        # follows ``connected``, and ``connected`` was waiting for the detach
        # that never came. Replugging could not recover it; only reconfiguring
        # the FPGA did.
        #
        # Timeouts specifically, not any failure: a device that STALLs is
        # present and answering, and re-enumerating it in a loop would trade a
        # wedge for a spin and throw away the diagnosis. That path still ends
        # at POLLING_FAILURE.
        disconnect_seen = (
            Mux(self.high_speed, self.host_disconnect, self.line_state == 0)
            | self.device_unresponsive
        )
        detached = ~powered | (
            disconnect_seen & (detach_counter >= timing.detach_stable_cycles - 1)
        )
        # The idle bus is a J at Full Speed, held there by the device's pull-up,
        # and SE0 at High Speed.
        idle_line_state = Mux(self.high_speed, _LINE_STATE_SE0, _LINE_STATE_J)

        m.d.comb += [
            self.target_discharge.eq(0),
            self.aux_vbus_en.eq(powered),
            self.dp_pulldown.eq(1),
            self.dm_pulldown.eq(1),
            # Normal operation at the negotiated speed. Full Speed selects the
            # FS transceiver and presents FS termination; High Speed selects the
            # HS transceiver and HS termination. This default is what puts the
            # link into High Speed once the chirp handshake has succeeded, since
            # every state that does not override it inherits it.
            self.phy_xcvr_select.eq(~self.high_speed),
            self.phy_term_select.eq(~self.high_speed),
            self.phy_op_mode.eq(0),
            self.chirp_tx_valid.eq(0),
            self.chirp_tx_data.eq(0),
            reset_active.eq(0),
            control.start.eq(0),
            control.connected.eq(self.connected),
            control.address.eq(self.device_address),
            control.request_type.eq(0),
            control.request.eq(0),
            control.value.eq(0),
            control.index.eq(0),
            control.length.eq(0),
            control.max_packet_size.eq(self.ep0_max_packet),
            control.out_payload.eq(0),
            control.data_ready.eq(0),
            store.clear.eq(0),
            store.capture_start.eq(0),
            store.capture_type.eq(0),
            store.capture_index.eq(0),
            store.capture_w_index.eq(0),
            store.capture_valid.eq(0),
            store.capture_data.eq(control.data),
            store.capture_commit.eq(0),
            store.capture_abort.eq(0),
        ]

        with m.If(~self.connected | reset_active | ~disconnect_seen):
            m.d.usb += detach_counter.eq(0)
        with m.Elif(detach_counter < timing.detach_stable_cycles):
            m.d.usb += detach_counter.eq(detach_counter + 1)

        def clear_session() -> None:
            m.d.usb += [
                self.connected.eq(0),
                self.enumerating.eq(0),
                self.ready.eq(0),
                self.device_address.eq(0),
                self.ep0_max_packet.eq(8),
                self.configuration_value.eq(0),
                self.ep_count.eq(0),
                self.traffic_enable.eq(0),
                self.high_speed.eq(0),
                detach_counter.eq(0),
            ]

        def go_detached() -> None:
            clear_session()
            m.d.usb += [
                self.error_code.eq(HostError.DISCONNECTED.value),
                attach_counter.eq(0),
            ]
            m.d.comb += [store.clear.eq(1), store.capture_abort.eq(1)]
            m.next = "WAIT_ATTACH"

        def fail(error: HostError) -> None:
            m.d.usb += [
                self.enumerating.eq(0),
                self.ready.eq(0),
                self.error_code.eq(error.value),
            ]
            m.d.comb += store.capture_abort.eq(1)
            m.next = "ERROR"

        def retry_enumeration() -> None:
            # Re-reset the port and re-run enumeration from scratch, keeping the
            # device attached. The reset returns the device to address 0, so the
            # retry is clean. All per-device state (address, ep0 size, endpoint
            # table, descriptor store) is cleared so composite capture restarts.
            m.d.usb += [
                enum_attempt.eq(enum_attempt + 1),
                self.enumerating.eq(1),
                self.ready.eq(0),
                self.device_address.eq(0),
                self.ep0_max_packet.eq(8),
                self.configuration_value.eq(0),
                self.ep_count.eq(0),
                self.high_speed.eq(0),
                self.error_code.eq(HostError.NONE.value),
                reset_counter.eq(0),
                chirp_counter.eq(0),
                chirp_pairs.eq(0),
                chirp_done.eq(0),
            ]
            m.d.comb += [store.clear.eq(1), store.capture_abort.eq(1)]
            m.next = "BUS_RESET"

        def handle_control_failure() -> None:
            with m.If(control.status == TransactionStatus.DISCONNECTED.value):
                go_detached()
            with m.Elif(enum_attempt < MAX_ENUM_ATTEMPTS - 1):
                retry_enumeration()
            with m.Else():
                fail(HostError.CONTROL_FAILURE)

        def start_request(
            *, request_type: int, request: int, value, index, length, address=None
        ) -> None:
            m.d.comb += [
                control.start.eq(1),
                control.request_type.eq(request_type),
                control.request.eq(request),
                control.value.eq(value),
                control.index.eq(index),
                control.length.eq(length),
            ]
            if address is not None:
                m.d.comb += control.address.eq(address)

        def begin_capture(descriptor_type, descriptor_index, w_index) -> None:
            m.d.comb += [
                store.capture_start.eq(1),
                store.capture_type.eq(descriptor_type),
                store.capture_index.eq(descriptor_index),
                store.capture_w_index.eq(w_index),
            ]
            m.d.usb += [
                byte_count.eq(0),
                header_length.eq(0),
                header_type.eq(0),
                malformed.eq(0),
                oversized.eq(0),
                unsupported.eq(0),
            ]

        def connect_stream_to_store() -> None:
            m.d.comb += [
                control.data_ready.eq(store.capture_ready),
                store.capture_valid.eq(control.data_valid),
            ]

        def check_detach_or_control_done() -> None:
            with m.If(detached):
                go_detached()
            with m.Elif(store.capture_overflow):
                fail(HostError.OVERSIZED_DESCRIPTOR)

        with m.FSM(domain="usb"):
            with m.State("POWER_OFF"):
                m.d.usb += powered.eq(0)
                with m.If(self.enable):
                    clear_session()
                    m.d.usb += [
                        power_counter.eq(0),
                        self.error_code.eq(HostError.NONE.value),
                    ]
                    m.next = "DISCHARGE"

            with m.State("DISCHARGE"):
                m.d.comb += self.target_discharge.eq(1)
                with m.If(~self.enable):
                    m.next = "POWER_OFF"
                with m.Elif(power_counter == timing.vbus_discharge_cycles - 1):
                    m.d.usb += [
                        powered.eq(1),
                        attach_counter.eq(0),
                        self.error_code.eq(HostError.DISCONNECTED.value),
                    ]
                    m.next = "WAIT_ATTACH"
                with m.Else():
                    m.d.usb += power_counter.eq(power_counter + 1)

            with m.State("WAIT_ATTACH"):
                with m.If(~self.enable):
                    clear_session()
                    m.d.comb += store.clear.eq(1)
                    m.next = "POWER_OFF"
                with m.Elif(~powered | (self.line_state == 0)):
                    m.d.usb += [attach_counter.eq(0), self.error_code.eq(HostError.DISCONNECTED)]
                with m.Else():
                    with m.If((attach_counter == 0) | (attach_state != self.line_state)):
                        m.d.usb += [
                            attach_state.eq(self.line_state),
                            attach_counter.eq(1),
                            self.error_code.eq(HostError.NONE.value),
                        ]
                    with m.Elif(attach_counter == timing.attach_stable_cycles - 1):
                        m.d.usb += attach_counter.eq(0)
                        with m.If(self.line_state != 1):
                            m.d.usb += self.error_code.eq(HostError.UNSUPPORTED_SPEED.value)
                            m.next = "ERROR"
                        with m.Else():
                            clear_session()
                            m.d.usb += [
                                self.connected.eq(1),
                                self.enumerating.eq(1),
                                self.error_code.eq(HostError.NONE.value),
                                reset_counter.eq(0),
                                enum_attempt.eq(0),
                                chirp_counter.eq(0),
                                chirp_pairs.eq(0),
                                chirp_done.eq(0),
                            ]
                            m.d.comb += store.clear.eq(1)
                            m.next = "BUS_RESET"
                    with m.Else():
                        m.d.usb += attach_counter.eq(attach_counter + 1)

            # The chirp drive configuration, shared by BUS_RESET and every
            # chirp state: high-speed transceiver, no Full Speed pull-up, and
            # op_mode 2 so the PHY passes transmitted bytes through without
            # NRZI encoding or bit stuffing. Driving nothing in this mode is
            # SE0 -- the reset itself; driving 0x00 is a chirp K and 0xFF a J.
            def drive_chirp_mode() -> None:
                m.d.comb += [
                    self.phy_xcvr_select.eq(0),
                    self.phy_term_select.eq(0),
                    self.phy_op_mode.eq(2),
                    reset_active.eq(1),
                ]

            with m.State("BUS_RESET"):
                drive_chirp_mode()
                with m.If(~self.enable | ~powered):
                    go_detached()
                with m.Elif(reset_counter == timing.reset_cycles - 1):
                    m.d.usb += [
                        reset_counter.eq(0),
                        reset_recovery_counter.eq(0),
                        recovery_wait.eq(0),
                    ]
                    m.next = "RESET_RECOVERY"
                with m.Else():
                    m.d.usb += reset_counter.eq(reset_counter + 1)
                    # Watch for the device's chirp K, once per reset. A High
                    # Speed capable device chirps K for 1-7 ms starting shortly
                    # after it sees the reset; a Full Speed device never does,
                    # which is the common case and costs only the wait.
                    with m.If(~chirp_done & ~self.force_full_speed):
                        with m.If(self.line_state == _LINE_STATE_K):
                            with m.If(chirp_counter >= timing.chirp_detect_cycles - 1):
                                m.d.usb += [chirp_counter.eq(0), chirp_pairs.eq(0)]
                                m.next = "CHIRP_AWAIT_DEVICE_END"
                            with m.Else():
                                m.d.usb += chirp_counter.eq(chirp_counter + 1)
                        with m.Else():
                            m.d.usb += chirp_counter.eq(0)
                            # The chirp deadline is measured from the start of
                            # this same reset, which reset_counter already
                            # tracks -- a second counter would only duplicate
                            # it, and this design has no spare logic.
                            with m.If(reset_counter >= timing.chirp_timeout_cycles - 1):
                                # No chirp inside the window the device is
                                # allowed. It is Full Speed only; stop looking
                                # and let the rest of the reset run out.
                                m.d.usb += chirp_done.eq(1)

            # The host must not answer until the device has finished chirping,
            # or its K and the device's would overlap and neither side could
            # time the other's. Debounced with the same qualifier used to
            # detect the chirp, so a momentary dip does not end it early.
            with m.State("CHIRP_AWAIT_DEVICE_END"):
                drive_chirp_mode()
                with m.If(~self.enable | ~powered):
                    go_detached()
                with m.Elif(self.line_state != _LINE_STATE_K):
                    with m.If(chirp_counter >= timing.chirp_detect_cycles - 1):
                        m.d.usb += chirp_counter.eq(0)
                        m.next = "CHIRP_DRIVE_K"
                    with m.Else():
                        m.d.usb += chirp_counter.eq(chirp_counter + 1)
                with m.Else():
                    m.d.usb += chirp_counter.eq(0)

            # The host answers with K-J-K-J-K-J, each 40-60 us. An all-zeroes
            # byte in chirp mode is a K; all-ones is a J.
            # The host answers with an unbroken alternation of K and J, each
            # 40-60 us. Three pairs is the minimum the device must see, but it
            # is NOT where the host stops: USB 2.0 section 7.1.7.5 requires the
            # alternation to continue until 100-500 us before the reset ends,
            # with no idle between chirps.
            #
            # Stopping at three pairs is a trap that looks like it works. The
            # device switches to High Speed within 500 us of seeing them, and a
            # High Speed device reads 3 ms of squelch as a reset -- so holding
            # SE0 for the remaining ~49.7 ms of a 50 ms reset knocks the device
            # straight back down to Full Speed, after a handshake that by every
            # local measure succeeded.
            with m.State("CHIRP_DRIVE_K"):
                drive_chirp_mode()
                m.d.comb += [
                    self.chirp_tx_valid.eq(1),
                    self.chirp_tx_data.eq(0x00),
                ]
                m.d.usb += reset_counter.eq(reset_counter + 1)
                with m.If(~self.enable | ~powered):
                    go_detached()
                with m.Elif(chirp_counter >= timing.chirp_drive_cycles - 1):
                    m.d.usb += chirp_counter.eq(0)
                    m.next = "CHIRP_DRIVE_J"
                with m.Else():
                    m.d.usb += chirp_counter.eq(chirp_counter + 1)

            with m.State("CHIRP_DRIVE_J"):
                drive_chirp_mode()
                m.d.comb += [
                    self.chirp_tx_valid.eq(1),
                    self.chirp_tx_data.eq(0xFF),
                ]
                m.d.usb += reset_counter.eq(reset_counter + 1)
                with m.If(~self.enable | ~powered):
                    go_detached()
                with m.Elif(chirp_counter >= timing.chirp_drive_cycles - 1):
                    m.d.usb += chirp_counter.eq(0)
                    # Latch the speed once the device has had its three pairs.
                    # It acts on them immediately; the rest of the alternation
                    # exists to keep the bus out of squelch until the reset is
                    # nearly over.
                    with m.If(chirp_pairs < _HOST_CHIRP_PAIRS):
                        m.d.usb += chirp_pairs.eq(chirp_pairs + 1)
                        with m.If(chirp_pairs == _HOST_CHIRP_PAIRS - 1):
                            m.d.usb += [self.high_speed.eq(1), chirp_done.eq(1)]
                    with m.If(reset_counter >= timing.reset_cycles - timing.chirp_tail_cycles):
                        # Into the tail: stop driving and let the bus rest at
                        # SE0, which is High Speed idle. The device reads that
                        # squelch as "chirp over" and waits for traffic.
                        m.next = "BUS_RESET"
                    with m.Else():
                        m.next = "CHIRP_DRIVE_K"
                with m.Else():
                    m.d.usb += chirp_counter.eq(chirp_counter + 1)

            with m.State("RESET_RECOVERY"):
                # Keep SOF running for the whole post-reset settle: a device that
                # is slow to boot after reset must stay out of suspend (>3 ms idle)
                # while it comes up, and needs a long margin before the first
                # SETUP. Enabling traffic here starts SOF immediately.
                m.d.usb += [self.traffic_enable.eq(1), recovery_wait.eq(recovery_wait + 1)]
                with m.If(~self.enable):
                    go_detached()
                with m.Elif(recovery_wait == recovery_timeout_cycles - 1):
                    # The device never presented a stable J within the budget.
                    # Re-reset and retry the whole enumeration rather than hang;
                    # give up only after the attempts are exhausted.
                    with m.If(enum_attempt < MAX_ENUM_ATTEMPTS - 1):
                        retry_enumeration()
                    with m.Else():
                        fail(HostError.CONTROL_FAILURE)
                with m.Elif(self.line_state != idle_line_state):
                    # Off idle. This is either our own per-frame SOF (which briefly
                    # drives the bus off idle) or a device dropping its pull-up while
                    # it reboots after reset. Only RESET the stable-J debounce on a
                    # non-J that persists beyond one frame; a brief SOF glitch merely
                    # pauses the debounce. Without this the SOF resets the debounce
                    # every frame, capping continuous J at ~1 frame so recovery can
                    # never reach the required window and enumeration never starts.
                    with m.If(nonj_counter == nonj_tolerance_cycles - 1):
                        m.d.usb += [reset_recovery_counter.eq(0), nonj_counter.eq(0)]
                    with m.Else():
                        m.d.usb += nonj_counter.eq(nonj_counter + 1)
                with m.Elif(reset_recovery_counter == timing.reset_recovery_cycles - 1):
                    m.d.usb += [reset_recovery_counter.eq(0), nonj_counter.eq(0)]
                    m.next = "GET8_START"
                with m.Else():
                    m.d.usb += [
                        reset_recovery_counter.eq(reset_recovery_counter + 1),
                        nonj_counter.eq(0),
                    ]

            with m.State("GET8_START"):
                start_request(
                    request_type=0x80,
                    request=GET_DESCRIPTOR,
                    value=0x0100,
                    index=0,
                    length=8,
                    address=0,
                )
                m.d.usb += [byte_count.eq(0), header_length.eq(0), header_type.eq(0)]
                m.next = "GET8_WAIT"

            with m.State("GET8_WAIT"):
                m.d.comb += control.data_ready.eq(1)
                with m.If(stream_accepted):
                    m.d.usb += byte_count.eq(byte_count + 1)
                    with m.Switch(byte_count):
                        with m.Case(0):
                            m.d.usb += header_length.eq(control.data)
                        with m.Case(1):
                            m.d.usb += header_type.eq(control.data)
                        with m.Case(7):
                            m.d.usb += self.ep0_max_packet.eq(control.data[:7])
                with m.If(detached):
                    go_detached()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Elif(
                        (control.transferred != 8)
                        | (byte_count != 8)
                        | (header_length != 18)
                        | (header_type != 1)
                        | ~(
                            (self.ep0_max_packet == 8)
                            | (self.ep0_max_packet == 16)
                            | (self.ep0_max_packet == 32)
                            | (self.ep0_max_packet == 64)
                        )
                    ):
                        fail(HostError.MALFORMED_DESCRIPTOR)
                    with m.Else():
                        m.next = "SET_ADDRESS_START"

            with m.State("SET_ADDRESS_START"):
                start_request(
                    request_type=0,
                    request=SET_ADDRESS,
                    value=1,
                    index=0,
                    length=0,
                    address=0,
                )
                m.next = "SET_ADDRESS_WAIT"

            with m.State("SET_ADDRESS_WAIT"):
                with m.If(detached):
                    go_detached()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Else():
                        m.d.usb += [
                            self.device_address.eq(1),
                            address_recovery_counter.eq(0),
                        ]
                        m.next = "SET_ADDRESS_RECOVERY"

            with m.State("SET_ADDRESS_RECOVERY"):
                with m.If(~self.enable | detached):
                    go_detached()
                with m.Elif(address_recovery_counter == timing.address_recovery_cycles - 1):
                    begin_capture(1, 0, 0)
                    m.d.usb += [
                        address_recovery_counter.eq(0),
                        manufacturer_index.eq(0),
                        product_index.eq(0),
                        serial_index.eq(0),
                    ]
                    m.next = "DEVICE_START"
                with m.Else():
                    m.d.usb += address_recovery_counter.eq(address_recovery_counter + 1)

            with m.State("DEVICE_START"):
                with m.If(detached):
                    go_detached()
                with m.Else():
                    start_request(
                        request_type=0x80,
                        request=GET_DESCRIPTOR,
                        value=0x0100,
                        index=0,
                        length=18,
                    )
                    m.next = "DEVICE_WAIT"

            with m.State("DEVICE_WAIT"):
                connect_stream_to_store()
                with m.If(stream_accepted):
                    m.d.usb += byte_count.eq(byte_count + 1)
                    with m.Switch(byte_count):
                        with m.Case(0):
                            m.d.usb += header_length.eq(control.data)
                        with m.Case(1):
                            m.d.usb += header_type.eq(control.data)
                        with m.Case(7):
                            with m.If(control.data != self.ep0_max_packet):
                                m.d.usb += malformed.eq(1)
                        with m.Case(14):
                            m.d.usb += manufacturer_index.eq(control.data)
                        with m.Case(15):
                            m.d.usb += product_index.eq(control.data)
                        with m.Case(16):
                            m.d.usb += serial_index.eq(control.data)
                        with m.Case(17):
                            with m.If(control.data != 1):
                                m.d.usb += unsupported.eq(1)
                check_detach_or_control_done()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Elif((control.transferred > 18) | (byte_count > 18)):
                        fail(HostError.OVERSIZED_DESCRIPTOR)
                    with m.Elif(
                        (control.transferred != 18)
                        | (byte_count != 18)
                        | (header_length != 18)
                        | (header_type != 1)
                        | malformed
                    ):
                        fail(HostError.MALFORMED_DESCRIPTOR)
                    with m.Elif(unsupported):
                        fail(HostError.UNSUPPORTED_TOPOLOGY)
                    with m.Else():
                        m.d.comb += store.capture_commit.eq(1)
                        m.next = "CONFIG_HEADER_START"

            with m.State("CONFIG_HEADER_START"):
                start_request(
                    request_type=0x80,
                    request=GET_DESCRIPTOR,
                    value=0x0200,
                    index=0,
                    length=9,
                )
                m.d.usb += [
                    byte_count.eq(0),
                    header_length.eq(0),
                    header_type.eq(0),
                    declared_length.eq(0),
                    unsupported.eq(0),
                    malformed.eq(0),
                ]
                m.next = "CONFIG_HEADER_WAIT"

            with m.State("CONFIG_HEADER_WAIT"):
                m.d.comb += control.data_ready.eq(1)
                with m.If(stream_accepted):
                    m.d.usb += byte_count.eq(byte_count + 1)
                    with m.Switch(byte_count):
                        with m.Case(0):
                            m.d.usb += header_length.eq(control.data)
                        with m.Case(1):
                            m.d.usb += header_type.eq(control.data)
                        with m.Case(2):
                            m.d.usb += declared_length[:8].eq(control.data)
                        with m.Case(3):
                            m.d.usb += declared_length[8:16].eq(control.data)
                        with m.Case(4):
                            with m.If((control.data == 0) | (control.data > MAX_INTERFACES)):
                                m.d.usb += unsupported.eq(1)
                        with m.Case(5):
                            m.d.usb += self.configuration_value.eq(control.data)
                            with m.If(control.data == 0):
                                m.d.usb += malformed.eq(1)
                with m.If(detached):
                    go_detached()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Elif(
                        (control.transferred != 9)
                        | (byte_count != 9)
                        | (header_length != 9)
                        | (header_type != 2)
                        | (declared_length < 9)
                        | malformed
                    ):
                        fail(HostError.MALFORMED_DESCRIPTOR)
                    with m.Elif(declared_length > MAX_CONFIGURATION_SIZE):
                        fail(HostError.OVERSIZED_DESCRIPTOR)
                    with m.Elif(unsupported):
                        fail(HostError.UNSUPPORTED_TOPOLOGY)
                    with m.Else():
                        m.next = "CONFIG_PREP"

            with m.State("CONFIG_PREP"):
                begin_capture(2, 0, 0)
                m.d.usb += [
                    descriptor_position.eq(0),
                    current_descriptor_length.eq(0),
                    current_descriptor_type.eq(0),
                    interface_count.eq(0),
                    self.ep_count.eq(0),
                    cur_is_hid.eq(0),
                    cur_report_length.eq(0),
                    mouse_seen.eq(0),
                    declared_interfaces.eq(0),
                ]
                m.next = "CONFIG_START"

            with m.State("CONFIG_START"):
                start_request(
                    request_type=0x80,
                    request=GET_DESCRIPTOR,
                    value=0x0200,
                    index=0,
                    length=declared_length,
                )
                m.next = "CONFIG_WAIT"

            with m.State("CONFIG_WAIT"):
                connect_stream_to_store()
                with m.If(stream_accepted):
                    m.d.usb += byte_count.eq(byte_count + 1)
                    with m.If(descriptor_position == 0):
                        m.d.usb += current_descriptor_length.eq(control.data)
                        with m.If(control.data < 2):
                            m.d.usb += malformed.eq(1)
                        m.d.usb += descriptor_position.eq(1)
                    with m.Elif(descriptor_position == 1):
                        m.d.usb += current_descriptor_type.eq(control.data)
                        with m.If(byte_count == 1):
                            # the first descriptor must be the configuration header
                            with m.If((control.data != 2) | (current_descriptor_length != 9)):
                                m.d.usb += malformed.eq(1)
                        with m.Elif(control.data == 4):
                            # a new interface descriptor begins; reset per-interface state
                            m.d.usb += [
                                interface_count.eq(interface_count + 1),
                                cur_is_hid.eq(0),
                                cur_report_length.eq(0),
                            ]
                            with m.If(current_descriptor_length != 9):
                                m.d.usb += malformed.eq(1)
                            with m.If(interface_count >= MAX_INTERFACES):
                                m.d.usb += unsupported.eq(1)
                        with m.Elif(control.data == 0x21):
                            m.d.usb += hid_subordinate_phase.eq(0)
                            with m.If(cur_is_hid & (current_descriptor_length < 9)):
                                m.d.usb += malformed.eq(1)
                        with m.Elif(control.data == 5):
                            with m.If(current_descriptor_length != 7):
                                m.d.usb += malformed.eq(1)
                        with m.If(current_descriptor_length == 2):
                            m.d.usb += descriptor_position.eq(0)
                        with m.Else():
                            m.d.usb += descriptor_position.eq(2)
                    with m.Else():
                        with m.If(current_descriptor_type == 2):
                            with m.Switch(descriptor_position):
                                with m.Case(2):
                                    with m.If(control.data != declared_length[:8]):
                                        m.d.usb += malformed.eq(1)
                                with m.Case(3):
                                    with m.If(control.data != declared_length[8:16]):
                                        m.d.usb += malformed.eq(1)
                                with m.Case(4):
                                    m.d.usb += declared_interfaces.eq(control.data)
                                    with m.If(
                                        (control.data == 0) | (control.data > MAX_INTERFACES)
                                    ):
                                        m.d.usb += unsupported.eq(1)
                                with m.Case(5):
                                    with m.If(control.data != self.configuration_value):
                                        m.d.usb += malformed.eq(1)
                        with m.Elif(current_descriptor_type == 4):
                            with m.Switch(descriptor_position):
                                with m.Case(2):
                                    m.d.usb += cur_interface.eq(control.data)
                                with m.Case(3):
                                    with m.If(control.data != 0):
                                        m.d.usb += unsupported.eq(1)
                                with m.Case(5):
                                    m.d.usb += cur_is_hid.eq(control.data == 3)
                                with m.Case(7):
                                    with m.If(cur_is_hid & (control.data == 2)):
                                        m.d.usb += mouse_seen.eq(1)
                        with m.Elif(current_descriptor_type == 0x21):
                            with m.If(cur_is_hid):
                                with m.If(descriptor_position == 5):
                                    m.d.usb += hid_declared_count.eq(control.data)
                                    with m.If(control.data == 0):
                                        m.d.usb += malformed.eq(1)
                                with m.Elif(descriptor_position >= 6):
                                    with m.Switch(hid_subordinate_phase):
                                        with m.Case(0):
                                            m.d.usb += [
                                                hid_subordinate_type.eq(control.data),
                                                hid_subordinate_phase.eq(1),
                                            ]
                                        with m.Case(1):
                                            m.d.usb += [
                                                hid_subordinate_length_low.eq(control.data),
                                                hid_subordinate_phase.eq(2),
                                            ]
                                        with m.Case(2):
                                            m.d.usb += hid_subordinate_phase.eq(0)
                                            with m.If(hid_subordinate_type == 0x22):
                                                with m.If(cur_report_length != 0):
                                                    m.d.usb += malformed.eq(1)
                                                with m.Else():
                                                    m.d.usb += cur_report_length.eq(
                                                        hid_subordinate_length_low
                                                        | (control.data << 8)
                                                    )
                                                    with m.If(
                                                        (hid_subordinate_length_low == 0)
                                                        & (control.data == 0)
                                                    ):
                                                        m.d.usb += malformed.eq(1)
                                                    with m.Elif(
                                                        (
                                                            hid_subordinate_length_low
                                                            | (control.data << 8)
                                                        )
                                                        > MAX_REPORT_SIZE
                                                    ):
                                                        m.d.usb += oversized.eq(1)
                        with m.Elif(current_descriptor_type == 5):
                            with m.Switch(descriptor_position):
                                with m.Case(2):
                                    m.d.usb += ep_addr.eq(control.data)
                                with m.Case(3):
                                    m.d.usb += ep_attrs.eq(control.data)
                                with m.Case(4):
                                    m.d.usb += endpoint_mps_low.eq(control.data)
                                with m.Case(5):
                                    m.d.usb += ep_mps.eq((control.data << 8) | endpoint_mps_low)

                        with m.If(descriptor_position == current_descriptor_length - 1):
                            with m.If(current_descriptor_type == 0x21):
                                with m.If(
                                    cur_is_hid
                                    & (current_descriptor_length != 6 + 3 * hid_declared_count)
                                ):
                                    m.d.usb += malformed.eq(1)
                            with m.Elif(current_descriptor_type == 5):
                                # capture a HID interrupt-IN endpoint of the current interface;
                                # OUT and non-interrupt endpoints are skipped.
                                with m.If(cur_is_hid & ep_addr[7] & (ep_attrs[:2] == 3)):
                                    with m.If(
                                        (ep_addr[4:7] != 0)
                                        | (ep_addr[:4] == 0)
                                        # The clone relay serves
                                        # RELAY_ENDPOINT_NUMBERS only, and the clone
                                        # presents the captured descriptors verbatim -
                                        # so a number it cannot serve would be
                                        # advertised to the PC and then NAK forever.
                                        | (ep_addr[:4] > MAX_RELAY_ENDPOINT_NUMBER)
                                    ):
                                        m.d.usb += unsupported.eq(1)
                                    with m.Elif(ep_mps == 0):
                                        m.d.usb += malformed.eq(1)
                                    with m.Elif(ep_mps > MAX_PACKET_SIZE):
                                        m.d.usb += unsupported.eq(1)
                                    with m.Elif(control.data == 0):
                                        m.d.usb += malformed.eq(1)
                                    with m.Elif(cur_report_length == 0):
                                        m.d.usb += malformed.eq(1)
                                    with m.Elif(self.ep_count >= MAX_ENDPOINTS):
                                        m.d.usb += oversized.eq(1)
                                    with m.Else():
                                        m.d.usb += [
                                            self.ep_interface[self.ep_count].eq(cur_interface),
                                            self.ep_number[self.ep_count].eq(ep_addr[:4]),
                                            self.ep_max_packet[self.ep_count].eq(ep_mps[:7]),
                                            self.ep_interval[self.ep_count].eq(control.data),
                                            self.ep_report_length[self.ep_count].eq(
                                                cur_report_length
                                            ),
                                            self.ep_count.eq(self.ep_count + 1),
                                        ]
                            m.d.usb += descriptor_position.eq(0)
                        with m.Else():
                            m.d.usb += descriptor_position.eq(descriptor_position + 1)

                check_detach_or_control_done()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Elif(
                        (control.transferred > declared_length) | (byte_count > declared_length)
                    ):
                        fail(HostError.OVERSIZED_DESCRIPTOR)
                    with m.Elif(
                        (control.transferred != declared_length)
                        | (byte_count != declared_length)
                        | (descriptor_position != 0)
                        | malformed
                    ):
                        fail(HostError.MALFORMED_DESCRIPTOR)
                    with m.Elif(oversized):
                        fail(HostError.OVERSIZED_DESCRIPTOR)
                    with m.Elif(
                        unsupported
                        | (interface_count != declared_interfaces)
                        | ~mouse_seen
                        | (self.ep_count == 0)
                    ):
                        fail(HostError.UNSUPPORTED_TOPOLOGY)
                    with m.Else():
                        m.d.comb += store.capture_commit.eq(1)
                        with m.If(
                            (manufacturer_index != 0) | (product_index != 0) | (serial_index != 0)
                        ):
                            m.next = "LANGUAGE_PREP"
                        with m.Else():
                            m.next = "SET_CONFIGURATION_START"

            with m.State("LANGUAGE_PREP"):
                begin_capture(3, 0, 0)
                m.d.usb += [language_low.eq(0), language_id.eq(0)]
                m.next = "LANGUAGE_START"

            with m.State("LANGUAGE_START"):
                start_request(
                    request_type=0x80,
                    request=GET_DESCRIPTOR,
                    value=0x0300,
                    index=0,
                    length=MAX_STRING_SIZE,
                )
                m.next = "LANGUAGE_WAIT"

            with m.State("LANGUAGE_WAIT"):
                connect_stream_to_store()
                with m.If(stream_accepted):
                    m.d.usb += byte_count.eq(byte_count + 1)
                    with m.Switch(byte_count):
                        with m.Case(0):
                            m.d.usb += header_length.eq(control.data)
                        with m.Case(1):
                            m.d.usb += header_type.eq(control.data)
                        with m.Case(2):
                            m.d.usb += language_low.eq(control.data)
                        with m.Case(3):
                            m.d.usb += language_id.eq(language_low | (control.data << 8))
                check_detach_or_control_done()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Elif(
                        (control.transferred > MAX_STRING_SIZE) | (byte_count > MAX_STRING_SIZE)
                    ):
                        fail(HostError.OVERSIZED_DESCRIPTOR)
                    with m.Elif(
                        (control.transferred != byte_count)
                        | (byte_count < 4)
                        | byte_count[0]
                        | (header_length != byte_count)
                        | (header_type != 3)
                    ):
                        fail(HostError.MALFORMED_DESCRIPTOR)
                    with m.Else():
                        m.d.comb += store.capture_commit.eq(1)
                        m.d.usb += string_cursor.eq(0)
                        m.next = "STRING_SELECT"

            with m.State("STRING_SELECT"):
                with m.If(string_cursor == 3):
                    m.next = "SET_CONFIGURATION_START"
                with m.Elif(
                    (string_indexes[string_cursor] == 0)
                    | ((string_cursor >= 1) & (string_indexes[string_cursor] == manufacturer_index))
                    | ((string_cursor == 2) & (string_indexes[string_cursor] == product_index))
                ):
                    m.d.usb += string_cursor.eq(string_cursor + 1)
                with m.Else():
                    m.d.usb += current_string_index.eq(string_indexes[string_cursor])
                    m.next = "STRING_PREP"

            with m.State("STRING_PREP"):
                begin_capture(3, current_string_index, language_id)
                m.next = "STRING_START"

            with m.State("STRING_START"):
                start_request(
                    request_type=0x80,
                    request=GET_DESCRIPTOR,
                    value=0x0300 | current_string_index,
                    index=language_id,
                    length=MAX_STRING_SIZE,
                )
                m.next = "STRING_WAIT"

            with m.State("STRING_WAIT"):
                connect_stream_to_store()
                with m.If(stream_accepted):
                    m.d.usb += byte_count.eq(byte_count + 1)
                    with m.Switch(byte_count):
                        with m.Case(0):
                            m.d.usb += header_length.eq(control.data)
                        with m.Case(1):
                            m.d.usb += header_type.eq(control.data)
                check_detach_or_control_done()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Elif(
                        (control.transferred > MAX_STRING_SIZE) | (byte_count > MAX_STRING_SIZE)
                    ):
                        fail(HostError.OVERSIZED_DESCRIPTOR)
                    with m.Elif(
                        (control.transferred != byte_count)
                        | (byte_count < 2)
                        | byte_count[0]
                        | (header_length != byte_count)
                        | (header_type != 3)
                    ):
                        fail(HostError.MALFORMED_DESCRIPTOR)
                    with m.Else():
                        m.d.comb += store.capture_commit.eq(1)
                        m.d.usb += string_cursor.eq(string_cursor + 1)
                        m.next = "STRING_SELECT"

            with m.State("SET_CONFIGURATION_START"):
                start_request(
                    request_type=0,
                    request=SET_CONFIGURATION,
                    value=self.configuration_value,
                    index=0,
                    length=0,
                )
                m.next = "SET_CONFIGURATION_WAIT"

            with m.State("SET_CONFIGURATION_WAIT"):
                with m.If(detached):
                    go_detached()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Else():
                        m.d.usb += [
                            report_cursor.eq(0),
                            report_fetched.eq(0),
                        ]
                        m.next = "REPORT_SELECT"

            with m.State("REPORT_SELECT"):
                with m.If(detached):
                    go_detached()
                with m.Elif(report_cursor == self.ep_count):
                    m.d.usb += [
                        self.ready.eq(1),
                        self.enumerating.eq(0),
                        self.error_code.eq(HostError.NONE.value),
                    ]
                    m.next = "READY"
                with m.Elif(
                    report_fetched & (self.ep_interface[report_cursor] == report_fetched_interface)
                ):
                    # This interface's report descriptor was already fetched for
                    # an earlier endpoint; skip the redundant GET.
                    m.d.usb += report_cursor.eq(report_cursor + 1)
                with m.Else():
                    m.next = "REPORT_PREP"

            with m.State("REPORT_PREP"):
                begin_capture(0x22, 0, self.ep_interface[report_cursor])
                m.d.usb += [
                    report_fetched.eq(1),
                    report_fetched_interface.eq(self.ep_interface[report_cursor]),
                ]
                m.next = "REPORT_START"

            with m.State("REPORT_START"):
                start_request(
                    request_type=0x81,
                    request=GET_DESCRIPTOR,
                    value=0x2200,
                    index=self.ep_interface[report_cursor],
                    length=self.ep_report_length[report_cursor],
                )
                m.next = "REPORT_WAIT"

            with m.State("REPORT_WAIT"):
                connect_stream_to_store()
                with m.If(stream_accepted):
                    m.d.usb += byte_count.eq(byte_count + 1)
                check_detach_or_control_done()
                with m.Elif(control.done):
                    with m.If(control.status != TransactionStatus.SUCCESS.value):
                        handle_control_failure()
                    with m.Elif(
                        (control.transferred > self.ep_report_length[report_cursor])
                        | (byte_count > self.ep_report_length[report_cursor])
                    ):
                        fail(HostError.OVERSIZED_DESCRIPTOR)
                    with m.Elif(
                        (control.transferred != self.ep_report_length[report_cursor])
                        | (byte_count != self.ep_report_length[report_cursor])
                    ):
                        fail(HostError.MALFORMED_DESCRIPTOR)
                    with m.Else():
                        m.d.comb += store.capture_commit.eq(1)
                        m.d.usb += report_cursor.eq(report_cursor + 1)
                        m.next = "REPORT_SELECT"

            with m.State("READY"):
                with m.If(~self.enable | detached):
                    go_detached()

            with m.State("ERROR"):
                with m.If(~self.enable):
                    clear_session()
                    m.d.comb += store.clear.eq(1)
                    m.next = "POWER_OFF"
                with m.Elif(detached):
                    go_detached()

        return m
