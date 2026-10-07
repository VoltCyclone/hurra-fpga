"""Integrated bounded USB HID host: enumeration, polling and report merging."""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from amaranth import Array, Cat, DomainRenamer, Elaboratable, Module, Mux, Signal
from luna.gateware.interface.utmi import UTMIInterface

from .boot_protocol import BootProtocolTracker
from .control import USBControlTransferEngine
from .control_relay import ControlRelay
from .descriptors import MAX_ENDPOINT_NUMBER, MAX_ENDPOINTS, DescriptorStore
from .enumerator import BoundedMouseEnumerator
from .out_writer import InterruptOutWriter
from .poller import InterruptInPoller
from .scheduler import FrameScheduler
from .timing import HostTiming
from .transaction import USBHostTransactionEngine, USBHostTransactionPort
from .types import HostError, TransactionStatus

_DEFAULT_TIMING = HostTiming.hardware()


class USBHostTransactionArbiter(Elaboratable):
    """Arbitrate enumeration, SOF, and N interrupt pollers onto one engine.

    Enumeration (control phase) and SOF keep strict priority. Interrupt
    pollers share the engine round-robin through a rotating pointer: only
    the pointed poller may start, and only the poller that started the
    in-flight transaction is routed its ``done``/``status``/``rx_*`` result.
    ``poller_busy`` (the OR of every poller's ``active``) blocks new poller
    grants while any poller still owns the shared receive buffer.

    A "poller" here is any port with that contract; the host puts its
    interrupt-OUT writer on the last one. Its ``active`` must be in
    ``poller_busy`` too: the engine reads ``tx_payload`` live through
    SEND_DATA, and ``control_owns`` switches that mux combinationally.
    """

    def __init__(self, *, engine, control_port, poller_ports) -> None:
        self.engine = engine
        self.control_port = control_port
        self.poller_ports = list(poller_ports)
        self.control_phase = Signal()
        self.connected = Signal()
        self.sof_start = Signal()
        self.sof_frame = Signal(11)
        self.sof_ready = Signal()
        self.poller_busy = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        engine = self.engine
        control = self.control_port
        ports = self.poller_ports
        count = len(ports)
        last = count - 1

        rotate = Signal(range(count))
        poller_owner = Signal(range(count))
        owner_valid = Signal()
        engine_done_d = Signal()
        m.d.usb += engine_done_d.eq(engine.done)

        # Who owns the engine AND its shared receive buffer. A poller holds
        # both from its grant until it has copied its report out
        # (poller_busy), so control may not take over until then -- even with
        # control_phase high. That could never happen while control only ran
        # during enumeration; the control relay forwards transfers AFTER it,
        # while pollers are live, and a control grant mid-copy overwrote the
        # buffer the poller was still reading.
        control_owns = self.control_phase & ~self.poller_busy
        control_grant = control_owns & control.start & ~engine.busy
        sof_grant = self.sof_start & ~engine.busy & ~engine.done & ~engine_done_d & ~control_grant
        poll_bus_free = ~self.control_phase & ~self.sof_start & ~engine.busy & ~self.poller_busy

        selected_start = Array([p.start for p in ports])[rotate]
        poller_grant = selected_start & poll_bus_free

        # The granted poller supplies engine inputs on the start cycle; the
        # owning poller supplies rx_read_index while it drains the shared
        # receive buffer (poller_busy high through poll+copy).
        ep_sel = Mux(self.poller_busy, poller_owner, rotate)
        token_pid = Array([p.token_pid for p in ports])[ep_sel]
        address = Array([p.address for p in ports])[ep_sel]
        endpoint = Array([p.endpoint for p in ports])[ep_sel]
        data_toggle = Array([p.data_toggle for p in ports])[ep_sel]
        tx_length = Array([p.tx_length for p in ports])[ep_sel]
        tx_payload = Array([p.tx_payload for p in ports])[ep_sel]
        rx_read_index = Array([p.rx_read_index for p in ports])[ep_sel]

        m.d.comb += [
            self.sof_ready.eq(sof_grant),
            control.start_ready.eq(control_owns & ~engine.busy),
            engine.start.eq(control_grant | sof_grant | poller_grant),
            engine.sof.eq(sof_grant),
            engine.frame.eq(self.sof_frame),
            engine.token_pid.eq(Mux(control_owns, control.token_pid, token_pid)),
            engine.address.eq(Mux(control_owns, control.address, address)),
            engine.endpoint.eq(Mux(control_owns, control.endpoint, endpoint)),
            engine.data_toggle.eq(Mux(control_owns, control.data_toggle, data_toggle)),
            engine.tx_length.eq(Mux(control_owns, control.tx_length, tx_length)),
            engine.tx_payload.eq(Mux(control_owns, control.tx_payload, tx_payload)),
            engine.connected.eq(self.connected),
            engine.rx_read_index.eq(Mux(control_owns, control.rx_read_index, rx_read_index)),
        ]

        # Result routing stays unconditional: the control engine only acts on
        # done/rx_* while one of its own transactions is in flight, and
        # control_owns keeps that from overlapping a poller's. (Control used
        # to run only during enumeration; the relay now runs it afterwards
        # too, which is why ownership above is no longer just control_phase.)
        m.d.comb += [
            control.busy.eq(engine.busy),
            control.done.eq(engine.done),
            control.status.eq(engine.status),
            control.tx_index.eq(engine.tx_index),
            control.rx_length.eq(engine.rx_length),
            control.rx_read_data.eq(engine.rx_read_data),
            control.rx_data_toggle.eq(engine.rx_data_toggle),
            control.duplicate.eq(engine.duplicate),
        ]

        for k, port in enumerate(ports):
            owns = owner_valid & (poller_owner == k)
            m.d.comb += [
                port.start_ready.eq(poll_bus_free & (rotate == k)),
                port.busy.eq(engine.busy | self.poller_busy | (rotate != k)),
                port.done.eq(engine.done & owns),
                port.status.eq(engine.status),
                port.tx_index.eq(engine.tx_index),
                port.rx_length.eq(engine.rx_length),
                port.rx_read_data.eq(engine.rx_read_data),
                port.rx_data_toggle.eq(engine.rx_data_toggle),
                port.duplicate.eq(engine.duplicate),
            ]

        with m.If(poller_grant):
            m.d.usb += [poller_owner.eq(rotate), owner_valid.eq(1)]
        with m.Elif(owner_valid & engine.done):
            m.d.usb += [
                owner_valid.eq(0),
                rotate.eq(Mux(poller_owner == last, 0, poller_owner + 1)),
            ]
        with m.Elif(poll_bus_free & ~selected_start):
            m.d.usb += rotate.eq(Mux(rotate == last, 0, rotate + 1))

        return m


