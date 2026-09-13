"""JTAG-readable diagnostic image for PC-facing AUX clone enumeration."""

from amaranth import Elaboratable, Module, Mux, Signal
from luna.gateware.interface.ulpi import UTMITranslator

from .debug_block import DebugRegisterBlock
from .debug_regs import aux_clone_register_map
from .device import MouseCloneDevice
from .host import BoundedMouseHost


class AuxCloneDebugState(Elaboratable):
    """Retains short AUX USB milestones as sticky bits, counters, and last values."""

    def __init__(self) -> None:
        self.effective_connect = Signal()
        self.rx_active = Signal()
        self.rx_valid = Signal()
        self.rx_error = Signal()
        self.tx_valid = Signal()
        self.reset_detected = Signal()
        self.setup_received = Signal()
        self.address_changed = Signal()
        self.config_changed = Signal()
        self.control_tx_valid = Signal()
        self.control_stall = Signal()

        self.setup_request_type = Signal(8)
        self.setup_request = Signal(8)
        self.setup_value = Signal(16)
        self.setup_index = Signal(16)
        self.setup_length = Signal(16)
        self.new_address = Signal(7)
        self.new_config = Signal(8)
        self.configured = Signal()

        self.connect_seen = Signal()
        self.rx_active_seen = Signal()
        self.rx_valid_seen = Signal()
        self.rx_error_seen = Signal()
        self.tx_valid_seen = Signal()
        self.reset_seen = Signal()
        self.setup_seen = Signal()
        self.address_seen = Signal()
        self.config_seen = Signal()
        self.control_tx_seen = Signal()
        self.control_stall_seen = Signal()

        self.reset_count = Signal(8)
        self.setup_count = Signal(8)
        self.address_count = Signal(8)
        self.config_count = Signal(8)

        self.last_request_type = Signal(8)
        self.last_request = Signal(8)
        self.last_value = Signal(16)
        self.last_index = Signal(16)
        self.last_length = Signal(16)
        self.last_address = Signal(7)
        self.last_config = Signal(8)

    @staticmethod
    def _retain_event(m: Module, event, seen: Signal, count: Signal | None = None) -> None:
        with m.If(event):
            m.d.usb += seen.eq(1)
            if count is not None:
                with m.If(count != 0xFF):
                    m.d.usb += count.eq(count + 1)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()

        self._retain_event(m, self.effective_connect, self.connect_seen)
        self._retain_event(m, self.rx_active, self.rx_active_seen)
        self._retain_event(m, self.rx_valid, self.rx_valid_seen)
        self._retain_event(m, self.rx_error, self.rx_error_seen)
        self._retain_event(m, self.tx_valid, self.tx_valid_seen)
        self._retain_event(m, self.reset_detected, self.reset_seen, self.reset_count)
        self._retain_event(m, self.setup_received, self.setup_seen, self.setup_count)
        self._retain_event(m, self.address_changed, self.address_seen, self.address_count)
        self._retain_event(m, self.config_changed, self.config_seen, self.config_count)
        self._retain_event(m, self.control_tx_valid, self.control_tx_seen)
        self._retain_event(m, self.control_stall, self.control_stall_seen)

        with m.If(self.setup_received):
            m.d.usb += [
                self.last_request_type.eq(self.setup_request_type),
                self.last_request.eq(self.setup_request),
                self.last_value.eq(self.setup_value),
                self.last_index.eq(self.setup_index),
                self.last_length.eq(self.setup_length),
            ]
        with m.If(self.address_changed):
            m.d.usb += self.last_address.eq(self.new_address)
        with m.If(self.config_changed):
            m.d.usb += self.last_config.eq(self.new_config)

        return m


