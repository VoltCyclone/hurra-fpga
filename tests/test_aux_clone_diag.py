import re
import warnings
from types import SimpleNamespace

from amaranth import Signal
from amaranth.sim import Simulator
from cynthion.gateware.platform import CynthionPlatformRev1D4

from hurra_cynthion.aux_clone_diag import AuxCloneDebugState, AuxCloneDiagnosticTop
from hurra_cynthion.debug_block import DebugRegisterBlock
from hurra_cynthion.debug_regs import aux_clone_register_map


def test_debug_block_binds_status_fields_from_an_object() -> None:
    block = object.__new__(DebugRegisterBlock)
    block.regmap = aux_clone_register_map()
    source = SimpleNamespace(
        last_request_type=Signal(8),
        last_request=Signal(8),
        last_value=Signal(16),
    )
    captured = {}
    block.status = lambda name, **signals: captured.update(name=name, signals=signals)

    block.status_from("last_setup_0", source, prefix="last_")

    assert captured == {
        "name": "last_setup_0",
        "signals": {
            "request_type": source.last_request_type,
            "request": source.last_request,
            "value": source.last_value,
        },
    }


def test_debug_state_retains_one_cycle_aux_events_and_last_request() -> None:
    dut = AuxCloneDebugState()
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(dut.effective_connect, 1)
        ctx.set(dut.rx_active, 1)
        ctx.set(dut.rx_valid, 1)
        ctx.set(dut.tx_valid, 1)
        ctx.set(dut.reset_detected, 1)
        ctx.set(dut.setup_received, 1)
        ctx.set(dut.address_changed, 1)
        ctx.set(dut.config_changed, 1)
        ctx.set(dut.control_tx_valid, 1)
        ctx.set(dut.control_stall, 1)
        ctx.set(dut.setup_request_type, 0x80)
        ctx.set(dut.setup_request, 6)
        ctx.set(dut.setup_value, 0x0100)
        ctx.set(dut.setup_index, 0x0409)
        ctx.set(dut.setup_length, 18)
        ctx.set(dut.new_address, 5)
        ctx.set(dut.new_config, 1)
        ctx.set(dut.configured, 1)
        await ctx.tick("usb")

        ctx.set(dut.effective_connect, 0)
        ctx.set(dut.rx_active, 0)
        ctx.set(dut.rx_valid, 0)
        ctx.set(dut.tx_valid, 0)
        ctx.set(dut.reset_detected, 0)
        ctx.set(dut.setup_received, 0)
        ctx.set(dut.address_changed, 0)
        ctx.set(dut.config_changed, 0)
        ctx.set(dut.control_tx_valid, 0)
        ctx.set(dut.control_stall, 0)
        await ctx.tick("usb")

        assert ctx.get(dut.connect_seen)
        assert ctx.get(dut.rx_active_seen)
        assert ctx.get(dut.rx_valid_seen)
        assert ctx.get(dut.tx_valid_seen)
        assert ctx.get(dut.reset_seen)
        assert ctx.get(dut.setup_seen)
        assert ctx.get(dut.address_seen)
        assert ctx.get(dut.config_seen)
        assert ctx.get(dut.control_tx_seen)
        assert ctx.get(dut.control_stall_seen)
        assert ctx.get(dut.reset_count) == 1
        assert ctx.get(dut.setup_count) == 1
        assert ctx.get(dut.address_count) == 1
        assert ctx.get(dut.config_count) == 1
        assert ctx.get(dut.last_request_type) == 0x80
        assert ctx.get(dut.last_request) == 6
        assert ctx.get(dut.last_value) == 0x0100
        assert ctx.get(dut.last_index) == 0x0409
        assert ctx.get(dut.last_length) == 18
        assert ctx.get(dut.last_address) == 5
        assert ctx.get(dut.last_config) == 1

    sim.add_testbench(bench)
    sim.run()


def test_debug_state_counters_saturate_instead_of_wrapping() -> None:
    dut = AuxCloneDebugState()
    sim = Simulator(dut)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(dut.reset_detected, 1)
        for _ in range(300):
            await ctx.tick("usb")
        assert ctx.get(dut.reset_count) == 0xFF

    sim.add_testbench(bench)
    sim.run()


def test_aux_clone_diagnostic_top_preserves_production_path_and_jtag_status() -> None:
    platform = CynthionPlatformRev1D4()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        build_plan = platform.prepare(AuxCloneDiagnosticTop())
    netlist = build_plan.files["top.il"]

    assert len(re.findall(r"(?m)^\s*memory width 8 size 4096", netlist)) == 1
    assert "top.device.descriptor_store" not in netlist
    assert "top.device.copy_engine" not in netlist
    assert "cell \\JTAGG \\jtag" in netlist
    assert "register_address_matches_8" in netlist
    assert "connect \\copy_enable 1'0" not in netlist
    assert "connect \\aux_vbus_in_en_0__o 1'0" in netlist
    assert "connect \\aux_vbus_en_0__o 1'0" in netlist
    assert "connect \\control_vbus_in_en_0__o 1'1" in netlist
    assert "connect \\target_c_vbus_en_0__o 1'0" in netlist
