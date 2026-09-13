"""Integrated bounded full-speed USB mouse host."""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from amaranth import Array, Cat, DomainRenamer, Elaboratable, Module, Mux, Signal
from luna.gateware.interface.utmi import UTMIInterface

from .control import USBControlTransferEngine
from .descriptors import MAX_ENDPOINTS, DescriptorStore
from .enumerator import BoundedMouseEnumerator
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

        control_grant = self.control_phase & control.start & ~engine.busy
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
            control.start_ready.eq(self.control_phase & ~engine.busy),
            engine.start.eq(control_grant | sof_grant | poller_grant),
            engine.sof.eq(sof_grant),
            engine.frame.eq(self.sof_frame),
            engine.token_pid.eq(Mux(self.control_phase, control.token_pid, token_pid)),
            engine.address.eq(Mux(self.control_phase, control.address, address)),
            engine.endpoint.eq(Mux(self.control_phase, control.endpoint, endpoint)),
            engine.data_toggle.eq(Mux(self.control_phase, control.data_toggle, data_toggle)),
            engine.tx_length.eq(Mux(self.control_phase, control.tx_length, tx_length)),
            engine.tx_payload.eq(Mux(self.control_phase, control.tx_payload, tx_payload)),
            engine.connected.eq(self.connected),
            engine.rx_read_index.eq(Mux(self.control_phase, control.rx_read_index, rx_read_index)),
        ]

        # Control transfers only run during enumeration, never concurrently
        # with polling, so its result routing stays unconditional.
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
        self.scheduler = FrameScheduler(timing)

        # One interrupt poller per capturable endpoint, all sharing the single
        # transaction engine through the arbiter.
        self.poller_ports = [USBHostTransactionPort() for _ in range(MAX_ENDPOINTS)]
        self.pollers = [
            InterruptInPoller(
                transaction=port,
                timing=timing,
                add_transaction_submodule=False,
            )
            for port in self.poller_ports
        ]
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
        m.submodules.report_merge = self.merge
        for k, poller in enumerate(self.pollers):
            m.submodules[f"poller_{k}"] = poller

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
            self.arbiter.control_phase.eq(~enumerator.ready),
            self.arbiter.connected.eq(enumerator.connected),
            self.arbiter.sof_start.eq(self.scheduler.sof_start),
            self.arbiter.sof_frame.eq(self.scheduler.frame_number),
            self.arbiter.poller_busy.eq(polling_active),
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
            ]

        # Control transfers only run while ``control_phase`` is high (that is,
        # before the enumerator is ready), so once polling starts an engine
        # start that is not a SOF is a poll and nothing else.
        poll_start = self.transaction.start & ~self.transaction.sof & enumerator.ready
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