class AuxCloneDiagnosticTop(Elaboratable):
    """Production clone data path plus passive AUX enumeration observability."""

    def elaborate(self, platform) -> Module:
        m = Module()
        m.submodules.clocking = platform.clock_domain_generator()

        target_phy = platform.request("target_phy")
        m.submodules.target_utmi = target_utmi = UTMITranslator(ulpi=target_phy)
        m.submodules.host = host = BoundedMouseHost(utmi=target_utmi)

        aux_phy = platform.request("aux_phy")
        m.submodules.aux_utmi = aux_utmi = UTMITranslator(ulpi=aux_phy)
        m.submodules.device = device = MouseCloneDevice(bus=aux_utmi, store=host.descriptor_store)
        m.submodules.state = state = AuxCloneDebugState()

        effective_connect = Signal()
        m.d.comb += [
            effective_connect.eq(host.enumerated & device.copy_done),
            device.copy_enable.eq(host.enumerated),
            device.connect.eq(effective_connect),
            device.report_valid.eq(host.report_valid),
            device.report_data.eq(host.report_data),
            device.report_first.eq(host.report_first),
            device.report_last.eq(host.report_last),
            device.report_endpoint.eq(host.report_endpoint),
            host.report_ready.eq(device.report_ready),
            state.effective_connect.eq(effective_connect),
            state.rx_active.eq(aux_utmi.rx_active),
            state.rx_valid.eq(aux_utmi.rx_valid),
            state.rx_error.eq(aux_utmi.rx_error),
            state.tx_valid.eq(aux_utmi.tx_valid),
            state.reset_detected.eq(device.debug_reset_detected),
            state.setup_received.eq(device.debug_setup_received),
            state.address_changed.eq(device.debug_address_changed),
            state.config_changed.eq(device.debug_config_changed),
            state.control_tx_valid.eq(device.debug_control_tx_valid),
            state.control_stall.eq(device.debug_control_stall),
            state.setup_request_type.eq(device.debug_setup_request_type),
            state.setup_request.eq(device.debug_setup_request),
            state.setup_value.eq(device.debug_setup_value),
            state.setup_index.eq(device.debug_setup_index),
            state.setup_length.eq(device.debug_setup_length),
            state.new_address.eq(device.debug_new_address),
            state.new_config.eq(device.debug_new_config),
            state.configured.eq(device.configured),
        ]

        regmap = aux_clone_register_map()
        m.submodules.dbg = dbg = DebugRegisterBlock(
            regmap=regmap,
            host=None,
            aux_vbus_en=host.aux_vbus_en,
            target_discharge=host.target_discharge,
            power_from_control=True,
        )
        dbg.status(
            "aux_prereq",
            host_connected=host.connected,
            host_enumerated=host.enumerated,
            host_error=host.error_code,
            copy_enable=device.copy_enable,
            copy_done=device.copy_done,
            copy_failed=device.copy_failed,
            aux_vbus_valid=aux_utmi.vbus_valid,
            effective_connect=effective_connect,
            configured=device.configured,
        )
        dbg.status(
            "aux_link_live",
            line_state=aux_utmi.line_state,
            rx_active=aux_utmi.rx_active,
            rx_valid=aux_utmi.rx_valid,
            rx_error=aux_utmi.rx_error,
            tx_valid=aux_utmi.tx_valid,
            tx_ready=aux_utmi.tx_ready,
            reset_detected=device.debug_reset_detected,
            setup_received=device.debug_setup_received,
            control_tx_valid=device.debug_control_tx_valid,
            control_stall=device.debug_control_stall,
        )
        dbg.status_from("aux_seen", state)
        dbg.status_from("aux_counts", state)
        dbg.status_from("last_setup_0", state, prefix="last_")
        dbg.status_from("last_setup_1", state, prefix="last_")
        dbg.status_from("device_state", state)

        led_values = (
            Mux(device.copy_failed, 1, dbg.heartbeat[-1]),
            host.enumerated,
            device.copy_done,
            aux_utmi.vbus_valid,
            state.reset_seen,
            state.setup_seen,
        )
        for index, value in enumerate(led_values):
            dbg.set_led(index, value)

        self.host = host
        self.device = device
        self.state = state
        return m


def main() -> None:
    from luna import top_level_cli

    top_level_cli(AuxCloneDiagnosticTop)


if __name__ == "__main__":
    main()
