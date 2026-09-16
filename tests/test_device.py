import re

from amaranth import Elaboratable, Module
from amaranth.back import rtlil
from amaranth.sim import Simulator
from luna.gateware.interface.utmi import UTMIInterface

from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.device import MouseCloneDevice


class CloneTop(Elaboratable):
    """Own the shared descriptor store beside the clone, as production does."""

    def __init__(self):
        self.bus = UTMIInterface()
        self.store = DescriptorStore()
        self.dut = MouseCloneDevice(bus=self.bus, store=self.store)

    def elaborate(self, platform):
        del platform
        m = Module()
        m.submodules.store = self.store
        m.submodules.dut = self.dut
        return m


def test_mouse_clone_device_elaborates_with_one_shared_descriptor_store():
    top = CloneTop()
    store, dut = top.store, top.dut
    assert dut.store is store
    assert dut._std_handler._store is store
    assert not hasattr(dut, "copy_engine")
    assert hasattr(dut, "copy_enable")
    assert hasattr(dut, "copy_done")
    assert hasattr(dut, "copy_failed")
    for name in (
        "debug_reset_detected",
        "debug_setup_received",
        "debug_setup_request_type",
        "debug_setup_request",
        "debug_setup_value",
        "debug_setup_index",
        "debug_setup_length",
        "debug_address_changed",
        "debug_new_address",
        "debug_config_changed",
        "debug_new_config",
        "debug_control_tx_valid",
        "debug_control_stall",
    ):
        assert hasattr(dut, name), f"MouseCloneDevice lacks passive output {name}"
    netlist = rtlil.convert(
        top,
        ports=[
            dut.connect,
            dut.copy_enable,
            dut.copy_done,
            dut.copy_failed,
            dut.configured,
            dut.report_valid,
            dut.report_ready,
            dut.report_data,
            dut.report_first,
            dut.report_last,
            dut.report_endpoint,
            dut.debug_reset_detected,
            dut.debug_setup_received,
            dut.debug_setup_request_type,
            dut.debug_setup_request,
            dut.debug_setup_value,
            dut.debug_setup_index,
            dut.debug_setup_length,
            dut.debug_address_changed,
            dut.debug_new_address,
            dut.debug_config_changed,
            dut.debug_new_config,
            dut.debug_control_tx_valid,
            dut.debug_control_stall,
        ],
    )
    assert "module" in netlist
    assert len(re.findall(r"(?m)^\s*memory width 8 size 4096", netlist)) == 1
    assert "top.dut.descriptor_store" not in netlist
    assert "top.dut.copy_engine" not in netlist
    assert len(dut.relay.streams) == 4


def test_clone_readiness_and_connection_drop_on_same_cycle_descriptor_mutation():
    top = CloneTop()
    bus, store, dut = top.bus, top.store, top.dut
    simulation = Simulator(top)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        ctx.set(bus.tx_ready, 1)
        ctx.set(bus.line_state, 1)
        ctx.set(dut.connect, 1)
        ctx.set(dut.copy_enable, 1)
        for _ in range(20):
            await ctx.tick("usb")
            if ctx.get(bus.term_select):
                break
        assert ctx.get(dut.copy_done)
        assert not ctx.get(dut.copy_failed)
        assert ctx.get(bus.term_select)

        # Host enumeration is the compatibility enable. Its removal must
        # disconnect the clone combinationally rather than one clock later.
        ctx.set(dut.copy_enable, 0)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.copy_done)
        assert not ctx.get(bus.term_select)
        ctx.set(dut.copy_enable, 1)
        await ctx.delay(1e-9)
        assert ctx.get(dut.copy_done)
        assert ctx.get(bus.term_select)

        # Clear invalidates the generation immediately, before the clock edge
        # on which DescriptorStore advances descriptor_generation.
        generation = ctx.get(store.descriptor_generation)
        ctx.set(store.clear, 1)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.copy_done)
        assert not ctx.get(bus.term_select)
        await ctx.tick("usb")
        assert ctx.get(store.descriptor_generation) == (generation + 1) & 0xFFFF
        ctx.set(store.clear, 0)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.copy_done)
        assert not ctx.get(bus.term_select)

        # A mutation fails readiness closed until the host lifecycle explicitly
        # drops enable and begins a new stable generation.
        ctx.set(dut.copy_enable, 0)
        await ctx.tick("usb")
        ctx.set(dut.copy_enable, 1)
        await ctx.delay(1e-9)
        assert ctx.get(dut.copy_done)
        assert ctx.get(bus.term_select)

        # capture_busy is registered, so capture_start must independently
        # disconnect the clone during the first mutation cycle.
        ctx.set(store.capture_start, 1)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.copy_done)
        assert not ctx.get(bus.term_select)
        await ctx.tick("usb")
        ctx.set(store.capture_start, 0)
        await ctx.delay(1e-9)
        assert ctx.get(store.capture_busy)
        assert not ctx.get(dut.copy_done)
        assert not ctx.get(bus.term_select)

        ctx.set(store.capture_abort, 1)
        await ctx.tick("usb")
        ctx.set(store.capture_abort, 0)
        await ctx.delay(1e-9)
        assert not ctx.get(store.capture_busy)
        assert not ctx.get(dut.copy_done)
        ctx.set(dut.copy_enable, 0)
        await ctx.tick("usb")
        ctx.set(dut.copy_enable, 1)
        await ctx.delay(1e-9)
        assert ctx.get(dut.copy_done)
        assert ctx.get(bus.term_select)

    simulation.add_testbench(bench)
    simulation.run()


def test_endpoint_submodule_names_are_stable_across_elaborations():
    """Synthesis must be reproducible, which means no name may come from id().

    LUNA names endpoint submodules after their class and falls back to
    ``f"{name}_{id(endpoint)}"`` for the second and later instance of the same
    class (``luna/gateware/usb/usb2/device.py``). All three relay endpoints
    share a base class, so two of the three used to be named after an object
    address. That produced a different netlist every build: two runs from
    identical source on an identical toolchain gave seed 9 at 59.69 MHz FAIL
    and 62.21 MHz PASS, i.e. naming churn alone moved the pinned seed across
    the 60.00 MHz constraint.

    Both tops are kept alive simultaneously so their endpoints cannot share a
    recycled address, which would mask a regression.
    """
    first, second = CloneTop(), CloneTop()
    first_il = rtlil.convert(first, ports=[first.dut.connect])
    second_il = rtlil.convert(second, ports=[second.dut.connect])

    stale = sorted(set(re.findall(r"USBStreamInEndpoint_\d{4,}", first_il)))
    assert not stale, f"endpoint named after id(): {stale}"
    assert first_il == second_il, "elaboration is not reproducible"