class ReportMergeMux(Elaboratable):
    """Round-robin merge of N poller report streams into one tagged stream.

    Locks onto one source at a time, streams its whole report, tags it with
    the source interface/endpoint, then advances to the next source. Only the
    locked source is offered ``report_ready``.
    """

    def __init__(self, sources, interfaces, endpoints) -> None:
        self.sources = list(sources)
        self.interfaces = interfaces
        self.endpoints = endpoints

        self.report_valid = Signal()
        self.report_ready = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()
        self.report_interface = Signal(8)
        self.report_endpoint = Signal(4)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        sources = self.sources
        count = len(sources)
        last = count - 1

        sel = Signal(range(count))
        locked = Signal()

        valid = Array([s.report_valid for s in sources])
        data = Array([s.report_data for s in sources])
        first = Array([s.report_first for s in sources])
        last_flag = Array([s.report_last for s in sources])
        interfaces = Array(self.interfaces)
        endpoints = Array(self.endpoints)

        m.d.comb += [
            self.report_valid.eq(locked & valid[sel]),
            self.report_data.eq(data[sel]),
            self.report_first.eq(locked & first[sel]),
            self.report_last.eq(locked & last_flag[sel]),
            self.report_interface.eq(interfaces[sel]),
            self.report_endpoint.eq(endpoints[sel]),
        ]
        for k, source in enumerate(sources):
            m.d.comb += source.report_ready.eq(self.report_ready & locked & (sel == k))

        with m.If(~locked):
            with m.Switch(sel):
                for start in range(count):
                    with m.Case(start):
                        order = [(start + offset) % count for offset in range(count)]
                        for position, k in enumerate(order):
                            block = m.If if position == 0 else m.Elif
                            with block(sources[k].report_valid):
                                m.d.usb += [sel.eq(k), locked.eq(1)]
        with m.Else():
            with m.If(~valid[sel]):
                # The locked source dropped its report without a last byte: its
                # buffer was cleared by a disconnect or a polling failure. Release
                # the lock so the pipeline cannot wedge (during normal streaming
                # the poller holds buffer_full through the last accepted byte, so
                # this only fires on the abnormal drop).
                m.d.usb += locked.eq(0)
            with m.Elif(self.report_valid & self.report_ready & self.report_last):
                m.d.usb += [locked.eq(0), sel.eq(Mux(sel == last, 0, sel + 1))]

        return m


class BoundedMouseHost(Elaboratable):
    """Integrate enumeration, SOF scheduling, and interrupt polling."""

    def __init__(self, utmi=None, timing: HostTiming = _DEFAULT_TIMING) -> None:
        self.utmi = utmi if utmi is not None else UTMIInterface()
        self.timing = timing

        self.transaction = USBHostTransactionEngine(utmi=self.utmi, timing=timing)
        self.control_port = USBHostTransactionPort()
        self.control = USBControlTransferEngine(
            transaction=self.control_port,
            timing=timing,
            add_transaction_submodule=False,
        )
        self.descriptor_store = DescriptorStore()
        self.enumerator = BoundedMouseEnumerator(
            timing=timing,
            control=self.control,
            descriptor_store=self.descriptor_store,
        )
        self.control_relay = ControlRelay()
        self.boot_protocol_tracker = BootProtocolTracker()
        self.scheduler = FrameScheduler(timing)

        # One interrupt poller per capturable endpoint, plus the interrupt-OUT
        # writer on the last port, all sharing the single transaction engine
        # through the arbiter. The arbiter is generic in its port count, so the
        # writer is simply one more round-robin participant.
        self.poller_ports = [USBHostTransactionPort() for _ in range(MAX_ENDPOINTS + 1)]
        self.pollers = [
            InterruptInPoller(
                transaction=port,
                timing=timing,
                add_transaction_submodule=False,
            )
            for port in self.poller_ports[:MAX_ENDPOINTS]
        ]
        self.out_writer = InterruptOutWriter(
            transaction=self.poller_ports[MAX_ENDPOINTS],
            timing=timing,
            add_transaction_submodule=False,
        )
        self.arbiter = USBHostTransactionArbiter(
            engine=self.transaction,
            control_port=self.control_port,
            poller_ports=self.poller_ports,
        )
        self.merge = ReportMergeMux(
            self.pollers,
            self.enumerator.ep_interface,
            self.enumerator.ep_number,
        )

        self.aux_vbus_en = self.enumerator.aux_vbus_en
        self.target_discharge = self.enumerator.target_discharge
        self.connected = self.enumerator.connected
        #: Negotiated TARGET link speed. Re-exported so the top level can mirror
        #: it onto the AUX device, keeping the relay transparent.
        self.high_speed = self.enumerator.high_speed
        #: Runtime override forcing the TARGET link to stay Full Speed.
        self.force_full_speed = self.enumerator.force_full_speed
        self.enumerating = self.enumerator.enumerating
        self.enumerated = Signal()
        self.error_code = Signal(4)
        self.polling_active = Signal()
        self.polling_failed = Signal()
        self.report_activity = Signal()

        # Poll-cadence instrumentation. All four wrap mod 2**32 rather than
        # saturating: they are read as deltas over a window, so a wrap costs
        # nothing and a 32-bit comparator on the critical path would.
        #
        # These exist because the rate a report *arrives* at cannot tell you
        # whether the host polled slowly or the device answered emptily --
        # native_reports counts reports received, and a NAK looks identical to
        # a poll that was never issued. Reading sof_ticks and polls_issued
        # against each other separates the two in one window.
        #: Live PHY disconnect report, and a sticky record that it was ever
        #: seen. The sticky bit is the useful one: on hardware the host failed
        #: to notice a High Speed unplug at all, and a live read cannot tell
        #: "never asserted" from "asserted and already gone".
        self.host_disconnect = Signal()
        self.host_disconnect_seen = Signal()
        self.device_unresponsive = Signal()
        self.polls_issued = Signal(32)
        self.poll_naks = Signal(32)

        # Merged interface-tagged report stream.
        self.report_valid = Signal()
        self.report_ready = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()
        self.report_interface = Signal(8)
        self.report_endpoint = Signal(4)

        # PC -> real device: the clone's interrupt-OUT endpoint stream, sunk
        # into the writer. ``out_ready`` stays low while a packet is held, so
        # the clone's FIFO fills and the PC is NAKed. ``out_flush`` pulses when
        # the clone resets that FIFO.
        self.out_flush = Signal()
        self.out_valid = Signal()
        self.out_data = Signal(8)
        self.out_last = Signal()
        self.out_ready = Signal()
        #: The captured interrupt-OUT endpoint, for the clone to serve.
        self.out_present = Signal()
        self.out_number = Signal(4)

        #: Registered: a PC bus reset or SET_CONFIGURATION on the clone. The
        #: real device never sees either, so the tracker replays
        #: SET_PROTOCOL(report) to any interface the PC had left in boot.
        self.boot_resync_trigger = Signal()
        #: Registered. Bit n: endpoint number n's interface is in boot
        #: protocol on the real device, so its reports are boot-layout.
        self.boot_protocol = Signal(MAX_ENDPOINT_NUMBER + 1)

        # Read-only mirror of the captured endpoint table.
        self.ep_count = Signal(range(MAX_ENDPOINTS + 1))
        self.ep_interface = Array(
            [Signal(8, name=f"host_ep_interface_{k}") for k in range(MAX_ENDPOINTS)]
        )
        self.ep_number = Array(
            [Signal(4, name=f"host_ep_number_{k}") for k in range(MAX_ENDPOINTS)]
        )
        self.ep_max_packet = Array(
            [Signal(7, name=f"host_ep_max_packet_{k}") for k in range(MAX_ENDPOINTS)]
        )
        self.ep_interval = Array(
            [Signal(8, name=f"host_ep_interval_{k}") for k in range(MAX_ENDPOINTS)]
        )

        self.lookup_type = self.descriptor_store.lookup_type
        self.lookup_index = self.descriptor_store.lookup_index
        self.lookup_w_index = self.descriptor_store.lookup_w_index
        self.lookup_offset = self.descriptor_store.lookup_offset
        self.lookup_request = self.descriptor_store.lookup_request
        self.lookup_ready = self.descriptor_store.lookup_ready
        self.lookup_response = self.descriptor_store.lookup_response
        self.lookup_found = self.descriptor_store.lookup_found
        self.lookup_length = self.descriptor_store.lookup_length
        self.lookup_data = self.descriptor_store.lookup_data

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.transaction_engine = self.transaction
        m.submodules.enumerator = self.enumerator
        m.submodules.scheduler = DomainRenamer({"sync": "usb"})(self.scheduler)
        m.submodules.arbiter = self.arbiter
        m.submodules.control_relay = relay = self.control_relay
        m.submodules.boot_protocol = tracker = self.boot_protocol_tracker
        m.submodules.report_merge = self.merge
        for k, poller in enumerate(self.pollers):
            m.submodules[f"poller_{k}"] = poller
        m.submodules.out_writer = writer = self.out_writer

        enumerator = self.enumerator
        # "The PHY is configured for normal operation at the negotiated speed",
        # not "the PHY is configured for Full Speed". Both speeds use op_mode
        # NORMAL and differ only in transceiver and termination select: 1/1 at
        # Full Speed, 0/0 at High Speed. The reset window and chirp drive are
        # excluded by op_mode alone, which is 2 there.
        full_speed_config = ~enumerator.high_speed
        normal_operation = (
            enumerator.connected
            & (enumerator.phy_op_mode == 0)
            & (enumerator.phy_xcvr_select == full_speed_config)
            & (enumerator.phy_term_select == full_speed_config)
        )

        polling_active = Cat(*[p.active for p in self.pollers]).any()
        polling_failed = Cat(*[p.failed for p in self.pollers]).any()
        # A poller that spent its whole transport-error budget on timeouts had
        # three polls in a row answered by nothing. That is a gone device, and
        # unlike the PHY's host_disconnect it is evidence the host gathered
        # itself. STALL and the other terminal statuses are deliberately not
        # included: that device is present and answering.
        # Registered, not combinational. This feeds the enumerator's
        # ``disconnect_seen``, and ``detached`` derived from it is tested in
        # every FSM state -- so anything added to it combinationally lands on a
        # wide fan-out path. One cycle of latency is free here: the detach is
        # debounced for detach_stable_cycles (150 cycles) downstream anyway,
        # and ``failed`` is a latch rather than a pulse, so nothing is missed.
        device_unresponsive = Signal()
        m.d.usb += device_unresponsive.eq(
            Cat(
                *[
                    p.failed & (p.last_status == TransactionStatus.TIMEOUT.value)
                    for p in self.pollers
                ]
            ).any()
        )

        m.d.comb += [
            enumerator.enable.eq(1),
            enumerator.line_state.eq(self.utmi.line_state),
            # High Speed disconnect cannot be read off line_state, because SE0
            # is the idle bus there. The PHY measures it during a SOF EOP.
            enumerator.host_disconnect.eq(self.utmi.host_disconnect),
            enumerator.device_unresponsive.eq(device_unresponsive),
            # The chirp is line signalling, not a packet, so it reaches the
            # transmitter through the transaction engine, which owns utmi.tx.
            self.transaction.chirp_valid.eq(enumerator.chirp_tx_valid),
            self.transaction.chirp_data.eq(enumerator.chirp_tx_data),
            # The enumerator gates presence on its own commanded TARGET-A power,
            # not on the target PHY's vbus_valid (which senses TARGET-C only).
            self.utmi.xcvr_select.eq(enumerator.phy_xcvr_select),
            self.utmi.term_select.eq(enumerator.phy_term_select),
            self.utmi.op_mode.eq(enumerator.phy_op_mode),
            self.utmi.dp_pulldown.eq(enumerator.dp_pulldown),
            self.utmi.dm_pulldown.eq(enumerator.dm_pulldown),
            self.utmi.suspend.eq(0),
            self.utmi.id_pullup.eq(0),
            self.utmi.chrg_vbus.eq(0),
            self.utmi.dischrg_vbus.eq(0),
            self.utmi.use_external_vbus_indicator.eq(0),
            self.scheduler.enable.eq(normal_operation & enumerator.traffic_enable),
            self.scheduler.high_speed.eq(enumerator.high_speed),
            self.transaction.high_speed.eq(enumerator.high_speed),
            self.scheduler.token_ready.eq(self.arbiter.sof_ready),
            # The relay pre-empts the pollers only while the control engine is
            # actually working for it -- not while it waits on the AUX host's
            # own data and status stages, or a slow or absent AUX host could
            # silence the controller. A report gap of a few frames per forward
            # is ordinary USB; starving the auth handshake is not.
            self.arbiter.control_phase.eq(~enumerator.ready | relay.engine_owned),
            # The engine is the enumerator's until TARGET is enumerated.
            relay.enable.eq(enumerator.ready),
            # Relay -> engine, by way of the enumerator, which is the only
            # module permitted to drive control.* (see its port comment).
            enumerator.relay_start.eq(relay.ctl_start),
            enumerator.relay_request_type.eq(relay.ctl_request_type),
            enumerator.relay_request.eq(relay.ctl_request),
            enumerator.relay_value.eq(relay.ctl_value),
            enumerator.relay_index.eq(relay.ctl_index),
            enumerator.relay_length.eq(relay.ctl_length),
            enumerator.relay_out_payload.eq(relay.ctl_out_payload),
            enumerator.relay_data_ready.eq(relay.ctl_data_ready),
            # Engine -> relay.
            relay.ctl_busy.eq(self.control.busy),
            relay.ctl_done.eq(self.control.done),
            relay.ctl_status.eq(self.control.status),
            relay.ctl_transferred.eq(self.control.transferred),
            relay.ctl_data.eq(self.control.data),
            relay.ctl_data_valid.eq(self.control.data_valid),
            relay.ctl_data_first.eq(self.control.data_first),
            relay.ctl_data_last.eq(self.control.data_last),
            relay.ctl_out_index.eq(self.control.out_index),
            # Boot protocol: the tracker snoops the PC's completed forwards and
            # offers its replays as the relay's lower-priority second source.
            tracker.ready.eq(enumerator.ready),
            tracker.ep_count.eq(enumerator.ep_count),
            tracker.forward_ok.eq(relay.forward_ok),
            tracker.forward_type.eq(relay.ctl_request_type),
            tracker.forward_request.eq(relay.ctl_request),
            tracker.forward_value.eq(relay.ctl_value),
            tracker.forward_index.eq(relay.ctl_index),
            tracker.resync_trigger.eq(self.boot_resync_trigger),
            relay.resync_valid.eq(tracker.resync_valid),
            relay.resync_index.eq(tracker.resync_index),
            tracker.relay_resync_active.eq(relay.resync_active),
            tracker.relay_resync_done.eq(relay.resync_done),
            tracker.relay_error.eq(relay.response_error),
            self.boot_protocol.eq(tracker.boot_mask),
            self.arbiter.connected.eq(enumerator.connected),
            self.arbiter.sof_start.eq(self.scheduler.sof_start),
            self.arbiter.sof_frame.eq(self.scheduler.frame_number),
            # The writer holds the engine through its whole OUT exactly as a
            # poller holds it through poll+copy. Without it, a control relay
            # start mid-OUT would flip control_owns -- and with it the
            # engine's tx_payload/tx_length -- while SEND_DATA is reading
            # them. polling_active itself stays pollers-only.
            self.arbiter.poller_busy.eq(polling_active | writer.active),
            self.polling_active.eq(polling_active),
            self.polling_failed.eq(polling_failed),
            self.enumerated.eq(enumerator.ready & ~polling_failed),
            self.error_code.eq(
                Mux(
                    enumerator.error_code != HostError.NONE.value,
                    enumerator.error_code,
                    Mux(
                        polling_failed,
                        HostError.POLLING_FAILURE.value,
                        HostError.NONE.value,
                    ),
                )
            ),
        ]

        for k, poller in enumerate(self.pollers):
            m.d.comb += [
                poller.enable.eq(
                    enumerator.ready & enumerator.connected & (k < enumerator.ep_count)
                ),
                poller.connected.eq(enumerator.connected),
                poller.sof_tick.eq(self.scheduler.frame_tick),
                poller.address.eq(enumerator.device_address),
                poller.endpoint.eq(enumerator.ep_number[k]),
                poller.max_packet_size.eq(enumerator.ep_max_packet[k]),
                poller.interval.eq(enumerator.ep_interval[k]),
                poller.high_speed.eq(enumerator.high_speed),
            ]

        # The writer feeds none of polling_failed, device_unresponsive,
        # enumerated or error_code: a STALLed rumble or LED endpoint must
        # never be able to stop the input path. Its enable is registered,
        # which costs one cycle on a level that changes once per session and
        # keeps the enumerator's state off the writer's FSM.
        out_enable = Signal()
        m.d.usb += out_enable.eq(enumerator.ready & enumerator.connected & enumerator.out_present)
        m.d.comb += [
            writer.enable.eq(out_enable),
            writer.connected.eq(enumerator.connected),
            writer.sof_tick.eq(self.scheduler.frame_tick),
            writer.address.eq(enumerator.device_address),
            writer.endpoint.eq(enumerator.out_number),
            writer.max_packet_size.eq(enumerator.out_max_packet),
            writer.interval.eq(enumerator.out_interval),
            writer.high_speed.eq(enumerator.high_speed),
            writer.flush.eq(self.out_flush),
            writer.out_valid.eq(self.out_valid),
            writer.out_data.eq(self.out_data),
            writer.out_last.eq(self.out_last),
            self.out_ready.eq(writer.out_ready),
            self.out_present.eq(enumerator.out_present),
            self.out_number.eq(enumerator.out_number),
        ]

        m.d.comb += [
            self.merge.report_ready.eq(self.report_ready),
            self.report_valid.eq(self.merge.report_valid),
            self.report_data.eq(self.merge.report_data),
            self.report_first.eq(self.merge.report_first),
            self.report_last.eq(self.merge.report_last),
            self.report_interface.eq(self.merge.report_interface),
            self.report_endpoint.eq(self.merge.report_endpoint),
            self.ep_count.eq(enumerator.ep_count),
        ]
        for k in range(MAX_ENDPOINTS):
            m.d.comb += [
                self.ep_interface[k].eq(enumerator.ep_interface[k]),
                self.ep_number[k].eq(enumerator.ep_number[k]),
                self.ep_max_packet[k].eq(enumerator.ep_max_packet[k]),
                self.ep_interval[k].eq(enumerator.ep_interval[k]),
                tracker.ep_interface[k].eq(enumerator.ep_interface[k]),
                tracker.ep_number[k].eq(enumerator.ep_number[k]),
            ]

        # Once the enumerator is ready, an engine start that is neither a SOF
        # nor the writer's OUT is a poll. The writer's start is only ever
        # raised under its own arbiter grant (it is gated on start_ready), so
        # it marks exactly the OUT grants. (The control relay's transfers are
        # still counted here, as they were before the writer existed.)
        poll_start = (
            self.transaction.start
            & ~self.transaction.sof
            & ~writer.transaction.start
            & enumerator.ready
        )
        poll_in_flight = Signal()
        with m.If(poll_start):
            m.d.usb += poll_in_flight.eq(1)
        with m.Elif(self.transaction.done):
            m.d.usb += poll_in_flight.eq(0)

        m.d.comb += [
            self.host_disconnect.eq(self.utmi.host_disconnect),
            self.device_unresponsive.eq(device_unresponsive),
        ]
        with m.If(self.utmi.host_disconnect):
            m.d.usb += self.host_disconnect_seen.eq(1)

        poll_done = self.transaction.done & poll_in_flight
        m.d.usb += [
            self.polls_issued.eq(self.polls_issued + poll_start),
            self.poll_naks.eq(
                self.poll_naks
                + (poll_done & (self.transaction.status == TransactionStatus.NAK.value))
            ),
        ]

        with m.If(~enumerator.connected):
            m.d.usb += self.report_activity.eq(0)
        with m.Elif(self.report_valid & self.report_ready & self.report_last):
            # A toggle is visible on an LED even when individual reports are brief.
            m.d.usb += self.report_activity.eq(~self.report_activity)

        return m
